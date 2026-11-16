"""Removing installed packages, their manifest rows and caches."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro import paths
from deeppyro.kernel.composition import CompositionError, load_bundle
from deeppyro.plugins.bootstrap import PluginPackage, scan_plugin_packages
from deeppyro.plugins.install_commit import _manager, _manifest_bytes
from deeppyro.plugins.install_errors import InstallFailure, PreflightFailure, UnknownPlugin
from deeppyro.plugins.install_requirements import normalise_name, parse_requirement, validate_plugin_id


@dataclass(frozen=True, slots=True)
class RemoveReport:
    plugin_id: str
    dependents: tuple[str, ...]
    manifest_changed: bool


def _require_package(plugin_id: str, root: Path) -> PluginPackage:
    for package in scan_plugin_packages(root):
        if package.id == plugin_id:
            return package
    raise UnknownPlugin(plugin_id)


def dependents_of(plugin_id: str, *, root: Path | None = None) -> tuple[str, ...]:
    """Installed package ids that require ``plugin_id`` (deps or bundle requires)."""
    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    wanted = normalise_name(plugin_id)
    found: set[str] = set()
    for package in scan_plugin_packages(plugin_root):
        if package.id == plugin_id:
            continue
        if package.metadata is not None:
            for requirement in package.metadata.deps.requires:
                try:
                    name, _ = parse_requirement(requirement)
                except ValueError:
                    continue
                if normalise_name(name) == wanted:
                    found.add(package.id)
                    break
        path = package.directory / "bundle.toml"
        if not path.is_file():
            continue
        try:
            bundle = load_bundle(path)
        except (CompositionError, OSError):
            continue
        if any(requirement.id == plugin_id for requirement in bundle.requires):
            found.add(package.id)
    return tuple(sorted(found))


def _purge_caches(root: Path) -> None:
    try:
        caches = list(root.rglob("__pycache__"))
    except OSError:
        return
    for cache in caches:
        shutil.rmtree(cache, ignore_errors=True)


def _drop_manifest_row(plugin_id: str, path: Path) -> bool:
    """Delete one row from the user manifest; return whether the file changed."""
    from deeppyro.kernel.manifest import ManifestError
    from deeppyro.plugins.manifest_store import read_manifest, save_user_manifest

    try:
        specs = read_manifest(path)
    except (OSError, ManifestError):
        return False
    kept = [spec for spec in specs if spec.id != plugin_id]
    if len(kept) == len(specs):
        return False
    try:
        save_user_manifest(path, kept)
    except OSError:
        return False
    return True


def remove(
    plugin_id: str,
    *,
    root: Path | None = None,
    force: bool = False,
    manager: Any | None = None,
) -> RemoveReport:
    """Remove an installed package directory and its manifest entry."""
    from deeppyro.plugins.errors import PluginError, PluginNotFoundError

    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    manifest_path = Path(manager.manifest_path) if manager is not None else paths.plugins_manifest_path()
    try:
        package = _require_package(plugin_id, plugin_root)
    except UnknownPlugin:
        dropped = _drop_manifest_row(plugin_id, manifest_path)
        directory = plugin_root / plugin_id
        removed = False
        if validate_plugin_id(plugin_id) is None and directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
            removed = True
        if not dropped and not removed:
            raise
        _purge_caches(plugin_root)
        return RemoveReport(plugin_id, (), dropped)
    dependents = dependents_of(plugin_id, root=plugin_root)
    if dependents and not force:
        raise PreflightFailure(tuple(f"{dependent} depends on {plugin_id}" for dependent in dependents))
    managed = manager or _manager(plugin_root)
    before = _manifest_bytes(Path(managed.manifest_path))
    try:
        managed.remove(plugin_id)
    except PluginNotFoundError as exc:
        raise UnknownPlugin(plugin_id) from exc
    except PluginError as exc:
        raise InstallFailure(f"could not remove {plugin_id!r}: {exc}", rolled_back=True) from exc
    if package.directory.exists():
        shutil.rmtree(package.directory, ignore_errors=True)
    _purge_caches(plugin_root)
    after = _manifest_bytes(Path(managed.manifest_path))
    return RemoveReport(plugin_id, dependents, before != after)
