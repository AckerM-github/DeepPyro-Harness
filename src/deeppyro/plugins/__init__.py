"""External plugin management for DeepPyro.

The package manages plugins outside this source tree: installed distributions
exposing a deeppyro.plugins entry point, plugin directories under the user
config directory, user patch-manifest entries and installable user bundles.
The kernel bundle plugins are read-only for this package.

Layer D adds the user bundle lifecycle (install/remove/enable/disable under
the deeppyro home bundles directory) and the plugin settings store that reads
and patches plugin configuration through the same user manifest.
"""

from __future__ import annotations

from deeppyro.plugins.bundles import BundleStore
from deeppyro.plugins.discovery import ENTRY_POINT_GROUP, PluginDiscovery
from deeppyro.plugins.errors import (
    ManifestWriteError,
    PluginError,
    PluginNotFoundError,
    PluginProtectionError,
    PluginRemovalError,
    SettingsConflict,
)
from deeppyro.plugins.manager import PluginManager
from deeppyro.plugins.manifest_store import (
    dump_manifest,
    read_manifest,
    save_user_manifest,
)
from deeppyro.plugins.model import (
    BUNDLE_STATE_DISABLED,
    BUNDLE_STATE_FAILED,
    BUNDLE_STATE_LOADED,
    BUNDLE_STATE_OVERRIDDEN,
    BUNDLE_STATE_REMOVED,
    BUNDLE_STATES,
    SOURCE_DIR,
    SOURCE_ENTRY_POINT,
    SOURCE_LABELS,
    SOURCE_MANIFEST,
    BundleInfo,
    PluginInfo,
    PluginSource,
    ValidationResult,
)
from deeppyro.plugins.settings_store import (
    APPLY_NEXT_BOOT,
    MASKED_VALUE,
    SettingsStore,
    manifest_revision,
    mask_secrets,
    plugin_config_schema,
    validate_config,
)

__all__ = [
    "APPLY_NEXT_BOOT",
    "BUNDLE_STATES",
    "BUNDLE_STATE_DISABLED",
    "BUNDLE_STATE_FAILED",
    "BUNDLE_STATE_LOADED",
    "BUNDLE_STATE_OVERRIDDEN",
    "BUNDLE_STATE_REMOVED",
    "ENTRY_POINT_GROUP",
    "MASKED_VALUE",
    "SOURCE_DIR",
    "SOURCE_ENTRY_POINT",
    "SOURCE_LABELS",
    "SOURCE_MANIFEST",
    "BundleInfo",
    "BundleStore",
    "ManifestWriteError",
    "PluginDiscovery",
    "PluginError",
    "PluginInfo",
    "PluginManager",
    "PluginNotFoundError",
    "PluginProtectionError",
    "PluginRemovalError",
    "PluginSource",
    "SettingsConflict",
    "SettingsStore",
    "ValidationResult",
    "dump_manifest",
    "manifest_revision",
    "mask_secrets",
    "plugin_config_schema",
    "read_manifest",
    "save_user_manifest",
    "validate_config",
]
