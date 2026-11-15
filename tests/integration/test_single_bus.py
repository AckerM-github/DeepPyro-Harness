"""One registry behind two doors: the bus and the hook facade.

M1b of the alignment plan: the transport must not keep a second listener table,
otherwise `ctx.on(...)` and `hooks.on(...)` quietly mean different things.
"""

from __future__ import annotations

import pytest

from deeppyro.api.context import PluginContext
from deeppyro.builtin.hooks import HooksPlugin
from deeppyro.core.bus import EventBus
from deeppyro.hooks.service import HookService
from deeppyro.hooks.types import AGENT_STATUS, TOOL_CALL


def _context(bus: EventBus, facade: HookService) -> PluginContext:
    class Host:
        reflect = None

        def __init__(self) -> None:
            self.services = {"bus": bus, "hooks": facade}

        def get(self, name: str):
            return self.services.get(name)

        def on(self, *args, **kwargs):
            raise AssertionError("catalog names must not fall through to cordis")

        once = on

    host = Host()
    host.reflect = host
    return PluginContext(host, plugin_id="third-party")


@pytest.mark.asyncio
async def test_a_bus_subscription_is_visible_through_the_facade() -> None:
    bus = EventBus()
    hooks = HookService(bus)
    seen: list[str] = []
    bus.on(AGENT_STATUS, lambda payload: seen.append("bus"))

    assert hooks.has(AGENT_STATUS)
    await hooks.emit(AGENT_STATUS, {"status": "thinking"})

    assert seen == ["bus"]


@pytest.mark.asyncio
async def test_a_facade_subscription_is_dispatched_by_the_bus() -> None:
    bus = EventBus()
    hooks = HookService(bus)
    seen: list[str] = []
    hooks.on(AGENT_STATUS, lambda payload: seen.append("hooks"))

    await bus.emit(AGENT_STATUS, {"status": "thinking"})

    assert seen == ["hooks"]


@pytest.mark.asyncio
async def test_the_facade_keeps_the_name_policy_and_the_bus_does_not() -> None:
    bus = EventBus()
    hooks = HookService(bus)

    with pytest.raises(ValueError, match="retired from the bus"):
        hooks.on(TOOL_CALL, lambda payload: None)

    seen: list[str] = []
    bus.on("tool/call", lambda payload: seen.append("bus"))
    await bus.emit("tool/call", {"name": "read"})

    assert seen == ["bus"]


def test_hooks_plugin_provides_one_bus_and_one_facade() -> None:
    provided: dict[str, object] = {}

    class Ctx:
        def provide(self, name: str, service: object) -> None:
            provided[name] = service

    plugin = HooksPlugin(Ctx(), None)
    plugin.setup(Ctx(), None)

    assert provided["bus"] is plugin.bus
    assert provided["hooks"] is plugin.service
    assert plugin.service.bus is plugin.bus


def test_a_plugin_context_prefers_the_bus_service() -> None:
    bus = EventBus()
    hooks = HookService(bus)
    context = _context(bus, hooks)

    assert context._bus() is bus
