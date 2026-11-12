"""Bundle-layer plugin manifest: the kernel's built-in plugin list.

Mirrors DSH's layered plugin configuration:

* **bundle layer** -- this module: the ordered default plugin ids;
* **patch layer** -- optional TOML manifests (`[[plugins]]`), merged on top by
  `deeppyro.kernel.manifest.merge_plugins` exactly like DSH's
  `cordis.patch.yml`.

The service contract lives in `docs/spec/INTERFACES.md` section 11.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

from deeppyro.kernel.manifest import PluginSpec, register_alias, resolve_specs

#: Short names usable in patch manifests and by the bundle layer.
_ALIASES = {
    "frontend": "deeppyro.kernel.plugins:FrontendPlugin",
    "llm": "deeppyro.kernel.plugins:LlmPlugin",
    "sessions": "deeppyro.kernel.plugins:SessionPlugin",
    "sandbox": "deeppyro.kernel.plugins:SandboxPlugin",
    "tools": "deeppyro.kernel.plugins:ToolsPlugin",
    "agents": "deeppyro.kernel.plugins:AgentPlugin",
}
for _alias, _target in _ALIASES.items():
    register_alias(_alias, _target)

#: Ordered bundle layer. `frontend` first so later plugins may inject it.
DEFAULT_PLUGINS: tuple[PluginSpec, ...] = (
    PluginSpec(id="frontend", name="frontend"),
    PluginSpec(id="llm", name="llm"),
    PluginSpec(id="sessions", name="sessions"),
    PluginSpec(id="sandbox", name="sandbox"),
    PluginSpec(id="tools", name="tools"),
    PluginSpec(id="agents", name="agents"),
)

#: Default patch-layer manifest location (~/.config/deeppyro/plugins.toml on Linux).
DEFAULT_MANIFEST_PATH = Path(user_config_dir("deeppyro", appauthor=False, roaming=True)) / "plugins.toml"


def default_manifest_paths() -> tuple[str, ...]:
    """Patch manifests that exist on disk, in merge order."""
    if DEFAULT_MANIFEST_PATH.exists():
        return (str(DEFAULT_MANIFEST_PATH),)
    return ()


def bundle_specs(manifest_paths: tuple[str, ...] | list[str] = ()) -> list[PluginSpec]:
    """Resolve the full spec list: bundle layer + existing patch manifests."""
    return list(resolve_specs(DEFAULT_PLUGINS, manifest_paths))


async def boot(
    config: Any = None,
    *,
    manifest_paths: tuple[str, ...] | list[str] | None = None,
    extra_plugins: tuple[PluginSpec, ...] | list[PluginSpec] = (),
) -> Any:
    """Boot the kernel with the default bundle + any on-disk patch manifest."""
    from deeppyro.kernel.app import create_app

    paths = default_manifest_paths() if manifest_paths is None else manifest_paths
    return await create_app(
        config,
        plugins=DEFAULT_PLUGINS,
        manifest_paths=paths,
        extra_plugins=extra_plugins,
    )
