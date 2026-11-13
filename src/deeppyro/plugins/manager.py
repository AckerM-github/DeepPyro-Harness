"""External plugin manager.

The manager lists, validates, enables, disables and removes plugins that live
outside this source tree. Enable/disable state is stored in the user patch
manifest (the same [[plugins]] layer the kernel merges at boot): removing the
disabled flag enables a plugin, setting disabled = true disables it. This is
exactly the kernel manifest semantics, so no separate state file is needed.

Built-in bundle plugins are never listed or modified by the manager.
"""

from __future__ import annotations

import importlib
import shutil
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from deeppyro.kernel.manifest import ManifestError, PluginSpec, resolve_plugin_name
from deeppyro.plugins.bundles import BundleStore
from deeppyro.plugins.discovery import PluginDiscovery
from deeppyro.plugins.errors import (
    PluginError,
    PluginNotFoundError,
    PluginProtectionError,
    PluginRemovalError,
)
from deeppyro.plugins.manifest_store import read_manifest, save_user_manifest
from deeppyro.plugins.model import (
    SOURCE_DIR,
    SOURCE_ENTRY_POINT,
    BundleInfo,
    PluginInfo,
    ValidationResult,
)


class PluginManager:
    """Manage external plugins and their enable/disable state.

    Every mutating method rescans the sources before returning, so the cached
    rows always reflect what was written to disk.
    """

    def __init__(
        self,
        *,
        manifest_path: str | Path | None = None,
        plugins_dir: str | Path | None = None,
        entry_points: Callable[[], Any] | None = None,
        bundle_ids: Iterable[str] | None = None,
        inject_sys_path: bool = True,
        bundles_root: str | Path | None = None,
    ) -> None:
        self._discovery = PluginDiscovery(
            manifest_path=manifest_path,
            plugins_dir=plugins_dir,
            entry_points=entry_points,
            bundle_ids=bundle_ids,
            inject_sys_path=inject_sys_path,
        )
        self._bundles = BundleStore(
            manifest_path=self._discovery.manifest_path,
            root=bundles_root,
            bundle_ids=self._discovery.bundle_ids,
        )
        self._plugins: dict[str, PluginInfo] = {}
        self.refresh()

    # -- readonly state --------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        """The user patch manifest this manager reads and writes."""
        return self._discovery.manifest_path

    @property
    def plugins_dir(self) -> Path:
        """Directory scanned for plugin directories (one per plugin)."""
        return self._discovery.plugins_dir

    @property
    def bundle_ids(self) -> frozenset[str]:
        """Plugin ids owned by the built-in bundle; operations on them are refused."""
        return self._discovery.bundle_ids

    @property
    def bundles_root(self) -> Path:
        """Directory holding installed user bundles (one subdirectory each)."""
        return self._bundles.root

    @property
    def errors(self) -> tuple[str, ...]:
        """Non-fatal problems from the last scan (unreadable sources, bad entries)."""
        return tuple(self._discovery.errors)

    @property
    def warnings(self) -> tuple[str, ...]:
        """Non-fatal notices from the last scan (skipped duplicates, bundle ids)."""
        return tuple(self._discovery.warnings)

    # -- listing ---------------------------------------------------------

    def list(self) -> list[PluginInfo]:
        """Current plugin rows, in display order."""
        return list(self._plugins.values())

    def get(self, plugin_id: str) -> PluginInfo | None:
        """One row, or None when the id is not provided externally."""
        return self._plugins.get(plugin_id)

    def refresh(self) -> list[PluginInfo]:
        """Rescan all sources and return the new rows."""
        rows = self._discovery.scan()
        self._plugins = {row.id: row for row in rows}
        return self.list()

    # -- validation ------------------------------------------------------

    def validate(self, plugin_id: str) -> ValidationResult:
        """Import the plugin target without booting the kernel.

        Returns a ValidationResult instead of raising, so the TUI can show the
        import error next to the plugin.
        """
        info = self._require(plugin_id)
        target: str | None = None
        try:
            target = self._target_for(info)
            module_name, _, attribute = target.partition(":")
            if not module_name or not attribute:
                raise PluginError(f"plugin target {target!r} is not module:Class")
            importlib.import_module(module_name)
            plugin_class = resolve_plugin_name(target)
            provides = _provided_services(plugin_class)
        except Exception as exc:  # noqa: BLE001 - report arbitrary import failures
            return ValidationResult(id=plugin_id, ok=False, target=target, error=str(exc))
        self._plugins[plugin_id] = replace(info, target=target, provides=provides, error=None)
        return ValidationResult(id=plugin_id, ok=True, target=target, provides=provides)

    # -- enable / disable ------------------------------------------------

    def enable(self, plugin_id: str) -> PluginInfo:
        """Enable a plugin by clearing the disabled flag in the user manifest."""
        self._guard_bundle(plugin_id)
        info = self._require(plugin_id)
        if info.enabled:
            return info
        result = self.validate(plugin_id)
        if not result.ok:
            raise PluginError(f"cannot enable plugin {plugin_id!r}: {result.error}")
        target = result.target or info.target
        if not target:
            raise PluginError(f"cannot enable plugin {plugin_id!r}: no resolvable target")
        specs = self._read_specs()
        index = _last_index(specs, plugin_id)
        if index is None:
            specs.append(PluginSpec(id=plugin_id, name=target))
        else:
            current = specs[index]
            specs[index] = replace(current, name=current.name or target, disabled=None)
        self._write_specs(specs)
        self.refresh()
        return self._plugins[plugin_id]

    def disable(self, plugin_id: str) -> PluginInfo:
        """Disable a plugin by setting disabled = true in the user manifest."""
        self._guard_bundle(plugin_id)
        info = self._require(plugin_id)
        if not info.enabled:
            return info
        target = info.target
        if target is None:
            try:
                target = self._target_for(info)
            except PluginError:
                target = None
        specs = self._read_specs()
        index = _last_index(specs, plugin_id)
        if index is None:
            if target is None:
                raise PluginError(f"cannot disable plugin {plugin_id!r}: no resolvable target to record")
            specs.append(PluginSpec(id=plugin_id, name=target, disabled=True))
        else:
            current = specs[index]
            specs[index] = replace(current, name=current.name or target, disabled=True)
        self._write_specs(specs)
        self.refresh()
        return self._plugins[plugin_id]

    # -- bundle lifecycle ------------------------------------------------

    def list_bundles(self) -> list[BundleInfo]:
        """Installed user bundles with their load and override state."""
        return list(self._bundles.list())

    def install_bundle(self, source: str | Path, *, force: bool = False) -> BundleInfo:
        """Copy a bundle directory into the user bundle store."""
        return self._bundles.install(source, force=force)

    def remove_bundle(self, bundle_id: str) -> BundleInfo:
        """Delete an installed user bundle and its orphaned manifest patches."""
        return self._bundles.remove(bundle_id)

    def set_bundle(self, bundle_id: str, enabled: bool) -> BundleInfo:
        """Enable or disable every plugin row contributed by a bundle."""
        return self._bundles.set_enabled(bundle_id, enabled)

    # -- removal ---------------------------------------------------------

    def remove(self, plugin_id: str) -> None:
        """Remove a plugin directory or manifest entry (never pip uninstall)."""
        self._guard_bundle(plugin_id)
        info = self._require(plugin_id)
        if info.source == SOURCE_ENTRY_POINT:
            raise PluginRemovalError(
                f"plugin {plugin_id!r} is installed as a Python distribution; uninstall it with pip instead"
            )
        if info.source == SOURCE_DIR:
            directory = Path(info.origin).parent
            if not self._inside_plugins_dir(directory):
                raise PluginRemovalError(
                    f"refusing to remove directory outside {self.plugins_dir}: {directory}"
                )
            self._drop_manifest_entry(plugin_id)
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                raise PluginRemovalError(f"cannot remove plugin directory {directory}: {exc}") from exc
        else:
            self._drop_manifest_entry(plugin_id, required=True)
        self.refresh()

    # -- internals -------------------------------------------------------

    def _require(self, plugin_id: str) -> PluginInfo:
        info = self._plugins.get(plugin_id)
        if info is None:
            self.refresh()
            info = self._plugins.get(plugin_id)
        if info is None:
            raise PluginNotFoundError(f"unknown external plugin {plugin_id!r}")
        return info

    def _guard_bundle(self, plugin_id: str) -> None:
        if plugin_id in self._discovery.bundle_ids:
            raise PluginProtectionError(
                f"plugin {plugin_id!r} is part of the built-in bundle and cannot be managed here"
            )

    def _target_for(self, info: PluginInfo) -> str:
        if info.target:
            return info.target
        if info.source == SOURCE_ENTRY_POINT:
            entry = self._discovery.entry_point(info.id)
            if entry is None:
                raise PluginError(f"entry point for plugin {info.id!r} is no longer installed")
            try:
                loaded = entry.load()
            except Exception as exc:  # entry point loaders are arbitrary code
                raise PluginError(f"cannot load entry point {info.id!r}: {exc}") from exc
            plugin_class = _plugin_class(loaded)
            if plugin_class is None:
                raise PluginError(f"entry point {info.id!r} did not resolve to a plugin class")
            qualname = plugin_class.__qualname__
            if "<locals>" in qualname:
                raise PluginError(f"plugin class {qualname!r} is not importable by name")
            return f"{plugin_class.__module__}:{qualname}"
        if info.error:
            raise PluginError(info.error)
        raise PluginError(f"plugin {info.id!r} has no resolvable target")

    def _inside_plugins_dir(self, directory: Path) -> bool:
        root = self.plugins_dir.resolve()
        try:
            candidate = directory.resolve()
        except OSError:
            return False
        return candidate != root and root in candidate.parents

    def _read_specs(self) -> list[PluginSpec]:
        try:
            return list(read_manifest(self.manifest_path))
        except (ManifestError, OSError) as exc:
            raise PluginError(f"cannot read user manifest {self.manifest_path}: {exc}") from exc

    def _write_specs(self, specs: list[PluginSpec]) -> None:
        save_user_manifest(self.manifest_path, specs)

    def _drop_manifest_entry(self, plugin_id: str, *, required: bool = False) -> None:
        specs = self._read_specs()
        index = _last_index(specs, plugin_id)
        if index is None:
            if required:
                raise PluginRemovalError(f"plugin {plugin_id!r} has no manifest entry to remove")
            return
        del specs[index]
        self._write_specs(specs)


def _last_index(specs: list[PluginSpec], plugin_id: str) -> int | None:
    for index in range(len(specs) - 1, -1, -1):
        if specs[index].id == plugin_id:
            return index
    return None


def _plugin_class(loaded: Any) -> type | None:
    if isinstance(loaded, type):
        return loaded
    candidate = getattr(loaded, "Plugin", None)
    if isinstance(candidate, type):
        return candidate
    return None


def _provided_services(plugin_class: type) -> tuple[str, ...]:
    provide = getattr(plugin_class, "provide", None)
    if isinstance(provide, str) and provide:
        return (provide,)
    if isinstance(provide, (list, tuple)):
        return tuple(str(item) for item in provide if isinstance(item, str) and item)
    return ()


__all__ = ["PluginManager"]
