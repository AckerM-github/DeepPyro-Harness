"""DeepPyro kernel: cordis (vendored) + layered plugin manifests.

Kernel responsibilities only — no domain logic:

- :mod:`deeppyro.kernel.manifest` — plugin specs, TOML manifest layers,
  id-targeted overrides (bundle layer + profile patch layer, like DSH's
  `cordis.patch.yml`).
- :mod:`deeppyro.kernel.app` — `create_app()` boots a cordis `Context`,
  loads plugins in order, exposes services, and disposes them in reverse.
- :class:`deeppyro.kernel.KernelConfig` — the `config` service (validated
  deeppyro config) every other plugin may inject.

Domain code must stay cordis-free; plugins wrap it.
"""

from deeppyro.kernel.app import App, create_app
from deeppyro.kernel.config_service import KernelConfig, provide_config
from deeppyro.kernel.manifest import (
    ManifestError,
    PluginSpec,
    load_manifest,
    merge_plugins,
    resolve_plugin_name,
)

__all__ = [
    "App",
    "create_app",
    "KernelConfig",
    "provide_config",
    "ManifestError",
    "PluginSpec",
    "load_manifest",
    "merge_plugins",
    "resolve_plugin_name",
]
