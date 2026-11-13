"""Application bootstrap: compose and load plugins.

The kernel is a plugin host. Boot has four stages:

1. build the composition (core bundle, user bundles, patch manifests);
2. create the root cordis Context and register the kernel config service;
3. mount every enabled row in order, awaiting each fiber so load errors surface
   at boot instead of mid-turn;
4. expose App.ctx / App.service(name) and dispose fibers in reverse order.

Rows marked critical (the core bundle) abort boot on failure and unwind the
plugins that already started. Non-critical rows from external bundles are
recorded in App.failures and the rest of the app keeps running.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable
from typing import Any

from deeppyro._vendor.cordis_port import Context, Fiber
from deeppyro.api import API_VERSION, Plugin, PluginConfigError, PluginContext, PluginStatus
from deeppyro.config import Config, load_config
from deeppyro.kernel.composition import (
    Composition,
    CompositionError,
    PluginRow,
    default_composition,
    resolve_row,
    row_to_spec,
    rows_from_specs,
)
from deeppyro.kernel.config_service import provide_config
from deeppyro.kernel.manifest import PluginSpec, load_manifest

log = logging.getLogger("deeppyro.kernel")


class KernelBootError(RuntimeError):
    """Raised when a critical plugin fails to load during app bootstrap."""


def _validate_config(cls: type, raw: Any, row: PluginRow) -> Any:
    config_cls = getattr(cls, "Config", None)
    if config_cls is None:
        return raw if raw is not None else {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PluginConfigError(f"plugin {row.id!r} config must be a table, got {type(raw).__name__}")
    try:
        return config_cls.model_validate(raw)
    except Exception as exc:
        raise PluginConfigError(f"plugin {row.id!r} config is invalid: {exc}") from exc


def _teardown(instance: Any) -> Callable[[], Any] | None:
    stop = getattr(instance, "stop", None)
    if not callable(stop):
        return None

    def run() -> Any:
        result = stop()
        if inspect.isawaitable(result):

            async def drain() -> None:
                await result

            return drain()
        return None

    return run


def _api_adapter(cls: type, row: PluginRow) -> Callable[..., Any]:
    async def apply(raw_ctx: Any, raw_config: Any) -> Any:
        config = _validate_config(cls, raw_config, row)
        ctx = PluginContext(raw_ctx, plugin_id=row.id, config=config)
        instance = cls(ctx, config)
        setup = getattr(instance, "setup", None)
        if callable(setup):
            result = setup(ctx, config)
            if inspect.isawaitable(result):
                await result
        teardown = _teardown(instance)
        if teardown is not None:
            return teardown

        def _keep_alive() -> None:
            # No stop hook: the returned callable is the fiber's disposer and
            # also keeps the plugin instance referenced for the fiber lifetime.
            return None

        return _keep_alive

    return apply


def _check_name(row: PluginRow, cls: type) -> None:
    if not row.name:
        return
    accepted = {cls.__name__, f"{cls.__module__}:{cls.__name__}"}
    plugin_id = getattr(cls, "plugin_id", "")
    if plugin_id:
        accepted.add(plugin_id)
    if row.name not in accepted:
        raise CompositionError(
            f"plugin {row.id!r} name assertion failed: {row.name!r} does not match {type_name(cls)}"
        )


def type_name(cls: type) -> str:
    return f"{getattr(cls, '__module__', '?')}:{getattr(cls, '__name__', '?')}"


def _plugin_for(row: PluginRow) -> tuple[Any, dict[str, Any]]:
    cls = resolve_row(row)
    _check_name(row, cls)
    raw_config = dict(row.config or {})
    if inspect.isclass(cls) and issubclass(cls, Plugin):
        api_version = int(getattr(cls, "api_version", API_VERSION) or API_VERSION)
        if api_version > API_VERSION:
            raise CompositionError(
                f"plugin {row.id!r} targets api_version {api_version}, this kernel supports {API_VERSION}"
            )
        plugin: Any = {
            "name": row.id,
            "apply": _api_adapter(cls, row),
            "inject": list(getattr(cls, "inject", []) or []),
        }
        return plugin, raw_config
    return cls, raw_config


def _mount(ctx: Context, row: PluginRow, plugin: Any, config: dict[str, Any]) -> Fiber:
    mount_ctx = ctx
    for name in row.isolate:
        mount_ctx = mount_ctx.isolate(name)
    for name, overrides in row.intercept.items():
        mount_ctx = mount_ctx.intercept(name, overrides)
    return mount_ctx.plugin(plugin, config)


class App:
    """A booted DeepPyro application: root context plus loaded plugin fibers."""

    def __init__(
        self,
        ctx: Context,
        rows: Iterable[PluginRow],
        fibers: list[Fiber],
        config: Config,
    ) -> None:
        self.ctx = ctx
        self.config = config
        self._rows = tuple(rows)
        self._fibers = fibers
        #: Plugin rows that failed to load while non-critical.
        self.failures: list[PluginStatus] = []

    @property
    def rows(self) -> tuple[PluginRow, ...]:
        """Every loaded row, in mount order."""
        return self._rows

    @property
    def specs(self) -> tuple[PluginSpec, ...]:
        """Loaded rows rendered as legacy plugin specs."""
        return tuple(row_to_spec(row) for row in self._rows)

    # -- service access ----------------------------------------------------
    def service(self, name: str) -> Any:
        """Return an active service by name, or raise LookupError.

        cordis ctx.foo returns None for unknown services instead of raising,
        so we must go through ctx.reflect.get to distinguish a missing service
        from a falsy one.
        """
        value = self.ctx.reflect.get(name)
        if value is None:
            raise LookupError(f"service {name!r} is not registered or not active")
        return value

    def has_service(self, name: str) -> bool:
        return self.ctx.reflect.get(name) is not None

    @property
    def started(self) -> dict[str, str]:
        """id -> fiber state for every loaded plugin."""
        return {row.id: fiber.get_state().name for row, fiber in zip(self._rows, self._fibers)}

    # -- lifecycle ---------------------------------------------------------
    async def stop(self) -> None:
        """Dispose plugin fibers in reverse load order."""
        await _dispose_all(self._fibers)
        self._fibers.clear()


async def _dispose_all(fibers: list[Fiber]) -> None:
    for fiber in reversed(fibers):
        try:
            result = fiber.dispose()
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("plugin dispose failed")


def _resolve_config(config: Config | dict[str, Any] | None) -> Config:
    if config is None:
        return load_config()
    if isinstance(config, Config):
        return config
    return Config.model_validate(config)


def _build_composition(
    plugins: Iterable[PluginSpec],
    manifest_paths: Iterable[str],
    extra_plugins: Iterable[PluginSpec],
    bundle_paths: Iterable[str],
) -> Composition:
    plugins = tuple(plugins)
    manifest_paths = tuple(manifest_paths)
    extra_plugins = tuple(extra_plugins)
    if plugins:
        composition = Composition()
        composition.apply(rows_from_specs(plugins, origin="plugins"), critical=True)
        for path in manifest_paths:
            composition.apply(
                rows_from_specs(load_manifest(str(path)), origin=f"manifest:{path}"),
                critical=True,
            )
    else:
        composition = default_composition(
            bundle_paths=bundle_paths,
            manifest_paths=manifest_paths,
        )
    if extra_plugins:
        composition.apply(rows_from_specs(extra_plugins, origin="extra"), critical=True)
    return composition


async def create_app(
    config: Config | dict[str, Any] | None = None,
    *,
    plugins: Iterable[PluginSpec] = (),
    manifest_paths: Iterable[str] = (),
    extra_plugins: Iterable[PluginSpec] = (),
    composition: Composition | None = None,
    bundle_paths: Iterable[str] = (),
) -> App:
    """Boot a DeepPyro app.

    Args:
        config: validated config object, raw dict, or None to load defaults.
        plugins: explicit bundle-layer specs; when given they replace the core
            bundle (tests and embedders use this).
        manifest_paths: patch-layer TOML manifests applied on top.
        extra_plugins: patch entries applied last (same replacement semantics).
        composition: a pre-built composition; overrides the three above.
        bundle_paths: additional bundle directories/files to load on top of the
            core bundle before user bundles.
    """
    resolved = _resolve_config(config)
    if composition is None:
        composition = _build_composition(plugins, manifest_paths, extra_plugins, bundle_paths)

    ctx = Context()
    provide_config(ctx, resolved)

    fibers: list[Fiber] = []
    rows: list[PluginRow] = []
    failures: list[PluginStatus] = []
    try:
        for row in composition.enabled():
            try:
                plugin, plugin_config = _plugin_for(row)
                fiber = _mount(ctx, row, plugin, plugin_config)
                await fiber
            except Exception as exc:
                critical = row.critical is not False
                if not critical:
                    failures.append(
                        PluginStatus(
                            id=row.id,
                            plugin=row.plugin,
                            state="failed",
                            critical=False,
                            origin=row.origin,
                            error=str(exc),
                        )
                    )
                    log.warning("plugin %s failed to load: %s", row.id, exc)
                    continue
                if isinstance(exc, KernelBootError):
                    raise
                raise KernelBootError(
                    f"plugin {row.id!r} ({row.plugin or row.name}) failed to load: {exc}"
                ) from exc
            fibers.append(fiber)
            rows.append(row)
    except KernelBootError:
        await _dispose_all(fibers)
        raise

    app = App(ctx, rows, fibers, resolved)
    app.failures = failures
    return app


__all__ = ["App", "KernelBootError", "create_app"]
