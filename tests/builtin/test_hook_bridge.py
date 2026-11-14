"""Tests for the builtin hook-bridge plugin (task-66, P2-1)."""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.builtin.hook_bridge import HookBridgePlugin
from deeppyro.hooks import LLM_STREAM, MODEL_SELECTION, TOOL_CALL, HookService
from deeppyro.kernel.hook_bridge import EVENT_INVOKED, EVENT_RESULT, HookEventScope


class FakeCtx:
    """Minimal PluginContext: service lookup, effect bookkeeping, event capture."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.effects: list[str] = []
        self.disposers: list[Any] = []
        self.events: list[tuple[Any, Any, Any]] = []

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered")
        return self._services[name]

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        dispose = execute()
        self.effects.append(label)
        self.disposers.append(dispose)
        return dispose

    def emit(self, scope: Any, event: Any, data: Any) -> None:
        self.events.append((scope, event, data))


def make_plugin() -> tuple[HookBridgePlugin, HookService, FakeCtx]:
    hooks = HookService()
    ctx = FakeCtx({"hooks": hooks})
    plugin = HookBridgePlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, hooks, ctx


# -- registration ----------------------------------------------------------
def test_plugin_metadata() -> None:
    assert HookBridgePlugin.plugin_id == "hook-bridge"
    assert HookBridgePlugin.inject == ["hooks"]
    assert HookBridgePlugin.version == "0.1.0"
    assert HookBridgePlugin.description


def test_setup_registers_one_observer_effect() -> None:
    _plugin, _hooks, ctx = make_plugin()

    assert ctx.effects == ["hook-bridge"]
    assert len(ctx.disposers) == 1


def test_setup_requires_the_hooks_service() -> None:
    ctx = FakeCtx({})

    with pytest.raises(LookupError):
        HookBridgePlugin(ctx, None).setup(ctx, None)


# -- bridging --------------------------------------------------------------
async def test_emit_dispatch_reaches_the_cordis_event_stream() -> None:
    _plugin, hooks, ctx = make_plugin()

    await hooks.emit(TOOL_CALL, {"tool": "read"})

    assert [event for _scope, event, _data in ctx.events] == [EVENT_INVOKED, EVENT_RESULT]
    scope, _event, invoked = ctx.events[0]
    assert isinstance(scope, HookEventScope)
    assert scope.scope is None
    assert invoked == {"hook": TOOL_CALL, "mode": "emit", "tool": "read"}
    assert ctx.events[1][2]["ok"] is True
    assert ctx.events[1][2]["outcome"] == "none"


async def test_waterfall_dispatch_forwards_the_scope() -> None:
    _plugin, hooks, ctx = make_plugin()

    await hooks.waterfall(MODEL_SELECTION, {}, fallback="local", scope={"session": "session-1"})

    scope, _event, invoked = ctx.events[0]
    assert scope.scope == {"session": "session-1"}
    assert invoked["scope"] == {"session": "session-1"}
    assert ctx.events[1][2]["preview"] == "local"


async def test_hot_hooks_never_reach_the_event_stream() -> None:
    _plugin, hooks, ctx = make_plugin()

    await hooks.emit(LLM_STREAM, {"provider": "local"})

    assert ctx.events == []


async def test_disposer_unregisters_the_bridge() -> None:
    _plugin, hooks, ctx = make_plugin()

    ctx.disposers[0]()
    await hooks.emit(TOOL_CALL, {"tool": "read"})

    assert ctx.events == []
