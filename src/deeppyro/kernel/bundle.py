"""The default bundle: core plugins, user bundles, and patch manifests.

The installed package ships one bundle (deeppyro/bundles/core/bundle.toml).
Users add bundles under ~/.dpro/bundles/<id>/bundle.toml and patch the
composition through ~/.dpro/plugins.toml. Everything is a plugin row, so a
user bundle can add, override, disable or reorder core capabilities without a
code change.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from deeppyro.config import Config
from deeppyro.kernel.app import App, create_app
from deeppyro.kernel.composition import (
    Composition,
    CompositionError,
    PluginRow,
    core_bundle_path,
    default_composition,
    discover_user_bundles,
    load_bundle,
    row_to_spec,
    rows_from_specs,
)
from deeppyro.kernel.manifest import PluginSpec
from deeppyro.paths import plugins_manifest_path


def _core_rows() -> tuple[PluginRow, ...]:
    return load_bundle(core_bundle_path()).rows


#: Rows of the core bundle, exposed as legacy specs for tooling that lists
#: built-in plugin ids (plugin manager protection, docs).
DEFAULT_PLUGINS: tuple[PluginSpec, ...] = tuple(row_to_spec(row) for row in _core_rows())

#: The user patch manifest location (kept for compatibility with older code).
DEFAULT_MANIFEST_PATH = plugins_manifest_path()


def default_manifest_paths() -> tuple[str, ...]:
    """The user manifest, when it exists."""
    path = plugins_manifest_path()
    return (str(path),) if path.is_file() else ()


def default_plugin_ids() -> tuple[str, ...]:
    return tuple(spec.id for spec in DEFAULT_PLUGINS)


def bundle_specs(manifest_paths: Iterable[str] | None = None) -> list[PluginSpec]:
    """The effective spec list: core rows, user bundles, patch manifests."""
    paths = default_manifest_paths() if manifest_paths is None else tuple(manifest_paths)
    composition = default_composition(manifest_paths=paths)
    return [row_to_spec(row) for row in composition.rows()]


def load_composition(
    *,
    manifest_paths: Iterable[str] | None = None,
    bundle_paths: Iterable[str] = (),
) -> Composition:
    """Build the composition boot() would use."""
    paths = default_manifest_paths() if manifest_paths is None else tuple(manifest_paths)
    return default_composition(bundle_paths=bundle_paths, manifest_paths=paths)


async def boot(
    config: Config | dict[str, Any] | None = None,
    *,
    manifest_paths: Iterable[str] | None = None,
    extra_plugins: Iterable[PluginSpec] = (),
    bundle_paths: Iterable[str] = (),
) -> App:
    """Boot the default application (core bundle plus user additions)."""
    paths = default_manifest_paths() if manifest_paths is None else tuple(manifest_paths)
    return await create_app(
        config,
        manifest_paths=paths,
        extra_plugins=extra_plugins,
        bundle_paths=bundle_paths,
    )


__all__ = [
    "DEFAULT_PLUGINS",
    "Composition",
    "CompositionError",
    "PluginRow",
    "boot",
    "bundle_specs",
    "core_bundle_path",
    "default_composition",
    "default_manifest_paths",
    "default_plugin_ids",
    "discover_user_bundles",
    "load_bundle",
    "load_composition",
    "rows_from_specs",
]
