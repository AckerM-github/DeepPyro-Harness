"""Application bootstrap: create a cordis Context and load plugin specs.

`create_app()` is the kernel's only entry point. It mirrors DSH's boot order:

1. build the plugin spec list from the bundle layer (Python defaults) and
   optional patch-layer TOML manifests;
2. create the root cordis `Context` and register the kernel `config` service;
3. load each enabled plugin in manifest order (`ctx.plugin`), awaiting it so a
   load error surfaces at boot instead of mid-turn;
4. expose `App.ctx` / `App.service(name)` and dispose every fiber in reverse
   order on `stop()`.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from deeppyro._vendor.cordis_port import Context, Fiber, FiberState
from deeppyro.config import Config, load_config
from deeppyro.kernel.config_service import provide_config
from deeppyro.kernel.manifest import PluginSpec, resolve_specs


class KernelBootError(RuntimeError):
    """Raised when a plugin fails to load during app bootstrap."""


class App:
    """A booted DeepPyro application: root context plus loaded plugin fibers."""

    def __init__(
        self,
        ctx: Context,
        specs: Sequence[PluginSpec],
        fibers: list[Fiber],
        config: Config,
    ) -> None:
        self.ctx = ctx
        self.specs = tuple(specs)
        self.config = config
        self._fibers = fibers

    # -- service access ----------------------------------------------------
    def service(self, name: str) -> Any:
        """Return an active service by name, or raise `LookupError`.

        Note: cordis' `ctx.foo` returns None for unknown services instead of
        raising, so we must go through `ctx.reflect.get` to distinguish a
        missing service from a falsy one.
        """
        value = self.ctx.reflect.get(name)
        if value is None:
            raise LookupError(f"service {name!r} is not registered or not active")
        return value

    def has_service(self, name: str) -> bool:
        """Whether `name` is registered and its providing fiber is active."""
        return self.ctx.reflect.get(name) is not None

    @property
    def started(self) -> dict[str, str]:
        """`id -> state` of every loaded plugin fiber."""
        return {
            spec.id: fiber.get_state().name
            for spec, fiber in zip([s for s in self.specs if s.enabled()], self._fibers)
        }

    # -- lifecycle ---------------------------------------------------------
    async def stop(self) -> None:
        """Dispose plugin fibers in reverse load order."""
        for fiber in reversed(self._fibers):
            try:
                result = fiber.dispose()
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # pragma: no cover - teardown is best effort
                import logging

                logging.getLogger("deeppyro.kernel").exception("plugin dispose failed")
        self._fibers.clear()


async def create_app(
    config: Config | dict[str, Any] | None = None,
    *,
    plugins: Iterable[PluginSpec] = (),
    manifest_paths: Iterable[str] = (),
    extra_plugins: Iterable[PluginSpec] = (),
) -> App:
    """Boot a DeepPyro app.

    Args:
        config: validated config object, raw dict, or None to load defaults.
        plugins: bundle-layer specs (the built-in plugin list).
        manifest_paths: patch-layer TOML manifests applied on top of `plugins`.
        extra_plugins: patch entries applied after the manifest files (tests,
            embedders); same merge semantics.
    """
    resolved: Config
    if config is None:
        resolved = load_config()
    elif isinstance(config, Config):
        resolved = config
    else:
        resolved = Config.model_validate(config)

    specs = list(resolve_specs(plugins, manifest_paths))
    if extra_plugins:
        from deeppyro.kernel.manifest import merge_plugins

        specs = list(merge_plugins(specs, extra_plugins))

    ctx = Context()
    provide_config(ctx, resolved)

    fibers: list[Fiber] = []
    for spec in specs:
        if not spec.enabled():
            continue
        plugin = spec.resolve()
        fiber = ctx.plugin(plugin, spec.config or {})
        try:
            await fiber
        except Exception as exc:
            state = fiber.get_state().name
            raise KernelBootError(
                f"plugin {spec.id!r} ({spec.name}) failed to load: state={state}: {exc}"
            ) from exc
        fibers.append(fiber)

    return App(ctx, specs, fibers, resolved)
