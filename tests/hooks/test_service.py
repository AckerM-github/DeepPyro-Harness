"""Unit tests for HookService dispatch semantics."""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from typing import Any

import pytest

from deeppyro.hooks import HookAggregateError, HookService
from deeppyro.hooks.types import (
    AGENT_PRE_STEP,
    AGENT_STATUS,
    AGENT_TURN_STOPPING,
    HOOK_NAMES,
    TOOLS_PRE_EXECUTE,
)

PAYLOAD: dict[str, Any] = {
    "turn": 1,
    "step": 1,
    "modelSelection": {"provider": "fake-provider", "model": "test-model"},
    "items": [1, 2],
}


def cancelled_signal() -> asyncio.Event:
    """Return an already-set cancellation signal."""
    signal = asyncio.Event()
    signal.set()
    return signal


# -- waterfall -------------------------------------------------------------


async def test_waterfall_runs_handlers_in_order_and_returns_fallback() -> None:
    service = HookService()
    calls: list[str] = []
    seen: list[Any] = []

    async def first(payload: Any, next_step: Any) -> Any:
        assert isinstance(payload, MappingProxyType)
        assert payload is not PAYLOAD
        seen.append(payload)
        calls.append("first")
        return await next_step()

    def second(payload: Any, next_step: Any) -> Any:
        seen.append(payload)
        calls.append("second")
        return next_step()

    service.on(AGENT_PRE_STEP, first)
    service.on(AGENT_PRE_STEP, second)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert calls == ["first", "second"]
    assert seen[0] is seen[1]


async def test_waterfall_veto_skips_downstream_and_fallback() -> None:
    service = HookService()
    calls: list[str] = []
    fallback_calls: list[str] = []

    async def veto(payload: Any, next_step: Any) -> None:
        calls.append("veto")

    async def never(payload: Any, next_step: Any) -> Any:
        calls.append("never")
        return {"kind": "never"}

    service.on(AGENT_PRE_STEP, veto)
    service.on(AGENT_PRE_STEP, never)

    result = await service.waterfall(AGENT_PRE_STEP, PAYLOAD, lambda: fallback_calls.append("fallback"))
    assert result is None
    assert calls == ["veto"]
    assert fallback_calls == []


async def test_waterfall_veto_on_last_listener_skips_fallback() -> None:
    service = HookService()
    fallback_calls: list[str] = []

    async def veto(payload: Any, next_step: Any) -> None:
        return None

    service.on(AGENT_PRE_STEP, veto)

    result = await service.waterfall(AGENT_PRE_STEP, PAYLOAD, lambda: fallback_calls.append("fallback"))
    assert result is None
    assert fallback_calls == []


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


async def test_waterfall_explicit_next_adopts_downstream_veto() -> None:
    service = HookService()
    fallback_calls: list[str] = []

    async def wrapper(payload: Any, next_step: Any) -> Any:
        return await next_step()

    async def veto(payload: Any, next_step: Any) -> None:
        return None

    service.on(AGENT_PRE_STEP, wrapper)
    service.on(AGENT_PRE_STEP, veto)

    result = await service.waterfall(AGENT_PRE_STEP, PAYLOAD, lambda: fallback_calls.append("fallback"))
    assert result is None
    assert fallback_calls == []


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
        return await next_step()

    async def early(payload: Any, next_step: Any) -> Any:
        order.append("early")
        return await next_step()

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


# -- registration ----------------------------------------------------------


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
        return await next_step()

    async def second(payload: Any, next_step: Any) -> Any:
        calls.append("second")
        return None

    dispose_first = service.on(AGENT_PRE_STEP, first)
    service.on(AGENT_PRE_STEP, second)

    dispose_first()
    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") is None
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


async def test_once_listener_is_removed_before_it_runs() -> None:
    service = HookService()
    calls: list[str] = []

    async def handler(payload: Any, next_step: Any) -> Any:
        assert service.has(AGENT_PRE_STEP) is False
        calls.append("handler")
        return {"kind": "handled"}

    dispose = service.on(AGENT_PRE_STEP, handler, once=True)
    assert service.has(AGENT_PRE_STEP) is True

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == {"kind": "handled"}
    assert calls == ["handler"]
    assert service.has(AGENT_PRE_STEP) is False

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert calls == ["handler"]

    dispose()
    dispose()


async def test_once_listener_disposer_stays_idempotent() -> None:
    service = HookService()
    calls: list[str] = []

    async def handler(payload: Any, next_step: Any) -> Any:
        calls.append("handler")
        return await next_step()

    dispose = service.on(AGENT_PRE_STEP, handler, once=True)
    dispose()
    dispose()

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert calls == []


# -- serial ----------------------------------------------------------------


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


# -- emit ------------------------------------------------------------------


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


# -- bail ------------------------------------------------------------------


async def test_bail_stops_at_first_bailed_value() -> None:
    service = HookService()
    calls: list[str] = []

    async def first(payload: Any) -> None:
        calls.append("first")

    def second(payload: Any) -> int:
        calls.append("second")
        return 0

    async def third(payload: Any) -> str:
        calls.append("third")
        return "third"

    service.on(AGENT_PRE_STEP, first)
    service.on(AGENT_PRE_STEP, second)
    service.on(AGENT_PRE_STEP, third)

    assert await service.bail(AGENT_PRE_STEP, PAYLOAD) == 0
    assert calls == ["first", "second"]


async def test_bail_ignores_none_and_false_results() -> None:
    service = HookService()
    calls: list[str] = []

    async def first(payload: Any) -> None:
        calls.append("first")

    def second(payload: Any) -> bool:
        calls.append("second")
        return False

    async def final(payload: Any) -> dict[str, Any]:
        calls.append("final")
        return {"kind": "decide"}

    service.on(AGENT_PRE_STEP, first)
    service.on(AGENT_PRE_STEP, second)
    service.on(AGENT_PRE_STEP, final)

    assert await service.bail(AGENT_PRE_STEP, PAYLOAD) == {"kind": "decide"}
    assert calls == ["first", "second", "final"]


async def test_bail_propagates_handler_exception() -> None:
    service = HookService()
    calls: list[str] = []

    async def boom(payload: Any) -> None:
        calls.append("boom")
        raise ValueError("boom")

    async def never(payload: Any) -> None:
        calls.append("never")

    service.on(AGENT_PRE_STEP, boom)
    service.on(AGENT_PRE_STEP, never)

    with pytest.raises(ValueError, match="boom"):
        await service.bail(AGENT_PRE_STEP, PAYLOAD)
    assert calls == ["boom"]


async def test_bail_returns_none_without_listeners() -> None:
    service = HookService()

    assert await service.bail(AGENT_PRE_STEP, PAYLOAD) is None


# -- parallel --------------------------------------------------------------


async def test_parallel_runs_every_handler_concurrently() -> None:
    service = HookService()
    started: list[str] = []
    release = asyncio.Event()

    async def waiter(payload: Any) -> None:
        started.append("waiter")
        await release.wait()

    async def trigger(payload: Any) -> None:
        while "waiter" not in started:
            await asyncio.sleep(0)
        started.append("trigger")
        release.set()

    service.on(AGENT_STATUS, waiter)
    service.on(AGENT_STATUS, trigger)

    assert await service.parallel(AGENT_STATUS, PAYLOAD) is None
    assert started == ["waiter", "trigger"]


async def test_parallel_aggregates_failures() -> None:
    service = HookService()

    async def value_error(payload: Any) -> None:
        raise ValueError("a")

    def runtime_error(payload: Any) -> None:
        raise RuntimeError("b")

    async def healthy(payload: Any) -> str:
        return "ok"

    service.on(AGENT_STATUS, value_error)
    service.on(AGENT_STATUS, runtime_error)
    service.on(AGENT_STATUS, healthy)

    with pytest.raises(HookAggregateError) as info:
        await service.parallel(AGENT_STATUS, PAYLOAD)

    assert info.value.hook == AGENT_STATUS
    assert len(info.value.errors) == 2
    assert isinstance(info.value.errors[0], ValueError)
    assert isinstance(info.value.errors[1], RuntimeError)
    assert info.value.__cause__ is info.value.errors[0]
    assert AGENT_STATUS in str(info.value)


async def test_parallel_returns_none_without_listeners() -> None:
    service = HookService()

    assert await service.parallel(AGENT_STATUS, PAYLOAD) is None


async def test_unknown_dispatch_names() -> None:
    service = HookService()

    for method in ("waterfall", "serial", "emit", "bail", "parallel"):
        with pytest.raises(ValueError, match="unknown hook"):
            await getattr(service, method)("agent/unknown", PAYLOAD)


# -- payload freezing ------------------------------------------------------


async def test_waterfall_payload_is_recursively_frozen() -> None:
    service = HookService()
    seen: list[Any] = []

    async def handler(payload: Any, next_step: Any) -> Any:
        seen.append(payload)
        return await next_step()

    service.on(AGENT_PRE_STEP, handler)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    frozen = seen[0]
    assert isinstance(frozen, MappingProxyType)
    assert frozen["turn"] == 1
    assert isinstance(frozen["modelSelection"], MappingProxyType)
    assert frozen["modelSelection"] == {"provider": "fake-provider", "model": "test-model"}
    assert isinstance(frozen["items"], tuple)
    assert frozen["items"] == (1, 2)


async def test_waterfall_payload_rejects_writes() -> None:
    service = HookService()

    async def handler(payload: Any, next_step: Any) -> Any:
        payload["turn"] = 2
        return None

    service.on(AGENT_PRE_STEP, handler)

    with pytest.raises(TypeError):
        await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback")


async def test_serial_payload_rejects_writes() -> None:
    service = HookService()

    async def handler(payload: Any) -> None:
        payload["turn"] = 2

    service.on(AGENT_TURN_STOPPING, handler)

    with pytest.raises(TypeError):
        await service.serial(AGENT_TURN_STOPPING, PAYLOAD)


async def test_emit_payload_rejects_writes_without_breaking_the_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = HookService()
    calls: list[str] = []

    async def writer(payload: Any) -> None:
        calls.append("writer")
        payload["turn"] = 2

    async def healthy(payload: Any) -> None:
        calls.append("healthy")

    service.on(AGENT_STATUS, writer)
    service.on(AGENT_STATUS, healthy)

    with caplog.at_level(logging.ERROR, logger="deeppyro.hooks.service"):
        assert await service.emit(AGENT_STATUS, PAYLOAD) is None

    assert calls == ["writer", "healthy"]
    assert any("hook handler" in record.getMessage() for record in caplog.records)


async def test_bail_payload_rejects_writes() -> None:
    service = HookService()

    async def handler(payload: Any) -> None:
        payload["turn"] = 2

    service.on(AGENT_PRE_STEP, handler)

    with pytest.raises(TypeError):
        await service.bail(AGENT_PRE_STEP, PAYLOAD)


async def test_parallel_payload_rejects_writes_as_aggregate() -> None:
    service = HookService()

    async def handler(payload: Any) -> None:
        payload["turn"] = 2

    service.on(AGENT_STATUS, handler)

    with pytest.raises(HookAggregateError) as info:
        await service.parallel(AGENT_STATUS, PAYLOAD)
    assert len(info.value.errors) == 1
    assert isinstance(info.value.errors[0], TypeError)


async def test_payload_keeps_custom_objects_by_reference() -> None:
    service = HookService()
    agent = object()
    signal = asyncio.Event()
    seen: dict[str, Any] = {}

    async def handler(payload: Any, next_step: Any) -> Any:
        seen["agent"] = payload["agent"]
        seen["signal"] = payload["signal"]
        seen["nested"] = payload["nested"]
        return await next_step()

    service.on(AGENT_PRE_STEP, handler)
    payload = {"agent": agent, "signal": signal, "nested": [{"value": 1}, agent]}

    assert await service.waterfall(AGENT_PRE_STEP, payload, "fallback") == "fallback"
    assert seen["agent"] is agent
    assert seen["signal"] is signal
    assert isinstance(seen["nested"], tuple)
    assert seen["nested"][1] is agent
    assert isinstance(seen["nested"][0], MappingProxyType)


async def test_fallback_result_is_not_frozen() -> None:
    service = HookService()

    def fallback() -> dict[str, Any]:
        return {"kind": "allow", "meta": {"value": 1}}

    result = await service.waterfall(AGENT_PRE_STEP, PAYLOAD, fallback)

    assert isinstance(result, dict)
    result["meta"]["value"] = 2
    assert result == {"kind": "allow", "meta": {"value": 2}}


# -- cancellation ----------------------------------------------------------


async def test_waterfall_returns_fallback_when_signal_already_set() -> None:
    service = HookService()
    calls: list[str] = []

    async def handler(payload: Any, next_step: Any) -> Any:
        calls.append("handler")
        return await next_step()

    service.on(AGENT_PRE_STEP, handler)

    signal = cancelled_signal()
    assert await service.waterfall(AGENT_PRE_STEP, {"turn": 1, "signal": signal}, "fallback") == "fallback"
    assert calls == []


async def test_waterfall_stops_downstream_after_signal_is_set_mid_chain() -> None:
    service = HookService()
    calls: list[str] = []
    fallback_calls: list[str] = []
    signal = asyncio.Event()

    async def first(payload: Any, next_step: Any) -> Any:
        calls.append("first")
        signal.set()
        return await next_step()

    async def second(payload: Any, next_step: Any) -> Any:
        calls.append("second")
        return "second"

    def fallback() -> str:
        fallback_calls.append("fallback")
        return "fallback"

    service.on(AGENT_PRE_STEP, first)
    service.on(AGENT_PRE_STEP, second)

    assert await service.waterfall(AGENT_PRE_STEP, {"turn": 1, "signal": signal}, fallback) == "fallback"
    assert calls == ["first"]
    assert fallback_calls == ["fallback"]


async def test_serial_returns_collected_results_when_signal_is_set_mid_chain() -> None:
    service = HookService()
    calls: list[str] = []
    signal = asyncio.Event()

    async def first(payload: Any) -> int:
        calls.append("first")
        signal.set()
        return 1

    async def second(payload: Any) -> int:
        calls.append("second")
        return 2

    service.on(AGENT_TURN_STOPPING, first)
    service.on(AGENT_TURN_STOPPING, second)

    assert await service.serial(AGENT_TURN_STOPPING, {"turn": 1, "signal": signal}) == [1]
    assert calls == ["first"]


async def test_emit_stops_when_signal_is_set_mid_chain() -> None:
    service = HookService()
    calls: list[str] = []
    signal = asyncio.Event()

    async def first(payload: Any) -> None:
        calls.append("first")
        signal.set()

    async def second(payload: Any) -> None:
        calls.append("second")

    service.on(AGENT_STATUS, first)
    service.on(AGENT_STATUS, second)

    assert await service.emit(AGENT_STATUS, {"turn": 1, "signal": signal}) is None
    assert calls == ["first"]


async def test_bail_stops_when_signal_is_set_mid_chain() -> None:
    service = HookService()
    calls: list[str] = []
    signal = asyncio.Event()

    async def first(payload: Any) -> None:
        calls.append("first")
        signal.set()

    async def second(payload: Any) -> dict[str, Any]:
        calls.append("second")
        return {"kind": "deny"}

    service.on(AGENT_PRE_STEP, first)
    service.on(AGENT_PRE_STEP, second)

    assert await service.bail(AGENT_PRE_STEP, {"turn": 1, "signal": signal}) is None
    assert calls == ["first"]


async def test_parallel_does_not_dispatch_when_signal_already_set() -> None:
    service = HookService()
    calls: list[str] = []

    async def handler(payload: Any) -> None:
        calls.append("handler")

    service.on(AGENT_STATUS, handler)

    assert await service.parallel(AGENT_STATUS, {"turn": 1, "signal": cancelled_signal()}) is None
    assert calls == []


async def test_signal_without_is_set_is_ignored() -> None:
    service = HookService()
    calls: list[str] = []

    async def handler(payload: Any, next_step: Any) -> Any:
        calls.append("handler")
        return await next_step()

    service.on(AGENT_PRE_STEP, handler)

    assert await service.waterfall(AGENT_PRE_STEP, {"turn": 1, "signal": object()}, "fallback") == "fallback"
    assert calls == ["handler"]


# -- dispatch bookkeeping --------------------------------------------------


async def test_empty_hooks_short_circuit_cleanly() -> None:
    service = HookService()

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert await service.serial(AGENT_TURN_STOPPING, PAYLOAD) == []
    assert await service.emit(AGENT_STATUS, PAYLOAD) is None
    assert await service.bail(AGENT_PRE_STEP, PAYLOAD) is None
    assert await service.parallel(AGENT_STATUS, PAYLOAD) is None


async def test_dispatch_snapshot_ignores_handlers_registered_mid_flight() -> None:
    service = HookService()
    calls: list[str] = []
    late_calls: list[str] = []

    async def late(payload: Any, next_step: Any) -> Any:
        late_calls.append("late")
        return await next_step()

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
            "system-prompt/assemble",
            "llm/stream",
            "model/selection",
            "session/created",
            "session/disposed",
            "agent/error",
            "tools/pre-execute",
            "tools/execute",
            "tools/post-execute",
            "tool/call",
            "approval/request",
            "approval/asked",
            "approval/decided",
        }
    )
