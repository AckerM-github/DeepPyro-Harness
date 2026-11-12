"""External plugin management for DeepPyro.

The package only manages plugins outside this source tree: installed
distributions exposing a deeppyro.plugins entry point, plugin directories under
the user config directory, and user patch-manifest entries. The kernel bundle
plugins are read-only for this package.
"""

from __future__ import annotations

from deeppyro.plugins.discovery import ENTRY_POINT_GROUP, PluginDiscovery
from deeppyro.plugins.errors import (
    ManifestWriteError,
    PluginError,
    PluginNotFoundError,
    PluginProtectionError,
    PluginRemovalError,
)
from deeppyro.plugins.manager import PluginManager
from deeppyro.plugins.manifest_store import (
    dump_manifest,
    read_manifest,
    save_user_manifest,
)
from deeppyro.plugins.model import (
    SOURCE_DIR,
    SOURCE_ENTRY_POINT,
    SOURCE_LABELS,
    SOURCE_MANIFEST,
    PluginInfo,
    PluginSource,
    ValidationResult,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "SOURCE_DIR",
    "SOURCE_ENTRY_POINT",
    "SOURCE_LABELS",
    "SOURCE_MANIFEST",
    "ManifestWriteError",
    "PluginDiscovery",
    "PluginError",
    "PluginInfo",
    "PluginManager",
    "PluginNotFoundError",
    "PluginProtectionError",
    "PluginRemovalError",
    "PluginSource",
    "ValidationResult",
    "dump_manifest",
    "read_manifest",
    "save_user_manifest",
]
