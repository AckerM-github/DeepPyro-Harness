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

Host-style plugins are first-class capabilities even without an API-v3
``Plugin`` class: every mount derives a
:class:`~deeppyro.api.capabilities.CapabilityManifest` (from the dict keys,
the class attributes or a synthetic host entry) and registers it with the
kernel capability registry of the scope.  Composition rows pass their manifest
explicitly because the kernel already indexed it at boot.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from deeppyro.kernel.fiber import Fiber

if TYPE_CHECKING:
    from deeppyro.api.capabilities import CapabilityManifest
    from deeppyro.kernel.context import RuntimeContext

log = logging.getLogger("deeppyro.kernel")


def mount(
    ctx: RuntimeContext,
    plugin: Any,
    config: dict[str, Any] | None = None,
    *,
    manifest: CapabilityManifest | None = None,
) -> Fiber:
    """Create a fiber for one plugin and attach it to the parent fiber.

    When ``manifest`` is given (composition rows) the kernel already indexed
    it and this function only mounts.  Host-style plugins without one derive a
    manifest from their declaration and register it with the capability
    registry, so ``ctx.capabilities`` reports them like any other plugin.
    """
    name, runner, inject = _normalize(plugin)
    if manifest is None:
        manifest = _manifest_for(plugin, name)
        _register_capability(ctx, manifest)
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


def _manifest_for(plugin: Any, name: str) -> CapabilityManifest:
    """Derive the capability manifest of a host-style plugin shape."""
    from deeppyro.api.capabilities import build_manifest

    if inspect.isclass(plugin):
        try:
            return build_manifest(plugin)
        except Exception as exc:  # noqa: BLE001 - a broken declaration must not block mounting
            log.warning("host plugin %s: cannot derive a capability manifest: %s", name, exc)
            return _fallback_manifest(name, "class")
    if isinstance(plugin, Mapping):
        try:
            return _dict_manifest(plugin, name)
        except Exception as exc:  # noqa: BLE001 - a broken declaration must not block mounting
            log.warning("host plugin %s: invalid declared capability manifest: %s", name, exc)
            return _fallback_manifest(name, "dict")
    return _fallback_manifest(name, type(plugin).__name__)


def _fallback_manifest(name: str, kind: str) -> CapabilityManifest:
    """Synthetic manifest for a plugin shape that declares nothing."""
    from deeppyro.api.capabilities import CapabilityManifest

    return CapabilityManifest(plugin_id=name or "plugin", metadata={"host-style": kind})


def _dict_manifest(plugin: Mapping[str, Any], name: str) -> CapabilityManifest:
    """Build a manifest from the capability keys of a host plugin dict."""
    from deeppyro.api.capabilities import (
        CapabilityManifest,
        RiskLevel,
        ServiceRequirement,
        tool_capability,
    )

    owner = name or "<unnamed>"
    required = _resolve_injections(plugin.get("inject") or (), owner=owner)
    raw_requirements = plugin.get("requires_versions") or {}
    requirements: tuple[ServiceRequirement, ...] = ()
    if isinstance(raw_requirements, Mapping):
        requirements = tuple(
            ServiceRequirement(service=str(service), version=str(constraint))
            for service, constraint in raw_requirements.items()
            if service
        )
    raw_tools = plugin.get("tools") or ()
    tools = ()
    if isinstance(raw_tools, Iterable) and not isinstance(raw_tools, (str, bytes)):
        tools = tuple(tool_capability(tool) for tool in raw_tools)
    risk = plugin.get("risk")
    if risk is None:
        risk_level = RiskLevel.LOW
    else:
        try:
            risk_level = RiskLevel(str(risk))
        except ValueError:
            risk_level = RiskLevel.LOW
    metadata = dict(plugin.get("metadata") or {}) if isinstance(plugin.get("metadata"), Mapping) else {}
    metadata["host-style"] = "dict"
    return CapabilityManifest(
        plugin_id=str(plugin.get("plugin_id") or owner),
        version=str(plugin.get("version") or "0.0.0"),
        services_provided=_service_names(plugin.get("provides", plugin.get("provide"))),
        services_required=required,
        tools=tools,
        hooks=_str_tuple(plugin.get("hooks")),
        events=_str_tuple(plugin.get("events")),
        permissions=_str_tuple(plugin.get("permissions")),
        sandbox=plugin.get("sandbox"),
        risk=risk_level,
        idempotent=bool(plugin.get("idempotent", True)),
        token_cost=plugin.get("token_cost"),
        context_window=plugin.get("context_window"),
        metadata=metadata,
        requires_versions=requirements,
        config_schema=_mapping(plugin.get("config_schema")),
        live_config=_str_tuple(plugin.get("live_config")),
    )


def _service_names(value: Any) -> tuple[str, ...]:
    """Normalize a declared provide list (string or iterable of strings)."""
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value if item)
    return ()


def _str_tuple(value: Any) -> tuple[str, ...]:
    """Normalize a declared string-list key."""
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value if item)
    return ()


def _mapping(value: Any) -> Mapping[str, Any] | None:
    """Normalize a declared mapping key."""
    return dict(value) if isinstance(value, Mapping) else None


def _register_capability(ctx: RuntimeContext, manifest: CapabilityManifest) -> None:
    """Register a host-style manifest once; never break mounting on failure."""
    from deeppyro.kernel.capabilities import ENTRY_RUNTIME

    try:
        registry = ctx.capabilities
        if registry is not None and any(entry.plugin_id == manifest.plugin_id for entry in registry.entries):
            # Composition rows are indexed before mounting; do not duplicate.
            return
        ctx.register_capability(
            manifest,
            plugin_id=manifest.plugin_id,
            origin="mount",
            state=ENTRY_RUNTIME,
            isolate=ctx.capability_isolate(),
        )
    except Exception:
        log.warning("host plugin %s: capability registration failed", manifest.plugin_id, exc_info=True)


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
