"""DeepPyro kernel: the native plugin runtime plus the composition loader.

Kernel responsibilities only - no domain logic:

- deeppyro.kernel.container - the service container: every value is registered
  under a name (and an optional isolate scope), providers are visible while
  their fiber is active, disposal removes the registration.
- deeppyro.kernel.context - RuntimeContext, the layered handle a plugin sees:
  explicit parent pointer instead of prototype chains, isolate/intercept
  scopes, provide/get/effect/on/plugin.
- deeppyro.kernel.fiber - a mounted plugin's lifecycle (PENDING/LOADING/
  ACTIVE/FAILED/DISPOSED), its effects and child plugins; dispose is
  synchronous and idempotent.
- deeppyro.kernel.host - mount(): adapts a plugin declaration (dict, class,
  callable) to a Fiber.
- deeppyro.kernel.composition - bundle files and patch rows: ordered plugin
  rows merged by id, config replaced as a whole, insert/override,
  isolate/intercept per row, nested group rows, include files and bundle
  dependency ordering.
- deeppyro.kernel.app - create_app() builds a root RuntimeContext, mounts every
  enabled row (recursively for groups), exposes services, records non-critical
  failures, reports live row states and disposes fibers in reverse order.
- deeppyro.kernel.bundle - the default composition: packaged core bundle,
  user bundles under ~/.dpro/bundles, and the user patch manifest.
- deeppyro.kernel.runtime - the "kernel" service mounted as the first core
  row: mounted_plugins() reports live states, apply_plugin_config() applies
  volatile-only config patches to running plugins.
- deeppyro.kernel.KernelConfig - the config service (validated deeppyro
  config) that every other plugin may inject.

Domain code stays free of the runtime; plugins wrap it.
"""

from deeppyro.kernel.app import App, KernelBootError, MountRecord, create_app
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
    BundleRequirement,
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
from deeppyro.kernel.runtime import KernelRuntime

__all__ = [
    "DEFAULT_MANIFEST_PATH",
    "DEFAULT_PLUGINS",
    "App",
    "Bundle",
    "BundleRequirement",
    "Composition",
    "CompositionError",
    "KernelBootError",
    "KernelConfig",
    "KernelRuntime",
    "ManifestError",
    "MountRecord",
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
