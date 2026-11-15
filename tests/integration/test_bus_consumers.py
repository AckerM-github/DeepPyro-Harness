"""The bus has more than one real consumer, and they see the same dispatch.

D5 of the event alignment plan asks for at least three genuine subscribers and a
test that pins the ordering rules, so a future refactor cannot quietly make the
bus a single-consumer pipe.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from deeppyro.api.context import PluginContext
from deeppyro.builtin.commands import CommandsPlugin
from deeppyro.core.bus import EventBus
from deeppyro.hooks.service import HookService
from deeppyro.hooks.types import AGENT_STATUS


class _Stub:
    """Minimal PluginContext host that exposes nothing but the bus."""

    reflect = None

    def __init__(self, hooks: HookService) -> None:
        self._hooks = hooks

    def get(self, name: str):
        return self._hooks if name == "hooks" else None

    def on(self, *args, **kwargs):
        raise AssertionError("a catalog name must not fall through to cordis")

    def once(self, *args, **kwargs):
        raise AssertionError("a catalog name must not fall through to cordis")


def _context(hooks: HookService) -> PluginContext:
    stub = _Stub(hooks)
    stub.reflect = stub
    return PluginContext(stub, plugin_id="third-party")


@pytest.mark.asyncio
async def test_three_subscribers_see_one_dispatch_in_registration_order() -> None:
    hooks = HookService()
    seen: list[str] = []

    for label in ("first", "second", "third"):
        hooks.on(AGENT_STATUS, lambda payload, label=label: seen.append(label))

    await hooks.emit(AGENT_STATUS, {"status": "thinking"})

    assert seen == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_prepend_runs_before_earlier_registrations() -> None:
    hooks = HookService()
    seen: list[str] = []
    hooks.on(AGENT_STATUS, lambda payload: seen.append("body"))
    hooks.on(AGENT_STATUS, lambda payload: seen.append("front"), prepend=True)

    await hooks.emit(AGENT_STATUS, {"status": "thinking"})

    assert seen == ["front", "body"]


@pytest.mark.asyncio
async def test_a_scoped_subscriber_only_sees_its_own_session() -> None:
    hooks = HookService()
    seen: list[str] = []
    hooks.on(AGENT_STATUS, lambda payload: seen.append("a"), scope={"session": "a"})
    hooks.on(AGENT_STATUS, lambda payload: seen.append("b"), scope={"session": "b"})
    hooks.on(AGENT_STATUS, lambda payload: seen.append("everywhere"), global_=True)

    await hooks.emit(AGENT_STATUS, {"status": "thinking"}, scope={"session": "b"})

    assert seen == ["b", "everywhere"]


@pytest.mark.asyncio
async def test_a_plugin_subscription_and_a_service_subscription_both_fire() -> None:
    hooks = HookService()
    seen: list[str] = []
    plugin = _context(hooks)
    plugin.on(AGENT_STATUS, lambda payload: seen.append("plugin"))
    hooks.on(AGENT_STATUS, lambda payload: seen.append("service"))

    await hooks.emit(AGENT_STATUS, {"status": "thinking"})

    assert seen == ["plugin", "service"]


@pytest.mark.asyncio
async def test_an_observer_sees_invoked_then_resolved_for_every_dispatch() -> None:
    hooks = HookService()
    observations: list[tuple[str, str]] = []
    hooks.add_observer(lambda observation: observations.append((observation.kind, observation.hook)))
    hooks.on(AGENT_STATUS, lambda payload: None)

    await hooks.emit(AGENT_STATUS, {"status": "thinking"})

    assert observations == [("invoked", AGENT_STATUS), ("resolved", AGENT_STATUS)]


@pytest.mark.asyncio
async def test_a_disposed_subscriber_stops_receiving() -> None:
    hooks = HookService()
    seen: list[str] = []
    dispose = hooks.on(AGENT_STATUS, lambda payload: seen.append("kept"))
    hooks.on(AGENT_STATUS, lambda payload: seen.append("dropped"))
    await hooks.emit(AGENT_STATUS, {"status": "thinking"})
    assert seen == ["kept", "dropped"]

    hooks.on(AGENT_STATUS, lambda payload: None)  # a later registration must not revive
    dispose()
    seen.clear()
    await hooks.emit(AGENT_STATUS, {"status": "thinking"})

    assert seen == ["dropped"]


class _BusHost:
    """The minimum a CommandsPlugin needs: a bus and nothing else."""

    plugin_id = "commands"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.reflect = self

    def get(self, name: str):
        return self._bus if name == "bus" else None

    def provide(self, *args, **kwargs) -> None:
        return None

    def effect(self, *args, **kwargs) -> None:
        return None


def _commands() -> tuple[Any, EventBus, list[dict[str, Any]]]:
    bus = EventBus()
    commands = CommandsPlugin(_BusHost(bus), None)
    seen: list[dict[str, Any]] = []
    bus.on("command/executed", seen.append)
    return commands, bus, seen


async def test_the_service_path_publishes_command_executed() -> None:
    commands, _bus, seen = _commands()
    commands.add("hello-world", lambda host, argument: None, plugin_id="test")
    spec = commands.get("hello-world")
    assert spec is not None
    await commands.invoke(spec, object(), "")
    await asyncio.sleep(0)
    assert seen == [{"name": "hello-world", "ok": True, "error": "", "plugin": "test"}]


async def test_a_failing_command_publishes_the_error_kind() -> None:
    commands, _bus, seen = _commands()

    def boom(host: Any, argument: str) -> None:
        raise RuntimeError("nope")

    commands.add("boom", boom)
    spec = commands.get("boom")
    assert spec is not None
    with pytest.raises(RuntimeError):
        await commands.invoke(spec, object(), "")
    await asyncio.sleep(0)
    assert seen == [{"name": "boom", "ok": False, "error": "RuntimeError", "plugin": ""}]


async def test_an_async_command_publishes_after_it_finishes() -> None:
    commands, _bus, seen = _commands()

    async def slow(host: Any, argument: str) -> str:
        await asyncio.sleep(0)
        return "done"

    commands.add("slow", slow)
    spec = commands.get("slow")
    assert spec is not None
    assert await commands.invoke(spec, object(), "") == "done"
    await asyncio.sleep(0)
    assert seen == [{"name": "slow", "ok": True, "error": "", "plugin": ""}]


async def test_the_tui_path_publishes_too() -> None:
    # The TUI calls spec.handler directly instead of CommandsService.invoke, so
    # the wrapper has to sit on the handler for both paths to report.
    commands, _bus, seen = _commands()
    commands.add("direct", lambda host, argument: "inline")
    spec = commands.get("direct")
    assert spec is not None and spec.handler is not None
    assert spec.handler(object(), "") == "inline"
    await asyncio.sleep(0)
    assert seen == [{"name": "direct", "ok": True, "error": "", "plugin": ""}]
