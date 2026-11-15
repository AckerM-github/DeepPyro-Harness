"""Discovery of external DeepPyro plugins from the supported sources.

Three sources are scanned, in decreasing precedence:

1. directories under the user plugin directory (each with a plugin.toml);
2. Python entry points in the deeppyro.plugins group;
3. the user patch manifest ([[plugins]] entries that are not provided by an
   installed source).

Discovery never imports plugin code; PluginManager.validate does that. A row
whose source cannot be parsed carries a non-empty error string instead of
raising, so one broken plugin cannot hide the rest of the list.
"""

from __future__ import annotations

import importlib.metadata
import sys
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from deeppyro import paths
from deeppyro.kernel.manifest import ManifestError, PluginSpec, load_manifest
from deeppyro.plugins.metadata import MetadataError, load_metadata
from deeppyro.plugins.model import (
    SOURCE_DIR,
    SOURCE_ENTRY_POINT,
    SOURCE_MANIFEST,
    PluginInfo,
)

#: Entry point group read from installed distributions.
ENTRY_POINT_GROUP = "deeppyro.plugins"

_STRING_FIELDS = ("name", "version", "description", "class")


def _entry_point_group(entry: Any) -> str:
    return str(getattr(entry, "group", ENTRY_POINT_GROUP) or ENTRY_POINT_GROUP)


class PluginDiscovery:
    """Scan entry points, plugin directories and the user manifest.

    The constructor accepts explicit paths and an entry-point factory so tests
    can run against a temporary tree without touching the real user config
    directory. No kernel boot happens here.
    """

    def __init__(
        self,
        *,
        manifest_path: str | Path | None = None,
        plugins_dir: str | Path | None = None,
        entry_points: Callable[[], Any] | None = None,
        bundle_ids: Iterable[str] | None = None,
        inject_sys_path: bool = True,
    ) -> None:
        explicit_manifest = manifest_path is not None
        if manifest_path is None:
            manifest_path = paths.plugins_manifest_path()
        if bundle_ids is None:
            from deeppyro.kernel.bundle import DEFAULT_PLUGINS

            bundle_ids = (spec.id for spec in DEFAULT_PLUGINS)
        self.manifest_path = Path(manifest_path)
        if plugins_dir is not None:
            self.plugins_dir = Path(plugins_dir)
        elif explicit_manifest:
            self.plugins_dir = self.manifest_path.parent / "plugins"
        else:
            self.plugins_dir = paths.plugins_dir()
        self._entry_points_factory = entry_points
        self._bundle_ids = frozenset(bundle_ids)
        self.inject_sys_path = inject_sys_path
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._plugins: dict[str, PluginInfo] = {}
        self._entry_point_map: dict[str, Any] = {}

    @property
    def bundle_ids(self) -> frozenset[str]:
        """Ids owned by the built-in bundle layer; the manager never edits them."""
        return self._bundle_ids

    @property
    def plugins(self) -> tuple[PluginInfo, ...]:
        """Rows from the most recent scan, in display order."""
        return tuple(self._plugins.values())

    def entry_point(self, plugin_id: str) -> Any | None:
        """The importlib EntryPoint object behind an entry-point sourced row."""
        return self._entry_point_map.get(plugin_id)

    def scan(self) -> tuple[PluginInfo, ...]:
        """Rescan every source and return the resulting rows."""
        self.errors.clear()
        self.warnings.clear()
        self._entry_point_map = {}
        discovered: dict[str, PluginInfo] = {}

        self._scan_directories(discovered)
        self._scan_entry_points(discovered)
        specs = self._load_user_manifest()
        enabled = {spec.id: spec.enabled() for spec in specs if spec.id not in self._bundle_ids}
        self._scan_manifest_entries(specs, discovered)

        rows = [replace(info, enabled=enabled.get(info.id, False)) for info in discovered.values()]
        self._plugins = {row.id: row for row in rows}
        return tuple(rows)

    # -- helpers ---------------------------------------------------------

    def _add(self, discovered: dict[str, PluginInfo], info: PluginInfo) -> bool:
        if info.id in self._bundle_ids:
            self.warnings.append(f"ignoring external plugin id {info.id!r}: reserved by the built-in bundle")
            return False
        previous = discovered.get(info.id)
        if previous is not None:
            self.warnings.append(
                f"duplicate plugin id {info.id!r}: keeping the {previous.source} source, "
                f"ignoring the {info.source} source"
            )
            return False
        discovered[info.id] = info
        return True

    def _scan_directories(self, discovered: dict[str, PluginInfo]) -> None:
        try:
            candidates = sorted(self.plugins_dir.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.errors.append(f"cannot read plugin directory {self.plugins_dir}: {exc}")
            return
        for path in candidates:
            try:
                is_directory = path.is_dir()
            except OSError:
                continue
            if not is_directory:
                continue
            if not (path / "plugin.toml").is_file():
                continue
            self._add(discovered, self._directory_info(path))

    def _directory_info(self, directory: Path) -> PluginInfo:
        """Describe one plugin package using the shared metadata parser.

        The package format (``plugin.toml`` v2 sections or legacy flat keys) is
        parsed by :mod:`deeppyro.plugins.metadata`, so discovery, the CLI and
        the boot-time bootstrap all agree on what a package declares.
        """
        plugin_id = directory.name
        toml_path = directory / "plugin.toml"
        base = PluginInfo(
            id=plugin_id,
            name=plugin_id,
            source=SOURCE_DIR,
            origin=str(toml_path),
        )
        try:
            metadata = load_metadata(directory, plugin_id=plugin_id)
        except MetadataError as exc:
            return replace(base, error=f"{exc.kind}: {exc.message}")

        info = replace(
            base,
            name=metadata.name or plugin_id,
            version=metadata.version,
            description=metadata.description,
            target=metadata.target,
            provides=metadata.provides,
        )
        if self.inject_sys_path:
            entry = str(directory)
            if entry not in sys.path:
                sys.path.insert(0, entry)
        return info

    def _scan_entry_points(self, discovered: dict[str, PluginInfo]) -> None:
        try:
            entries = sorted(self._iter_entry_points(), key=lambda entry: str(getattr(entry, "name", "")))
        except Exception as exc:  # noqa: BLE001 - one bad source must not hide the rest
            self.errors.append(f"cannot enumerate plugin entry points: {exc}")
            return
        for entry in entries:
            info = self._entry_point_info(entry)
            if self._add(discovered, info) and info.error is None and info.id:
                self._entry_point_map[info.id] = entry

    def _iter_entry_points(self) -> list[Any]:
        factory = self._entry_points_factory
        entries = factory() if factory is not None else importlib.metadata.entry_points()
        select = getattr(entries, "select", None)
        if callable(select):
            return list(select(group=ENTRY_POINT_GROUP))
        if isinstance(entries, dict):
            return list(entries.get(ENTRY_POINT_GROUP, ()))
        return [entry for entry in entries if _entry_point_group(entry) == ENTRY_POINT_GROUP]

    def _entry_point_info(self, entry: Any) -> PluginInfo:
        plugin_id = str(getattr(entry, "name", "") or "")
        value = str(getattr(entry, "value", "") or "")
        distribution = getattr(entry, "dist", None)
        version = ""
        description = ""
        origin = value
        if distribution is not None:
            version = str(getattr(distribution, "version", "") or "")
            dist_name = str(getattr(distribution, "name", None) or "")
            if dist_name:
                origin = f"{dist_name}=={version}" if version else dist_name
            try:
                summary = distribution.metadata["Summary"] or ""
            except (KeyError, TypeError):  # metadata is best effort
                summary = ""
            description = str(summary)
        error = None
        if not plugin_id:
            error = "entry point has no name"
        elif not value:
            error = f"entry point {plugin_id!r} has no value"
        return PluginInfo(
            id=plugin_id,
            name=plugin_id,
            version=version,
            source=SOURCE_ENTRY_POINT,
            origin=origin,
            description=description,
            error=error,
            target=value if ":" in value else None,
        )

    def _load_user_manifest(self) -> tuple[PluginSpec, ...]:
        try:
            return load_manifest(self.manifest_path)
        except (ManifestError, OSError) as exc:
            self.errors.append(f"cannot read user manifest {self.manifest_path}: {exc}")
            return ()

    def _scan_manifest_entries(self, specs: Iterable[PluginSpec], discovered: dict[str, PluginInfo]) -> None:
        installed_ids = set(discovered)
        for spec in specs:
            if spec.id in self._bundle_ids or spec.id in installed_ids:
                continue
            info = PluginInfo(
                id=spec.id,
                name=spec.name or spec.id,
                source=SOURCE_MANIFEST,
                origin=str(self.manifest_path),
            )
            if spec.name is None:
                info = replace(info, error="manifest entry has no 'name' and cannot be resolved")
            elif ":" not in spec.name:
                info = replace(
                    info,
                    error=f"plugin reference {spec.name!r} is not module:Class",
                    target=None,
                )
            else:
                info = replace(info, target=spec.name)
            discovered[info.id] = info


__all__ = ["ENTRY_POINT_GROUP", "PluginDiscovery"]
