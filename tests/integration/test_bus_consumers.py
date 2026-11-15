"""The bus has more than one real consumer, and they see the same dispatch.

D5 of the event alignment plan asks for at least three genuine subscribers and a
test that pins the ordering rules, so a future refactor cannot quietly make the
bus a single-consumer pipe.
"""

from __future__ import annotations

import pytest

from deeppyro.api.context import PluginContext
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
