"""Read and edit plugin configuration through the user patch manifest.

Configuration is layered like the composition itself: bundle rows provide
defaults and the user manifest patches them. Layer D adds a small settings
store on top of that:

* describe() renders the plugin Config JSON schema and the effective, masked
  config values;
* read() returns the same values plus a revision token;
* write() performs an optimistic-concurrency update (CAS) and stores the full
  effective config in the last user manifest row for the plugin, because the
  kernel replaces a row config wholesale instead of deep-merging it.

A plugin that lives inside a group is a group child: the store resolves the
short runtime id to its qualified composition address ("group-id/child-id",
repeated for nested groups) and patches that child through a normal user
manifest row with the qualified id. The bundle that declared the group is never
rewritten, so its comments, row order and bytes stay untouched; the patch can
only change config/disabled/critical, not the group structure.

Secret-looking keys are masked on read. A client that writes the masked
placeholder back does not overwrite the stored secret; omitting the key keeps
the current value.

Writes are serialized across processes by a small advisory lock file next to
the manifest (settings.toml.lock, see deeppyro.plugins.file_lock).  The CAS
token still rejects stale clients; the lock prevents two clients that both
read the same revision from silently overwriting each other.

Every write is persisted immediately but only applied on the next boot, except
for fields that are safe to hot-apply: a plugin's declared `volatile` fields
plus the shared runtime-safe whitelist (log/trace/debug levels, timeouts, retry
and concurrency limits; see deeppyro.api.capabilities.LIVE_CONFIG_TOKENS). Those
are reported separately so the settings service can hand them to the kernel
without a restart; everything else is reported as needing a restart.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from deeppyro import paths
from deeppyro.api.capabilities import live_config_fields
from deeppyro.api.errors import PluginConfigError
from deeppyro.kernel.bundle import load_composition
from deeppyro.kernel.composition import CompositionError, PluginRow, resolve_row
from deeppyro.kernel.manifest import ManifestError, PluginSpec, resolve_plugin_name

from .errors import ManifestWriteError, PluginError, PluginNotFoundError, SettingsConflict
from .file_lock import (
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    LockError,
    LockTimeout,
    file_lock,
    lock_path_for,
)
from .manifest_store import read_manifest, save_user_manifest

#: Placeholder returned for secrets; writing it back is a no-op.
MASKED_VALUE = "***"

#: A field is secret when its name contains one of these fragments.
_SECRET_PARTS = ("key", "token", "secret", "password")

#: Configuration changes take effect on the next boot by default.
APPLY_NEXT_BOOT = "next-boot"

#: Volatile fields can be applied to a mounted plugin without a restart.
APPLY_LIVE = "live"


class SettingsLockTimeout(PluginError):
    """Another process held the settings manifest lock for too long."""


# -- masking and merging -------------------------------------------------


def _is_secret(field_name: str) -> bool:
    lowered = field_name.lower()
    return any(part in lowered for part in _SECRET_PARTS)


def mask_secrets(value: Any) -> Any:
    """Return a copy of value with secret-looking keys masked."""
    if isinstance(value, Mapping):
        masked: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_secret(key) and item is not None:
                masked[key] = MASKED_VALUE
            else:
                masked[key] = mask_secrets(item)
        return masked
    if isinstance(value, (list, tuple)):
        return [mask_secrets(item) for item in value]
    return value


def _strip_masked(value: Any) -> Any:
    """Drop masked placeholders so writing a read-back payload keeps secrets."""
    if isinstance(value, Mapping):
        stripped: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_secret(key) and item == MASKED_VALUE:
                continue
            stripped[key] = _strip_masked(item)
        return stripped
    if isinstance(value, list):
        return [_strip_masked(item) for item in value]
    return value


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


# -- schema and validation ----------------------------------------------


def _config_class(plugin_class: type | None) -> type[BaseModel] | None:
    if plugin_class is None:
        return None
    candidate = getattr(plugin_class, "Config", None)
    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
        return candidate
    return None


def plugin_config_schema(plugin_class: type | None) -> dict[str, Any] | None:
    """JSON schema of a plugin Config, or None when the plugin has no Config."""
    config_class = _config_class(plugin_class)
    if config_class is None:
        return None
    try:
        return config_class.model_json_schema()
    except Exception:  # noqa: BLE001 - schema generation must never break inspection
        return None


def _format_validation_error(exc: ValidationError) -> str:
    """Summarize a pydantic error without echoing secret input values."""
    parts: list[str] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "(root)"
        message = str(item.get("msg", "invalid value"))
        parts.append(f"{location}: {message}")
        if len(parts) >= 8:
            break
    return "; ".join(parts) or "invalid configuration"


def plugin_volatile_fields(plugin_class: type | None) -> tuple[str, ...]:
    """Config field names declared as volatile on the plugin class."""
    if plugin_class is None:
        return ()
    declared = getattr(plugin_class, "volatile", ())
    if isinstance(declared, str):
        return (declared,) if declared else ()
    if isinstance(declared, (list, tuple)):
        return tuple(str(item) for item in declared if isinstance(item, str) and item)
    return ()


def plugin_live_fields(plugin_class: type | None) -> tuple[str, ...]:
    """Config fields that may be applied to a running plugin without a restart.

    Combines the plugin's declared `volatile` fields with the shared
    runtime-safe whitelist.  Every other field needs a restart.
    """
    fields: list[str] = list(plugin_volatile_fields(plugin_class))
    for name in live_config_fields(plugin_class):
        if name not in fields:
            fields.append(name)
    return tuple(fields)


def validate_config(plugin_class: type | None, values: Mapping[str, Any]) -> None:
    """Validate values against the plugin Config, raising PluginConfigError."""
    config_class = _config_class(plugin_class)
    if config_class is None:
        return
    try:
        config_class.model_validate(dict(values))
    except ValidationError as exc:
        raise PluginConfigError(f"invalid configuration: {_format_validation_error(exc)}") from exc


# -- manifest revision ---------------------------------------------------


def manifest_revision(path: str | Path) -> str:
    """Short content hash of the user manifest, used as the CAS token."""
    manifest = Path(path)
    try:
        data = manifest.read_bytes()
    except FileNotFoundError:
        data = b""
    except OSError as exc:
        raise PluginError(f"cannot read plugin manifest {manifest}: {exc}") from exc
    return hashlib.sha256(data).hexdigest()[:16]


def _last_index(specs: Sequence[PluginSpec], plugin_id: str) -> int | None:
    for index in range(len(specs) - 1, -1, -1):
        if specs[index].id == plugin_id:
            return index
    return None


def _config_source(row: PluginRow | None) -> str:
    if row is None:
        return "none"
    layers = [layer for layer in row.origin.split("|") if layer]
    if any(layer.startswith("manifest:") for layer in layers):
        return "manifest"
    if any(layer.startswith("bundle:") for layer in layers):
        return "bundle"
    return "default"


class SettingsStore:
    """Describe and edit plugin config for any plugin in the composition."""

    def __init__(
        self,
        *,
        manifest_path: str | Path | None = None,
        plugins_dir: str | Path | None = None,
        bundle_paths: Sequence[str | Path] = (),
        lock_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if manifest_path is None:
            self._manifest_path = paths.plugins_manifest_path()
        else:
            self._manifest_path = Path(manifest_path)
        if plugins_dir is None:
            if manifest_path is None:
                self._plugins_dir = paths.plugins_dir()
            else:
                self._plugins_dir = self._manifest_path.parent / "plugins"
        else:
            self._plugins_dir = Path(plugins_dir)
        self._bundle_paths = tuple(str(path) for path in bundle_paths)
        self._lock_timeout = max(0.0, float(lock_timeout))

    @property
    def manifest_path(self) -> Path:
        """The user patch manifest that stores configuration overrides."""
        return self._manifest_path

    @property
    def plugins_dir(self) -> Path:
        """Directory scanned for external plugin directories."""
        return self._plugins_dir

    @property
    def revision(self) -> str:
        """Current CAS token for this manifest."""
        return manifest_revision(self._manifest_path)

    @property
    def lock_path(self) -> Path:
        """The cross-process lock file guarding this manifest."""
        return lock_path_for(self._manifest_path)

    @property
    def lock_timeout(self) -> float:
        """Seconds write() waits for another process to release the lock."""
        return self._lock_timeout

    # -- public API --------------------------------------------------------

    def describe(self, plugin_id: str) -> dict[str, Any]:
        """Schema, origin, masked values and revision for one plugin config."""
        row, target, plugin_class, error, _address = self._lookup(plugin_id)
        values = dict(row.config or {}) if row is not None else {}
        return {
            "plugin_id": plugin_id,
            "target": target,
            "origin": row.origin if row is not None else "",
            "config_source": _config_source(row),
            "critical": row.critical is True if row is not None else False,
            "disabled": row.disabled is True if row is not None else False,
            "schema": plugin_config_schema(plugin_class),
            "values": mask_secrets(values),
            "revision": self.revision,
            "apply": APPLY_NEXT_BOOT,
            "volatile_fields": list(plugin_volatile_fields(plugin_class)),
            "live_fields": list(plugin_live_fields(plugin_class)),
            "error": error,
        }

    def read(self, plugin_id: str) -> dict[str, Any]:
        """Current masked values plus the revision required by write()."""
        row, target, plugin_class, error, _address = self._lookup(plugin_id)
        values = dict(row.config or {}) if row is not None else {}
        return {
            "plugin_id": plugin_id,
            "target": target,
            "schema": plugin_config_schema(plugin_class),
            "values": mask_secrets(values),
            "revision": self.revision,
            "apply": APPLY_NEXT_BOOT,
            "volatile_fields": list(plugin_volatile_fields(plugin_class)),
            "live_fields": list(plugin_live_fields(plugin_class)),
            "error": error,
        }

    def write(
        self,
        plugin_id: str,
        patch: Mapping[str, Any],
        expected_revision: str,
    ) -> dict[str, Any]:
        """Validate and persist a configuration patch with CAS semantics."""
        if not isinstance(patch, Mapping):
            raise PluginError("settings patch must be a JSON object")
        # The whole read-check-write cycle runs under the cross-process lock:
        # the CAS check alone cannot stop two writers that read the same
        # revision from both persisting, because each atomic replace would
        # overwrite the other's update.
        with self._settings_lock():
            return self._write_locked(plugin_id, patch, expected_revision)

    def _write_locked(
        self,
        plugin_id: str,
        patch: Mapping[str, Any],
        expected_revision: str,
    ) -> dict[str, Any]:
        """Persist a patch; the caller must hold the settings lock."""
        actual_revision = self.revision
        if expected_revision != actual_revision:
            raise SettingsConflict(
                f"stale settings revision for plugin {plugin_id!r}: "
                f"expected {expected_revision!r}, current is {actual_revision!r}"
            )
        row, target, plugin_class, _error, address = self._lookup(plugin_id)
        current = dict(row.config or {}) if row is not None else {}
        merged = _deep_merge(current, _strip_masked(patch))
        validate_config(plugin_class, merged)
        specs = self._read_specs()
        # A group child is patched through its qualified address: the bundle that
        # declared the group keeps owning the group row, so only the user manifest
        # gains/replaces one row and the bundle bytes are never rewritten.
        index = _last_index(specs, address)
        if index is None:
            if not target:
                raise PluginError(f"cannot persist settings for plugin {plugin_id!r}: no plugin target")
            specs.append(PluginSpec(id=address, name=target, config=dict(merged)))
        else:
            specs[index] = replace(specs[index], config=dict(merged))
        self._write_specs(specs)
        changed = sorted(str(key) for key in merged if current.get(key) != merged.get(key))
        live = plugin_live_fields(plugin_class)
        live_changed = sorted(key for key in changed if key in live)
        restart_fields = sorted(key for key in changed if key not in live)
        return {
            "plugin_id": plugin_id,
            "revision": self.revision,
            "values": mask_secrets(merged),
            "changed": changed,
            "volatile_fields": list(live),
            "volatile_changed": live_changed,
            "live_fields": list(live),
            "live_changed": live_changed,
            "restart_fields": restart_fields,
            # The store cannot apply anything itself: the settings service either
            # hands the live fields to the mounted kernel or reports a restart.
            "restart_required": True,
            "apply": APPLY_NEXT_BOOT,
        }

    def effective_config(self, plugin_id: str) -> dict[str, Any]:
        """Unmasked effective config used for live application.

        This must never be returned by agent tools; settings_read and
        settings_write expose masked values instead.
        """
        row, _target, _plugin_class, _error, _address = self._lookup(plugin_id)
        return dict(row.config or {}) if row is not None else {}

    # -- internals ---------------------------------------------------------

    @contextmanager
    def _settings_lock(self) -> Iterator[None]:
        """Hold the cross-process settings lock for the manifest."""
        try:
            with file_lock(self._manifest_path, timeout=self._lock_timeout):
                yield
        except LockTimeout as exc:
            raise SettingsLockTimeout(
                f"timed out after {exc.timeout:.2f}s waiting for {exc.path}; "
                f"another process may be editing the plugin manifest {self._manifest_path}"
            ) from exc
        except LockError as exc:
            raise PluginError(str(exc)) from exc

    def _manifest_paths(self) -> tuple[str, ...]:
        return (str(self._manifest_path),) if self._manifest_path.is_file() else ()

    def _lookup(
        self,
        plugin_id: str,
    ) -> tuple[PluginRow | None, str, type | None, str, str]:
        """Resolve a plugin id to (row, target, class, error, manifest address).

        The last item is the id used in the user manifest. It differs from the
        requested id for a group child: writes address it as "parent/child"
        (repeated for nested groups) so the declaring bundle row -- and
        therefore the whole group table -- is never rewritten.
        """
        if self._manifest_path.is_file():
            # Surface a broken user manifest here: load_composition records
            # parse failures as warnings and would report the plugin as unknown.
            self._read_specs()
        composition = load_composition(
            manifest_paths=self._manifest_paths(),
            bundle_paths=self._bundle_paths,
        )
        resolved = composition.find(plugin_id)
        row: PluginRow | None = None
        address = plugin_id
        if resolved is not None:
            address, row = resolved
        if row is not None and row.plugin:
            target = row.plugin
            try:
                return row, target, resolve_row(row), "", address
            except (CompositionError, ManifestError) as exc:
                return row, target, None, str(exc), address
        if row is not None:
            return row, "", None, "", address
        target = self._target_from_manager(plugin_id)
        if target is None:
            raise PluginNotFoundError(f"unknown plugin {plugin_id!r}")
        try:
            plugin_class = resolve_plugin_name(target)
        except ManifestError as exc:
            return None, target, None, str(exc), address
        return None, target, plugin_class, "", address

    def _target_from_manager(self, plugin_id: str) -> str | None:
        from .manager import PluginManager

        manager = PluginManager(manifest_path=self._manifest_path, plugins_dir=self._plugins_dir)
        info = manager.get(plugin_id)
        if info is None:
            return None
        if info.target:
            return info.target
        result = manager.validate(plugin_id)
        return result.target if result.ok else None

    def _read_specs(self) -> list[PluginSpec]:
        try:
            return list(read_manifest(self._manifest_path))
        except (ManifestError, OSError) as exc:
            raise PluginError(f"cannot read plugin manifest {self._manifest_path}: {exc}") from exc

    def _write_specs(self, specs: Sequence[PluginSpec]) -> None:
        """Persist specs, retrying transient Windows sharing violations.

        CPython opens files without FILE_SHARE_DELETE, so os.replace() can fail
        with a sharing violation while another process happens to be reading
        the manifest.  Those readers hold the file only briefly and are not
        coordinated through our lock (read-only inspection never blocks), so
        retry for a bounded time instead of surfacing a spurious write error.
        """
        deadline = time.monotonic() + self._lock_timeout
        while True:
            try:
                save_user_manifest(self._manifest_path, tuple(specs))
                return
            except ManifestWriteError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(DEFAULT_POLL_SECONDS, remaining))


__all__ = [
    "APPLY_LIVE",
    "APPLY_NEXT_BOOT",
    "MASKED_VALUE",
    "SettingsLockTimeout",
    "SettingsStore",
    "manifest_revision",
    "mask_secrets",
    "plugin_config_schema",
    "plugin_volatile_fields",
    "validate_config",
]
