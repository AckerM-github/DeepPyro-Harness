"""DeepPyro kernel: cordis (vendored) plus the plugin composition loader.

Kernel responsibilities only - no domain logic:

- deeppyro.kernel.composition - bundle files and patch rows: ordered plugin
  rows merged by id, config replaced as a whole, insert/override,
  isolate/intercept per row.
- deeppyro.kernel.app - create_app() builds a cordis Context, mounts every
  enabled row, exposes services, records non-critical failures, and disposes
  fibers in reverse order.
- deeppyro.kernel.bundle - the default composition: packaged core bundle,
  user bundles under ~/.dpro/bundles, and the user patch manifest.
- deeppyro.kernel.KernelConfig - the config service (validated deeppyro
  config) that every other plugin may inject.

Domain code stays cordis-free; plugins wrap it.
"""

from deeppyro.kernel.app import App, KernelBootError, create_app
from deeppyro.kernel.bundle import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_PLUGINS,
    boot,
    bundle_specs,
    core_bundle_path,
    default_manifest_paths,
    default_plugin_ids,
    discover_user_bundles,
    load_bundle,
    load_composition,
)
from deeppyro.kernel.composition import (
    Bundle,
    Composition,
    CompositionError,
    PluginRow,
    default_composition,
    rows_from_specs,
)
from deeppyro.kernel.config_service import KernelConfig, provide_config
from deeppyro.kernel.manifest import (
    ManifestError,
    PluginSpec,
    load_manifest,
    merge_plugins,
    resolve_plugin_name,
)

__all__ = [
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_PLUGINS",
    "App",
    "Bundle",
    "Composition",
    "CompositionError",
    "KernelBootError",
    "KernelConfig",
    "ManifestError",
    "PluginRow",
    "PluginSpec",
    "boot",
    "bundle_specs",
    "core_bundle_path",
    "create_app",
    "default_composition",
    "default_manifest_paths",
    "default_plugin_ids",
    "discover_user_bundles",
    "load_bundle",
    "load_composition",
    "load_manifest",
    "merge_plugins",
    "provide_config",
    "resolve_plugin_name",
    "rows_from_specs",
]
