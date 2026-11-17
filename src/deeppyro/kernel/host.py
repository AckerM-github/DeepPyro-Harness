"""Mounting and unmounting plugins on the native runtime.

The host accepts the three plugin shapes DeepPyro actually mounts:

* a host plugin dict ``{"name", "apply", "inject"}`` — what
  ``kernel.app._plugin_for`` and ``builtin.presets._row_plugin`` build for
  API-v3 :class:`deeppyro.api.Plugin` classes.  ``apply(context, config)`` may be
  a coroutine function and returns a teardown callable (or None);
* a non-API plugin class (legacy host shape) — instantiated as
  ``cls(context, config)``, ``init()`` called when present, ``stop()`` used as
  the teardown;
* a plain callable / callable object with ``apply`` — called with
  ``(context, config)``.

Injection entries are resolved through :func:`deeppyro.api.plugin.resolve_injections`,
so typed service contracts (``inject = (ToolsPlugin,)``) and plain names both end
up as the string names the fiber waiter needs.

Mounting creates the fiber, attaches it to the parent fiber's child list and
returns it; the plugin body runs when the fiber is awaited.  Unmounting is
``fiber.dispose()`` in reverse mount order (the kernel does that in
``App.stop``).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from deeppyro.kernel.fiber import Fiber

if TYPE_CHECKING:
    from deeppyro.kernel.context import RuntimeContext

log = logging.getLogger("deeppyro.kernel")


def mount(ctx: RuntimeContext, plugin: Any, config: dict[str, Any] | None = None) -> Fiber:
    """Create a fiber for one plugin and attach it to the parent fiber."""
    name, runner, inject = _normalize(plugin)
    parent = ctx.fiber
    fiber = Fiber(
        name=name,
        parent_ctx=ctx,
        parent=parent,
        config={} if config is None else config,
        inject=inject,
        runner=runner,
    )
    if parent is not None:
        parent._children.append(fiber)
    return fiber


def _normalize(plugin: Any) -> tuple[str, Callable[[Any, Any], Any], tuple[str, ...]]:
    """Turn a plugin shape into (name, async runner, inject names)."""
    if isinstance(plugin, dict):
        name = str(plugin.get("name") or "")
        apply = plugin.get("apply")
        if not callable(apply):
            raise TypeError(f"host plugin {name or '<unnamed>'!r} has no callable apply")
        inject = _resolve_injections(plugin.get("inject") or (), owner=name or "<unnamed>")
        return name or getattr(apply, "__name__", "plugin"), _dict_runner(apply), inject

    if inspect.isclass(plugin):
        name = str(getattr(plugin, "plugin_id", "") or plugin.__name__)
        inject = _resolve_injections(getattr(plugin, "inject", ()) or (), owner=name)
        return name, _class_runner(plugin), inject

    apply = getattr(plugin, "apply", None)
    if callable(apply):
        name = str(getattr(plugin, "plugin_id", "") or getattr(plugin, "name", "") or "plugin")
        return name, _apply_runner(apply), ()

    if callable(plugin):
        name = str(getattr(plugin, "__name__", "") or "plugin")
        return name, _apply_runner(plugin), ()

    raise TypeError(f"unsupported plugin object: {plugin!r}")


def _resolve_injections(raw: Any, *, owner: str) -> tuple[str, ...]:
    """Resolve typed/plain inject entries to service names for the fiber waiter."""
    from deeppyro.api.plugin import resolve_injections

    return tuple(resolve_injections(raw or (), owner=owner))


def _dict_runner(apply: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
    async def run(ctx: RuntimeContext, config: dict[str, Any]) -> Any:
        result = apply(ctx, config)
        if inspect.isawaitable(result):
            result = await result
        return result if callable(result) else None

    return run


def _apply_runner(apply: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
    async def run(ctx: RuntimeContext, config: dict[str, Any]) -> Any:
        result = apply(ctx, config)
        if inspect.isawaitable(result):
            result = await result
        return result if callable(result) else None

    return run


def _class_runner(cls: type) -> Callable[[Any, Any], Any]:
    async def run(ctx: RuntimeContext, config: dict[str, Any]) -> Any:
        instance = cls(ctx, config)
        init = getattr(instance, "init", None)
        if callable(init):
            result = init()
            if inspect.isawaitable(result):
                await result
        return _teardown(instance)

    return run


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


__all__ = ["mount"]
