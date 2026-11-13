"""Value objects describing external DeepPyro plugins and installed bundles.

An "external" plugin is any plugin that is not part of this source tree: an
installed distribution exposing a deeppyro.plugins entry point, a directory
under the user plugin directory, or an entry in the user patch manifest.

A bundle is the layer-D install unit: a directory with bundle.toml and ordered
plugin rows, discovered from ~/.dpro/bundles/<id>/ after installation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: Where a plugin was discovered. The user manifest is also the layer where
#: enable/disable state is stored, so it is the fallback source for plugins
#: that are only configured there.
PluginSource = Literal["entry-point", "dir", "manifest"]

SOURCE_ENTRY_POINT: PluginSource = "entry-point"
SOURCE_DIR: PluginSource = "dir"
SOURCE_MANIFEST: PluginSource = "manifest"

#: Human readable labels used by the TUI.
SOURCE_LABELS: dict[PluginSource, str] = {
    SOURCE_ENTRY_POINT: "entry point",
    SOURCE_DIR: "插件目录",
    SOURCE_MANIFEST: "用户清单",
}


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """One externally provided plugin, as shown by the manager and the TUI.

    The target field holds the resolved module:Class string when it could be
    derived without importing the plugin module (validation fills it in for
    entry-point plugins whose value is a bare module).
    """

    id: str
    name: str
    version: str = ""
    source: PluginSource = SOURCE_MANIFEST
    origin: str = ""
    description: str = ""
    enabled: bool = False
    error: str | None = None
    provides: tuple[str, ...] = ()
    target: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Result of importing a plugin target without booting the kernel."""

    id: str
    ok: bool
    target: str | None = None
    error: str | None = None
    provides: tuple[str, ...] = ()


# -- bundle lifecycle ----------------------------------------------------

#: A bundle is structurally valid and its rows are part of the composition.
BUNDLE_STATE_LOADED = "loaded"
#: bundle.toml could not be parsed or failed API-version validation.
BUNDLE_STATE_FAILED = "failed"
#: A later bundle (higher composition order) patches one or more row ids.
BUNDLE_STATE_OVERRIDDEN = "overridden"
#: Every row of the bundle is disabled through the user manifest.
BUNDLE_STATE_DISABLED = "disabled"
#: Returned by remove(): the directory was deleted by this call.
BUNDLE_STATE_REMOVED = "removed"

#: All bundle states, for docs and validation.
BUNDLE_STATES: tuple[str, ...] = (
    BUNDLE_STATE_LOADED,
    BUNDLE_STATE_FAILED,
    BUNDLE_STATE_OVERRIDDEN,
    BUNDLE_STATE_DISABLED,
    BUNDLE_STATE_REMOVED,
)


@dataclass(frozen=True, slots=True)
class BundleInfo:
    """Snapshot of one user-installed bundle under ~/.dpro/bundles/.

    Args:
        id: Value of [bundle].id. This is also the installation directory name.
        name: Value of [bundle].name (falls back to id).
        version: Value of [bundle].version.
        api_version: Bundle API version declared in bundle.toml.
        order: Composition order (lower bundles are applied first).
        critical: Bundle-level critical flag.
        path: Absolute path of bundle.toml (or the failed directory).
        enabled: True unless every row is currently disabled.
        state: One of BUNDLE_STATES. See module constants.
        error: Parse/validation error for state failed, otherwise empty.
        row_ids: Plugin row ids declared by the bundle.
        disabled_rows: Row ids whose effective disabled flag is true.
        overridden_rows: Row ids overridden by a later-order bundle.
        restart_required: The mutation that produced this row only takes
            effect after a restart; list() always reports false.
    """

    id: str
    name: str = ""
    version: str = ""
    api_version: int = 0
    order: int = 0
    critical: bool = False
    path: str = ""
    enabled: bool = False
    state: str = BUNDLE_STATE_LOADED
    error: str = ""
    row_ids: tuple[str, ...] = ()
    disabled_rows: tuple[str, ...] = ()
    overridden_rows: tuple[str, ...] = ()
    restart_required: bool = False


__all__ = [
    "BUNDLE_STATES",
    "BUNDLE_STATE_DISABLED",
    "BUNDLE_STATE_FAILED",
    "BUNDLE_STATE_LOADED",
    "BUNDLE_STATE_OVERRIDDEN",
    "BUNDLE_STATE_REMOVED",
    "SOURCE_DIR",
    "SOURCE_ENTRY_POINT",
    "SOURCE_LABELS",
    "SOURCE_MANIFEST",
    "BundleInfo",
    "PluginInfo",
    "PluginSource",
    "ValidationResult",
]
