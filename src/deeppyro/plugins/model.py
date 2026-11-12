"""Value objects describing external DeepPyro plugins.

An "external" plugin is any plugin that is not part of this source tree: an
installed distribution exposing a deeppyro.plugins entry point, a directory
under the user plugin directory, or an entry in the user patch manifest.
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


__all__ = [
    "SOURCE_DIR",
    "SOURCE_ENTRY_POINT",
    "SOURCE_LABELS",
    "SOURCE_MANIFEST",
    "PluginInfo",
    "PluginSource",
    "ValidationResult",
]
