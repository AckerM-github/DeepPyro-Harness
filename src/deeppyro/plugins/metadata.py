"""Plugin package metadata: parse and normalise ``plugin.toml`` (v2).

A plugin package is one directory under the user plugin directory::

    ~/.dpro/plugins/<id>/
        plugin.toml          # the only authoritative metadata
        <python package>/    # code
        _vendor/cp313/       # dependencies (optional)
        ui.toml              # declarative TUI contributions (optional)
        bundle.toml          # composition rows / overrides (optional)

The v2 schema is sectioned (``[package]``/``[compat]``/``[deps]``/``[ui]``/
``[bundle]``).  The legacy flat form (``name``/``version``/``description``/
``module``/``class``/``provides`` at top level) is still accepted and is
normalised into the same object, so existing plugin directories keep working.

This module never imports plugin code and never mutates ``sys.path``; it is
pure parsing plus version checks, which keeps it usable from the manager, the
TUI and ``dpyro plugin doctor`` alike.
"""

from __future__ import annotations

import importlib.metadata
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# -- failure classification ------------------------------------------------

#: ``metadata-invalid``: plugin.toml missing/unparsable/ill-typed.
FAILURE_METADATA = "metadata-invalid"
#: ``import-failed``: the declared module/class could not be imported.
FAILURE_IMPORT = "import-failed"
#: ``dep-missing``: a declared dependency is not present in the vendor dir.
FAILURE_DEP_MISSING = "dep-missing"
#: ``dep-conflict``: two plugins vendor different versions of the same package.
FAILURE_DEP_CONFLICT = "dep-conflict"
#: ``abi-mismatch``: the vendor directory targets another Python minor.
FAILURE_ABI = "abi-mismatch"
#: ``version-incompat``: ``[compat]`` does not accept this host.
FAILURE_INCOMPAT = "version-incompat"

#: Every failure class a plugin row can carry.
FAILURE_KINDS: tuple[str, ...] = (
    FAILURE_METADATA,
    FAILURE_IMPORT,
    FAILURE_DEP_MISSING,
    FAILURE_DEP_CONFLICT,
    FAILURE_ABI,
    FAILURE_INCOMPAT,
)

#: One-line remediation shown next to a failure class.
REMEDIATION: dict[str, str] = {
    FAILURE_METADATA: "fix plugin.toml (see docs/spec/plugin-platform.md) and rerun 'dpyro plugin doctor'",
    FAILURE_IMPORT: "run 'dpyro plugin doctor --verbose' to see the import traceback",
    FAILURE_DEP_MISSING: "run 'dpyro plugin add <id>' (or rebuild _vendor with uv) to install its dependencies",
    FAILURE_DEP_CONFLICT: "uninstall one of the plugins that vendor different versions of the same package",
    FAILURE_ABI: "rebuild _vendor for this interpreter: uv pip install --python 3.13 --target _vendor/cp313 -r requirements.txt",
    FAILURE_INCOMPAT: "upgrade the plugin, upgrade deeppyro, or pin it with 'dpyro plugin allow-version'",
}

#: Slot kinds a package may declare in ``[ui].slots``.
KNOWN_SLOT_KINDS: tuple[str, ...] = ("status", "conversation", "panel", "settings", "screen")

_DEFAULT_VENDOR = "_vendor"


class MetadataError(Exception):
    """A classified plugin metadata failure.

    ``kind`` is one of :data:`FAILURE_KINDS`; ``remediation`` is a short,
    actionable sentence. Callers render both instead of a bare traceback.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        plugin_id: str = "",
        remediation: str | None = None,
    ) -> None:
        self.kind = kind
        self.message = message
        self.plugin_id = plugin_id
        self.remediation = REMEDIATION.get(kind, "") if remediation is None else remediation
        super().__init__(f"{kind}: {message}")


# -- value objects ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Compat:
    """Host compatibility window (mirrors DSH ``engines.dsh``)."""

    dpyro: str = ""
    python: str = ""
    exempt_versions: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.dpyro and not self.python and not self.exempt_versions


@dataclass(frozen=True, slots=True)
class Deps:
    """Vendored dependency declaration."""

    vendor: str = _DEFAULT_VENDOR
    requires: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.requires


@dataclass(frozen=True, slots=True)
class UiDeclaration:
    """Declarative TUI contribution surface (mirrors DSH ``dsh.client``)."""

    slots: tuple[str, ...] = ()
    package: str = ""

    def is_empty(self) -> bool:
        return not self.slots and not self.package


@dataclass(frozen=True, slots=True)
class BundleDeclaration:
    """Composition rows shipped inside the package (mirrors DSH bundle patch)."""

    patch: str = ""

    def is_empty(self) -> bool:
        return not self.patch


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """Normalised ``plugin.toml`` for one package directory."""

    id: str
    name: str
    version: str
    description: str
    module: str
    class_name: str
    api_version: int
    directory: Path
    provides: tuple[str, ...] = ()
    compat: Compat = field(default_factory=Compat)
    deps: Deps = field(default_factory=Deps)
    ui: UiDeclaration = field(default_factory=UiDeclaration)
    bundle: BundleDeclaration = field(default_factory=BundleDeclaration)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> str:
        """``module:Class`` string used by the manifest and the loader."""
        return f"{self.module}:{self.class_name}" if self.class_name else self.module

    @property
    def toml_path(self) -> Path:
        return self.directory / "plugin.toml"

    def with_directory(self, directory: Path) -> PluginMetadata:
        return PluginMetadata(
            id=self.id,
            name=self.name,
            version=self.version,
            description=self.description,
            module=self.module,
            class_name=self.class_name,
            api_version=self.api_version,
            directory=Path(directory),
            provides=self.provides,
            compat=self.compat,
            deps=self.deps,
            ui=self.ui,
            bundle=self.bundle,
            raw=self.raw,
        )


# -- version helpers ------------------------------------------------------


def python_tag(version_info: Any = None) -> str:
    """``cp313`` style tag for the running interpreter."""
    info = version_info if version_info is not None else sys.version_info
    return f"cp{info[0]}{info[1]}"


def dpyro_version() -> str:
    """Installed deeppyro version (falls back to the package ``__version__``)."""
    try:
        return importlib.metadata.version("deeppyro-harness")
    except Exception:  # noqa: BLE001 - frozen builds may lack metadata
        from deeppyro import __version__

        return __version__


def _version_parts(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(text).strip().split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _compare(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    size = max(len(left), len(right))
    for index in range(size):
        first = left[index] if index < len(left) else 0
        second = right[index] if index < len(right) else 0
        if first != second:
            return -1 if first < second else 1
    return 0


def satisfies(version: str, spec: str) -> bool:
    """Check ``version`` against a comma separated constraint list.

    Supports ``>=``, ``<=``, ``>``, ``<``, ``==``, ``!=`` and ``=``; an empty
    spec accepts everything. Mirrors the semantics the bundle loader uses for
    ``[bundle].requires`` so plugin and bundle compatibility agree.
    """
    text = str(spec or "").strip()
    if not text or text == "*":
        return True
    for clause in text.split(","):
        clause = clause.strip()
        if not clause:
            continue
        for operator in (">=", "<=", "==", "!=", ">", "<", "="):
            if clause.startswith(operator):
                bound = clause[len(operator) :].strip()
                break
        else:
            raise ValueError(f"invalid version constraint {clause!r}")
        if not bound:
            raise ValueError(f"invalid version constraint {clause!r}")
        result = _compare(_version_parts(version), _version_parts(bound))
        if operator in (">=",) and result < 0:
            return False
        if operator in ("<=",) and result > 0:
            return False
        if operator in (">",) and result <= 0:
            return False
        if operator in ("<",) and result >= 0:
            return False
        if operator in ("==", "=") and result != 0:
            return False
        if operator == "!=" and result == 0:
            return False
    return True


# -- parsing --------------------------------------------------------------


def _as_str(value: Any, field_name: str, *, strict: bool = True) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if strict:
        raise MetadataError(FAILURE_METADATA, f"field {field_name!r} must be a string")
    return str(value)


def _as_str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Iterable):
        raise MetadataError(FAILURE_METADATA, f"field {field_name!r} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MetadataError(FAILURE_METADATA, f"field {field_name!r} must be a list of strings")
        items.append(item)
    return tuple(items)


def _section(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MetadataError(FAILURE_METADATA, f"section [{name}] must be a table")
    return value


def parse_metadata(
    document: Mapping[str, Any],
    *,
    plugin_id: str,
    directory: str | Path = ".",
    known_slots: Iterable[str] | None = None,
) -> PluginMetadata:
    """Normalise a parsed ``plugin.toml`` document.

    Raises :class:`MetadataError` with ``kind=metadata-invalid`` for any
    structural problem. Unknown keys are kept in ``raw`` (reserved for future
    format revisions) but never rejected, matching the loader's forward
    compatibility policy.
    """
    directory = Path(directory)
    if not isinstance(document, Mapping):
        raise MetadataError(FAILURE_METADATA, "plugin.toml must contain a TOML table", plugin_id=plugin_id)

    package = _section(document, "package")
    compat_section = _section(document, "compat")
    deps_section = _section(document, "deps")
    ui_section = _section(document, "ui")
    bundle_section = _section(document, "bundle")

    # A v2 document keeps identity in [package]; the legacy form keeps the same
    # keys at top level. Merge with the section winning so both work.
    def identity(key: str) -> Any:
        if key in package:
            return package[key]
        return document.get(key)

    package_id = _as_str(identity("id"), "package.id", strict=False) or plugin_id
    if plugin_id and package_id != plugin_id:
        raise MetadataError(
            FAILURE_METADATA,
            f"[package].id is {package_id!r} but the directory is {plugin_id!r}",
            plugin_id=plugin_id,
        )
    module = _as_str(identity("module"), "module").strip()
    class_name = _as_str(identity("class"), "class").strip()
    if not module:
        raise MetadataError(
            FAILURE_METADATA, "plugin.toml requires a non-empty 'module' string", plugin_id=plugin_id
        )
    if ":" in module:
        module_part, _, class_part = module.partition(":")
        module = module_part.strip()
        class_name = class_name or class_part.strip()
        if not module:
            raise MetadataError(FAILURE_METADATA, "module must not start with ':'", plugin_id=plugin_id)
    if not class_name:
        raise MetadataError(
            FAILURE_METADATA,
            "plugin.toml must set 'class' or use 'module:Class' in 'module'",
            plugin_id=plugin_id,
        )

    api_version_raw = identity("api_version")
    if api_version_raw is None:
        if package:
            # The sectioned format is the published one: an explicit api_version
            # is how a package states which host contract it targets.
            raise MetadataError(
                FAILURE_METADATA,
                "plugin.toml must declare an integer 'api_version' (see deeppyro.api.API_VERSION)",
                plugin_id=plugin_id,
            )
        # Legacy flat packages predate api_version; treat them as v1 so existing
        # directories keep loading after the upgrade.
        from deeppyro.api import API_VERSION

        api_version_raw = API_VERSION
    if isinstance(api_version_raw, bool) or not isinstance(api_version_raw, int):
        raise MetadataError(FAILURE_METADATA, "field 'api_version' must be an integer", plugin_id=plugin_id)

    slots = _as_str_tuple(ui_section.get("slots"), "ui.slots")
    if known_slots is not None:
        allowed = tuple(known_slots)
        for slot in slots:
            if slot not in allowed:
                raise MetadataError(
                    FAILURE_METADATA,
                    f"[ui].slots declares unknown slot kind {slot!r} (known: {', '.join(allowed)})",
                    plugin_id=plugin_id,
                )

    metadata = PluginMetadata(
        id=package_id,
        name=_as_str(identity("name"), "name", strict=False) or package_id,
        version=_as_str(identity("version"), "version", strict=False),
        description=_as_str(identity("description"), "description", strict=False),
        module=module,
        class_name=class_name,
        api_version=api_version_raw,
        directory=directory,
        provides=_as_str_tuple(identity("provides"), "provides"),
        compat=Compat(
            dpyro=_as_str(compat_section.get("dpyro"), "compat.dpyro", strict=False),
            python=_as_str(compat_section.get("python"), "compat.python", strict=False),
            exempt_versions=_as_str_tuple(
                compat_section.get("dpyro_exempt_versions", compat_section.get("exempt_versions")),
                "compat.dpyro_exempt_versions",
            ),
        ),
        deps=Deps(
            vendor=_as_str(deps_section.get("vendor"), "deps.vendor", strict=False) or _DEFAULT_VENDOR,
            requires=_as_str_tuple(deps_section.get("requires"), "deps.requires"),
        ),
        ui=UiDeclaration(
            slots=slots,
            package=_as_str(ui_section.get("package"), "ui.package", strict=False),
        ),
        bundle=BundleDeclaration(
            patch=_as_str(bundle_section.get("patch"), "bundle.patch", strict=False),
        ),
        raw=dict(document),
    )
    return metadata


def parse_toml(data: bytes) -> dict[str, Any]:
    """Decode TOML bytes, tolerating a UTF-8 BOM written by Windows editors."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return tomllib.loads(data.decode("utf-8"))


def load_metadata(
    source: str | Path,
    *,
    plugin_id: str | None = None,
    known_slots: Iterable[str] | None = None,
) -> PluginMetadata:
    """Load and normalise the metadata of one package directory or plugin.toml."""
    path = Path(source)
    directory = path.parent if path.name == "plugin.toml" else path
    toml_path = path if path.name == "plugin.toml" else path / "plugin.toml"
    resolved_id = plugin_id if plugin_id is not None else directory.name
    if not toml_path.is_file():
        raise MetadataError(
            FAILURE_METADATA,
            f"{toml_path} does not exist",
            plugin_id=resolved_id,
        )
    try:
        document = parse_toml(toml_path.read_bytes())
    except tomllib.TOMLDecodeError as exc:
        raise MetadataError(FAILURE_METADATA, f"{toml_path}: {exc}", plugin_id=resolved_id) from exc
    except OSError as exc:
        raise MetadataError(
            FAILURE_METADATA, f"cannot read {toml_path}: {exc}", plugin_id=resolved_id
        ) from exc
    return parse_metadata(document, plugin_id=resolved_id, directory=directory, known_slots=known_slots)


# -- compatibility and vendor layout -------------------------------------


def check_compat(
    metadata: PluginMetadata,
    *,
    python_version: Any = None,
    host_version: str | None = None,
    api_version: int | None = None,
) -> list[MetadataError]:
    """Return every compatibility failure for ``metadata`` (empty means ok)."""
    from deeppyro.api import API_VERSION

    errors: list[MetadataError] = []
    host_api = API_VERSION if api_version is None else api_version
    if metadata.api_version > host_api:
        errors.append(
            MetadataError(
                FAILURE_INCOMPAT,
                f"plugin declares api_version {metadata.api_version} but this deeppyro speaks {host_api}",
                plugin_id=metadata.id,
            )
        )
    info = python_version if python_version is not None else sys.version_info
    raw_python = f"{info[0]}.{info[1]}.{info[2]}"
    spec = metadata.compat.python
    if spec:
        try:
            if not satisfies(raw_python, spec):
                errors.append(
                    MetadataError(
                        FAILURE_INCOMPAT,
                        f"plugin requires python {spec}, host is {raw_python}",
                        plugin_id=metadata.id,
                    )
                )
        except ValueError as exc:
            errors.append(MetadataError(FAILURE_METADATA, f"[compat].python: {exc}", plugin_id=metadata.id))
    host = dpyro_version() if host_version is None else host_version
    spec = metadata.compat.dpyro
    if spec and host not in metadata.compat.exempt_versions:
        try:
            if not satisfies(host, spec):
                errors.append(
                    MetadataError(
                        FAILURE_INCOMPAT,
                        f"plugin requires deeppyro {spec}, host is {host}",
                        plugin_id=metadata.id,
                    )
                )
        except ValueError as exc:
            errors.append(MetadataError(FAILURE_METADATA, f"[compat].dpyro: {exc}", plugin_id=metadata.id))
    return errors


def vendor_candidates(metadata: PluginMetadata, *, python_version: Any = None) -> list[Path]:
    """Vendor directories in load order: ``cpXY`` first, then the plain one."""
    root = metadata.directory / metadata.deps.vendor
    tag = python_tag(python_version)
    return [root / tag, root]


def find_vendor_dir(metadata: PluginMetadata, *, python_version: Any = None) -> Path | None:
    """First existing vendor directory, or ``None``."""
    for candidate in vendor_candidates(metadata, python_version=python_version):
        if candidate.is_dir():
            return candidate
    return None


def describe_failure(kind: str, message: str) -> str:
    """``"<kind>: <message> — <remediation>"`` for UI and CLI output."""
    remediation = REMEDIATION.get(kind, "")
    if remediation:
        return f"{kind}: {message} - {remediation}"
    return f"{kind}: {message}"


__all__ = [
    "FAILURE_ABI",
    "FAILURE_DEP_CONFLICT",
    "FAILURE_DEP_MISSING",
    "FAILURE_IMPORT",
    "FAILURE_INCOMPAT",
    "FAILURE_KINDS",
    "FAILURE_METADATA",
    "KNOWN_SLOT_KINDS",
    "REMEDIATION",
    "BundleDeclaration",
    "Compat",
    "Deps",
    "MetadataError",
    "PluginMetadata",
    "UiDeclaration",
    "check_compat",
    "describe_failure",
    "dpyro_version",
    "find_vendor_dir",
    "load_metadata",
    "parse_metadata",
    "parse_toml",
    "python_tag",
    "satisfies",
    "vendor_candidates",
]
