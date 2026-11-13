"""Unit tests for HookService waterfall, serial, and emit dispatch."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from deeppyro.hooks import HookService
from deeppyro.hooks.types import (
    AGENT_PRE_STEP,
    AGENT_STATUS,
    AGENT_TURN_STOPPING,
    HOOK_NAMES,
    TOOLS_PRE_EXECUTE,
)

PAYLOAD: dict[str, Any] = {"turn": 1, "step": 1}


async def test_waterfall_runs_handlers_in_order_and_returns_fallback() -> None:
    service = HookService()
    calls: list[str] = []

    async def first(payload: Any, next_step: Any) -> Any:
        assert payload is PAYLOAD
        calls.append("first")
        return await next_step()

    def second(payload: Any, next_step: Any) -> Any:
        calls.append("second")
        return next_step()

    service.on(AGENT_PRE_STEP, first)
    service.on(AGENT_PRE_STEP, second)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert calls == ["first", "second"]


async def test_waterfall_short_circuits_on_non_none_result() -> None:
    service = HookService()
    calls: list[str] = []
    fallback_calls: list[str] = []

    async def stop(payload: Any, next_step: Any) -> Any:
        calls.append("stop")
        return {"kind": "deny"}

    async def never(payload: Any, next_step: Any) -> Any:
        calls.append("never")
        return "never"

    def fallback() -> str:
        fallback_calls.append("fallback")
        return "fallback"

    service.on(AGENT_PRE_STEP, stop)
    service.on(AGENT_PRE_STEP, never)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, fallback) == {"kind": "deny"}
    assert calls == ["stop"]
    assert fallback_calls == []


async def test_waterfall_continues_when_handler_returns_none_without_next() -> None:
    service = HookService()
    calls: list[str] = []

    async def passthrough(payload: Any, next_step: Any) -> Any:
        calls.append("passthrough")
        return None

    async def decider(payload: Any, next_step: Any) -> Any:
        calls.append("decider")
        return "decided"

    service.on(AGENT_PRE_STEP, passthrough)
    service.on(AGENT_PRE_STEP, decider)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "decided"
    assert calls == ["passthrough", "decider"]


async def test_waterfall_returns_downstream_result_when_handler_returns_none_after_next() -> None:
    service = HookService()
    seen: list[Any] = []

    async def wrapper(payload: Any, next_step: Any) -> Any:
        downstream = await next_step()
        seen.append(downstream)
        return None

    async def decider(payload: Any, next_step: Any) -> Any:
        return "decided"

    service.on(AGENT_PRE_STEP, wrapper)
    service.on(AGENT_PRE_STEP, decider)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "decided"
    assert seen == ["decided"]


async def test_waterfall_non_none_result_after_next_overrides_chain() -> None:
    service = HookService()

    async def override(payload: Any, next_step: Any) -> Any:
        await next_step()
        return "override"

    async def decider(payload: Any, next_step: Any) -> Any:
        return "decided"

    service.on(AGENT_PRE_STEP, override)
    service.on(AGENT_PRE_STEP, decider)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "override"


async def test_prepend_puts_handler_first() -> None:
    service = HookService()
    order: list[str] = []

    async def original(payload: Any, next_step: Any) -> Any:
        order.append("original")
        return None

    async def early(payload: Any, next_step: Any) -> Any:
        order.append("early")
        return None

    service.on(AGENT_PRE_STEP, original)
    service.on(AGENT_PRE_STEP, early, prepend=True)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert order == ["early", "original"]


async def test_waterfall_next_can_only_be_called_once() -> None:
    service = HookService()

    async def twice(payload: Any, next_step: Any) -> Any:
        await next_step()
        return await next_step()

    service.on(AGENT_PRE_STEP, twice)

    with pytest.raises(RuntimeError, match="more than once"):
        await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback")


async def test_waterfall_propagates_handler_exception() -> None:
    service = HookService()
    calls: list[str] = []

    async def boom(payload: Any, next_step: Any) -> Any:
        calls.append("boom")
        raise ValueError("boom")

    async def never(payload: Any, next_step: Any) -> Any:
        calls.append("never")
        return "never"

    service.on(AGENT_PRE_STEP, boom)
    service.on(AGENT_PRE_STEP, never)

    with pytest.raises(ValueError, match="boom"):
        await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback")
    assert calls == ["boom"]


async def test_waterfall_awaits_async_fallback() -> None:
    service = HookService()

    async def fallback() -> str:
        await asyncio.sleep(0)
        return "async-fallback"

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, fallback) == "async-fallback"


async def test_waterfall_returns_plain_fallback_values() -> None:
    service = HookService()

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD) is None
    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, {"kind": "allow"}) == {"kind": "allow"}


def test_has_listeners_and_idempotent_disposer() -> None:
    service = HookService()

    async def handler(payload: Any, next_step: Any) -> Any:
        return None

    assert service.has(AGENT_PRE_STEP) is False
    assert service.has("agent/unknown") is False
    assert service.listeners(AGENT_PRE_STEP) == ()
    assert service.listeners("agent/unknown") == ()

    dispose = service.on(AGENT_PRE_STEP, handler)
    assert service.has(AGENT_PRE_STEP) is True
    assert service.listeners(AGENT_PRE_STEP) == (handler,)

    dispose()
    dispose()
    assert service.has(AGENT_PRE_STEP) is False
    assert service.listeners(AGENT_PRE_STEP) == ()


async def test_disposer_removes_only_its_own_handler() -> None:
    service = HookService()
    calls: list[str] = []

    async def first(payload: Any, next_step: Any) -> Any:
        calls.append("first")
        return None

    async def second(payload: Any, next_step: Any) -> Any:
        calls.append("second")
        return None

    dispose_first = service.on(AGENT_PRE_STEP, first)
    service.on(AGENT_PRE_STEP, second)

    dispose_first()
    await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback")
    assert calls == ["second"]


def test_on_rejects_unknown_hook_name() -> None:
    service = HookService()

    async def handler(payload: Any, next_step: Any) -> Any:
        return None

    with pytest.raises(ValueError, match="unknown hook"):
        service.on("agent/unknown", handler)


def test_on_rejects_non_callable_handler() -> None:
    service = HookService()

    with pytest.raises(TypeError, match="callable"):
        service.on(AGENT_PRE_STEP, object())  # type: ignore[arg-type]


@pytest.mark.parametrize("method", ["waterfall", "serial", "emit"])
async def test_dispatch_rejects_unknown_hook_name(method: str) -> None:
    service = HookService()

    with pytest.raises(ValueError, match="unknown hook"):
        await getattr(service, method)("agent/unknown", PAYLOAD)


async def test_serial_collects_results_without_short_circuit() -> None:
    service = HookService()
    calls: list[str] = []

    async def first(payload: Any) -> int:
        calls.append("first")
        return 1

    def second(payload: Any) -> int:
        calls.append("second")
        return 2

    async def third(payload: Any) -> int:
        calls.append("third")
        return 3

    service.on(AGENT_TURN_STOPPING, first)
    service.on(AGENT_TURN_STOPPING, second)
    service.on(AGENT_TURN_STOPPING, third)

    assert await service.serial(AGENT_TURN_STOPPING, PAYLOAD) == [1, 2, 3]
    assert calls == ["first", "second", "third"]


async def test_serial_propagates_handler_exception() -> None:
    service = HookService()
    calls: list[str] = []

    async def boom(payload: Any) -> None:
        calls.append("boom")
        raise ValueError("boom")

    async def never(payload: Any) -> None:
        calls.append("never")

    service.on(AGENT_TURN_STOPPING, boom)
    service.on(AGENT_TURN_STOPPING, never)

    with pytest.raises(ValueError, match="boom"):
        await service.serial(AGENT_TURN_STOPPING, PAYLOAD)
    assert calls == ["boom"]


async def test_emit_isolates_failures_and_calls_every_handler(caplog: pytest.LogCaptureFixture) -> None:
    service = HookService()
    calls: list[str] = []

    async def broken(payload: Any) -> None:
        calls.append("broken")
        raise RuntimeError("broken")

    async def healthy(payload: Any) -> None:
        calls.append("healthy")

    service.on(AGENT_STATUS, broken)
    service.on(AGENT_STATUS, healthy)

    with caplog.at_level(logging.ERROR, logger="deeppyro.hooks.service"):
        assert await service.emit(AGENT_STATUS, PAYLOAD) is None

    assert calls == ["broken", "healthy"]
    assert any("hook handler" in record.getMessage() for record in caplog.records)


async def test_emit_does_not_swallow_cancellation() -> None:
    service = HookService()

    async def cancel(payload: Any) -> None:
        raise asyncio.CancelledError()

    service.on(AGENT_STATUS, cancel)

    with pytest.raises(asyncio.CancelledError):
        await service.emit(AGENT_STATUS, PAYLOAD)


async def test_empty_hooks_short_circuit_cleanly() -> None:
    service = HookService()

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert await service.serial(AGENT_TURN_STOPPING, PAYLOAD) == []
    assert await service.emit(AGENT_STATUS, PAYLOAD) is None


async def test_dispatch_snapshot_ignores_handlers_registered_mid_flight() -> None:
    service = HookService()
    calls: list[str] = []
    late_calls: list[str] = []

    async def late(payload: Any, next_step: Any) -> Any:
        late_calls.append("late")
        return None

    async def first(payload: Any, next_step: Any) -> Any:
        calls.append("first")
        service.on(AGENT_PRE_STEP, late)
        return await next_step()

    service.on(AGENT_PRE_STEP, first)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert calls == ["first"]
    assert late_calls == []
    assert service.has(AGENT_PRE_STEP) is True


async def test_concurrent_dispatches_do_not_share_next_state() -> None:
    service = HookService()

    async def capture(payload: Any, next_step: Any) -> Any:
        await asyncio.sleep(0)
        return payload["value"]

    service.on(TOOLS_PRE_EXECUTE, capture)

    results = await asyncio.gather(
        service.waterfall(TOOLS_PRE_EXECUTE, {"value": 1}, "fallback"),
        service.waterfall(TOOLS_PRE_EXECUTE, {"value": 2}, "fallback"),
    )
    assert results == [1, 2]


def test_hook_names_are_fixed() -> None:
    assert HOOK_NAMES == frozenset(
        {
            "agent/pre-step",
            "agent/request",
            "agent/request-error",
            "agent/turn-stopping",
            "agent/assistant-stream",
            "agent/status",
            "tools/pre-execute",
            "tools/execute",
            "tools/post-execute",
        }
    )
