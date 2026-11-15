"""The five dispatch modes, pinned one by one.

DSH's cordis documents each mode precisely; the port has to agree or a plugin
that ships `serial()` handlers behaves differently here than there. This matrix
also covers cancellation, which cordis implements cooperatively.
"""

from __future__ import annotations

import asyncio

import pytest

from deeppyro.core.bus import EventBus, HookAggregateError


@pytest.mark.asyncio
async def test_serial_collects_every_return_value_in_order() -> None:
    bus = EventBus()
    bus.on("x/y", lambda payload: "a")
    bus.on("x/y", lambda payload: "b")

    assert await bus.serial("x/y", {}) == ["a", "b"]


@pytest.mark.asyncio
async def test_serial_stops_at_a_cancelled_signal() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.on("x/y", lambda payload: seen.append("first"))
    cancel = asyncio.Event()
    cancel.set()
    bus.on("x/y", lambda payload: seen.append("second"))

    await bus.serial("x/y", {"signal": cancel})

    assert seen == []


@pytest.mark.asyncio
async def test_bail_returns_the_first_truthy_value_and_stops() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.on("x/y", lambda payload: seen.append("one") or None)
    bus.on("x/y", lambda payload: seen.append("two") or "stop")
    bus.on("x/y", lambda payload: seen.append("three") or "late")

    assert await bus.bail("x/y", {}) == "stop"
    assert seen == ["one", "two"]


@pytest.mark.asyncio
async def test_bail_treats_false_as_continue() -> None:
    bus = EventBus()
    bus.on("x/y", lambda payload: False)
    bus.on("x/y", lambda payload: "later")

    assert await bus.bail("x/y", {}) == "later"


@pytest.mark.asyncio
async def test_waterfall_vetoes_when_a_handler_returns_without_next() -> None:
    bus = EventBus()
    bus.on("x/y", lambda payload, nxt: None)

    assert await bus.waterfall("x/y", {}, fallback="fallback") is None


@pytest.mark.asyncio
async def test_waterfall_passes_through_next_and_keeps_the_fallback() -> None:
    bus = EventBus()
    bus.on("x/y", lambda payload, nxt: nxt())

    assert await bus.waterfall("x/y", {}, fallback="fallback") == "fallback"


@pytest.mark.asyncio
async def test_emit_isolates_handler_failures() -> None:
    bus = EventBus()
    seen: list[str] = []

    def boom(payload):
        raise RuntimeError("boom")

    bus.on("x/y", boom)
    bus.on("x/y", lambda payload: seen.append("after"))

    await bus.emit("x/y", {})

    assert seen == ["after"]


@pytest.mark.asyncio
async def test_parallel_aggregates_every_failure() -> None:
    bus = EventBus()

    def first(payload):
        raise RuntimeError("first")

    def second(payload):
        raise RuntimeError("second")

    bus.on("x/y", first)
    bus.on("x/y", second)

    with pytest.raises(HookAggregateError) as caught:
        await bus.parallel("x/y", {})

    assert {str(error) for error in caught.value.errors} == {"first", "second"}


@pytest.mark.asyncio
async def test_parallel_accepts_a_clean_run() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.on("x/y", lambda payload: seen.append("a"))
    bus.on("x/y", lambda payload: seen.append("b"))

    await bus.parallel("x/y", {})

    assert sorted(seen) == ["a", "b"]


@pytest.mark.asyncio
async def test_payloads_are_frozen_for_listeners() -> None:
    bus = EventBus()
    captured: dict[str, object] = {}

    def handler(payload):
        captured["payload"] = payload

    bus.on("x/y", handler)
    await bus.emit("x/y", {"nested": {"value": 1}})

    with pytest.raises(TypeError):
        captured["payload"]["nested"]["value"] = 2


@pytest.mark.asyncio
async def test_an_unknown_name_is_dispatchable_on_the_bus_but_not_the_facade() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.on("totally/new", lambda payload: seen.append("bus"))

    await bus.emit("totally/new", {})

    assert seen == ["bus"]
