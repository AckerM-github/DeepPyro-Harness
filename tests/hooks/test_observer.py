"""Observer tests for the runtime hook service (task-65)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from types import MappingProxyType
from typing import Any

import pytest

from deeppyro.hooks import HookAggregateError, HookObservation, HookService
from deeppyro.hooks.types import AGENT_PRE_STEP, AGENT_STATUS, HOOK_NAMES

PAYLOAD: dict[str, Any] = {"turn": 1, "step": 2}


def recorder(seen: list[HookObservation]) -> Any:
    def observe(observation: HookObservation) -> None:
        seen.append(observation)

    return observe


def kinds(seen: list[HookObservation]) -> list[str]:
    return [observation.kind for observation in seen]


def cancelled_payload() -> dict[str, Any]:
    signal = asyncio.Event()
    signal.set()
    return {"signal": signal, "turn": 1}


async def test_waterfall_emits_invoked_then_resolved() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    dispose = service.add_observer(recorder(seen))

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "fallback"
    assert kinds(seen) == ["invoked", "resolved"]

    invoked, resolved = seen
    assert invoked.hook == AGENT_PRE_STEP
    assert invoked.mode == "waterfall"
    assert invoked.scope is None
    assert invoked.result is None
    assert invoked.error is None
    assert invoked.duration_ms == 0.0
    assert isinstance(invoked.payload, MappingProxyType)
    assert invoked.payload["turn"] == 1
    assert invoked.payload is resolved.payload
    assert resolved.result == "fallback"
    assert resolved.error is None
    assert resolved.duration_ms >= 0.0
    dispose()


async def test_payload_is_frozen_before_invoked_and_shared_with_handlers() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    captured: list[Any] = []

    async def handler(payload: Any, next_step: Any) -> Any:
        captured.append(payload)
        return await next_step()

    service.on(AGENT_PRE_STEP, handler)
    service.add_observer(recorder(seen))

    payload = {"items": [1, 2], "nested": {"kept": True}}
    await service.waterfall(AGENT_PRE_STEP, payload, "fallback")

    assert isinstance(seen[0].payload, MappingProxyType)
    assert isinstance(seen[0].payload["nested"], MappingProxyType)
    assert seen[0].payload["items"] == (1, 2)
    assert captured[0] is seen[0].payload
    assert captured[0] is seen[1].payload


async def test_invoked_is_delivered_before_listeners_run() -> None:
    service = HookService()
    order: list[tuple[str, str]] = []
    service.add_observer(lambda observation: order.append(("observe", observation.kind)))
    service.on(AGENT_STATUS, lambda payload: order.append(("handler", "")))

    await service.emit(AGENT_STATUS, PAYLOAD)

    assert order == [("observe", "invoked"), ("handler", ""), ("observe", "resolved")]


async def test_serial_reports_collected_results() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    service.on(AGENT_STATUS, lambda payload: "first")
    service.on(AGENT_STATUS, lambda payload: "second")
    service.add_observer(recorder(seen))

    assert await service.serial(AGENT_STATUS, PAYLOAD) == ["first", "second"]
    assert seen[1].mode == "serial"
    assert seen[1].result == ["first", "second"]
    assert seen[1].error is None


async def test_emit_reports_no_result() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    calls: list[str] = []
    service.on(AGENT_STATUS, lambda payload: calls.append("one"))
    service.add_observer(recorder(seen))

    assert await service.emit(AGENT_STATUS, PAYLOAD) is None
    assert calls == ["one"]
    assert seen[1].mode == "emit"
    assert seen[1].result is None
    assert seen[1].error is None


async def test_bail_reports_the_bailed_value() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    service.on(AGENT_STATUS, lambda payload: "stop")
    service.add_observer(recorder(seen))

    assert await service.bail(AGENT_STATUS, PAYLOAD) == "stop"
    assert seen[1].mode == "bail"
    assert seen[1].result == "stop"


async def test_bail_without_a_decision_reports_none() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    service.add_observer(recorder(seen))

    assert await service.bail(AGENT_STATUS, PAYLOAD) is None
    assert seen[1].result is None
    assert seen[1].error is None


async def test_parallel_reports_no_result() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    calls: list[str] = []
    service.on(AGENT_STATUS, lambda payload: calls.append("one"))
    service.add_observer(recorder(seen))

    assert await service.parallel(AGENT_STATUS, PAYLOAD) is None
    assert calls == ["one"]
    assert seen[1].mode == "parallel"
    assert seen[1].result is None
    assert seen[1].error is None


async def test_parallel_reports_hook_aggregate_error() -> None:
    service = HookService()
    seen: list[HookObservation] = []

    def first(payload: Any) -> None:
        raise ValueError("first failed")

    def second(payload: Any) -> None:
        raise RuntimeError("second failed")

    service.on(AGENT_STATUS, first)
    service.on(AGENT_STATUS, second)
    service.add_observer(recorder(seen))

    with pytest.raises(HookAggregateError) as caught:
        await service.parallel(AGENT_STATUS, PAYLOAD)

    assert kinds(seen) == ["invoked", "resolved"]
    resolved = seen[1]
    assert resolved.result is None
    assert resolved.error is caught.value
    assert isinstance(resolved.error, HookAggregateError)
    assert len(resolved.error.errors) == 2


async def test_waterfall_handler_error_is_recorded_and_reraised() -> None:
    service = HookService()
    seen: list[HookObservation] = []

    async def boom(payload: Any, next_step: Any) -> None:
        raise RuntimeError("boom")

    service.on(AGENT_PRE_STEP, boom)
    service.add_observer(recorder(seen))

    with pytest.raises(RuntimeError) as caught:
        await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback")

    assert kinds(seen) == ["invoked", "resolved"]
    assert seen[1].result is None
    assert seen[1].error is caught.value


async def test_serial_handler_error_is_recorded_without_partial_result() -> None:
    service = HookService()
    seen: list[HookObservation] = []

    def first(payload: Any) -> str:
        return "first"

    def second(payload: Any) -> str:
        raise RuntimeError("second failed")

    service.on(AGENT_STATUS, first)
    service.on(AGENT_STATUS, second)
    service.add_observer(recorder(seen))

    with pytest.raises(RuntimeError):
        await service.serial(AGENT_STATUS, PAYLOAD)

    assert seen[1].result is None
    assert isinstance(seen[1].error, RuntimeError)
    assert str(seen[1].error) == "second failed"


async def test_bail_handler_error_is_recorded() -> None:
    service = HookService()
    seen: list[HookObservation] = []

    def boom(payload: Any) -> None:
        raise RuntimeError("bail failed")

    service.on(AGENT_STATUS, boom)
    service.add_observer(recorder(seen))

    with pytest.raises(RuntimeError):
        await service.bail(AGENT_STATUS, PAYLOAD)

    assert seen[1].result is None
    assert isinstance(seen[1].error, RuntimeError)


async def test_emit_isolates_handler_errors_and_reports_clean_result() -> None:
    service = HookService()
    seen: list[HookObservation] = []

    def boom(payload: Any) -> None:
        raise RuntimeError("listener failed")

    service.on(AGENT_STATUS, boom)
    service.add_observer(recorder(seen))

    assert await service.emit(AGENT_STATUS, PAYLOAD) is None
    assert kinds(seen) == ["invoked", "resolved"]
    assert seen[1].result is None
    assert seen[1].error is None


@pytest.mark.parametrize("mode", ["waterfall", "serial", "emit", "bail", "parallel"])
async def test_cancelled_dispatch_is_still_paired(mode: str) -> None:
    service = HookService()
    seen: list[HookObservation] = []
    handled: list[str] = []
    service.on(AGENT_STATUS, lambda payload: handled.append("ran"))
    service.add_observer(recorder(seen))
    payload = cancelled_payload()

    dispatch = getattr(service, mode)
    if mode == "waterfall":
        assert await dispatch(AGENT_STATUS, payload, "fallback") == "fallback"
    elif mode == "serial":
        assert await dispatch(AGENT_STATUS, payload) == []
    else:
        assert await dispatch(AGENT_STATUS, payload) is None

    assert handled == []
    assert kinds(seen) == ["invoked", "resolved"]
    expected_result = {"waterfall": "fallback", "serial": []}.get(mode)
    assert seen[1].result == expected_result
    assert seen[1].error is None


async def test_unknown_hook_is_not_observed() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    service.add_observer(recorder(seen))

    with pytest.raises(ValueError):
        await service.waterfall("not/a-known-hook", PAYLOAD)
    with pytest.raises(ValueError):
        await service.parallel("not/a-known-hook", PAYLOAD)

    assert seen == []
    assert HOOK_NAMES


async def test_observer_failure_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    service = HookService()
    received: list[HookObservation] = []

    def broken(observation: HookObservation) -> None:
        raise RuntimeError("observer down")

    service.add_observer(broken)
    service.add_observer(recorder(received))
    caplog.set_level(logging.ERROR)

    assert await service.emit(AGENT_STATUS, PAYLOAD) is None

    assert kinds(received) == ["invoked", "resolved"]
    assert "hook observer" in caplog.text


async def test_observer_failure_does_not_change_the_dispatch_result() -> None:
    service = HookService()
    service.add_observer(lambda observation: (_ for _ in ()).throw(RuntimeError("observer down")))

    async def deny(payload: Any, next_step: Any) -> str:
        return "deny"

    service.on(AGENT_PRE_STEP, deny)

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback") == "deny"


async def test_disposer_is_idempotent_and_stops_notifications() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    dispose = service.add_observer(recorder(seen))

    await service.serial(AGENT_STATUS, PAYLOAD)
    dispose()
    dispose()
    await service.serial(AGENT_STATUS, PAYLOAD)

    assert kinds(seen) == ["invoked", "resolved"]


async def test_disposing_one_observer_keeps_the_others_active() -> None:
    service = HookService()
    first: list[HookObservation] = []
    second: list[HookObservation] = []
    dispose = service.add_observer(recorder(first))
    service.add_observer(recorder(second))

    dispose()
    await service.emit(AGENT_STATUS, PAYLOAD)

    assert first == []
    assert kinds(second) == ["invoked", "resolved"]


async def test_every_observer_sees_the_same_observation_object() -> None:
    service = HookService()
    first: list[HookObservation] = []
    second: list[HookObservation] = []
    service.add_observer(recorder(first))
    service.add_observer(recorder(second))

    await service.emit(AGENT_STATUS, PAYLOAD)

    assert first == second
    assert first[0] is second[0]
    assert first[1] is second[1]


async def test_duplicate_registration_is_removed_one_disposer_at_a_time() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    observer = recorder(seen)
    first = service.add_observer(observer)
    second = service.add_observer(observer)

    await service.emit(AGENT_STATUS, PAYLOAD)
    assert len(seen) == 4

    first()
    await service.emit(AGENT_STATUS, PAYLOAD)
    assert len(seen) == 6

    second()
    second()
    await service.emit(AGENT_STATUS, PAYLOAD)
    assert len(seen) == 6


async def test_observation_carries_normalized_scope() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    service.add_observer(recorder(seen))
    scope = {"session": "session-1", "preset": "default"}

    await service.serial(AGENT_STATUS, PAYLOAD, scope=scope)
    scope["session"] = "mutated"

    assert isinstance(seen[0].scope, MappingProxyType)
    assert dict(seen[0].scope) == {"session": "session-1", "preset": "default"}
    assert seen[0].scope is seen[1].scope


def test_add_observer_requires_a_callable() -> None:
    with pytest.raises(TypeError):
        HookService().add_observer(object())  # type: ignore[arg-type]


def test_observed_is_false_without_observers() -> None:
    assert HookService().observed is False


async def test_observed_tracks_observer_registration() -> None:
    service = HookService()
    seen: list[HookObservation] = []
    dispose = service.add_observer(recorder(seen))

    assert service.observed is True

    dispose()
    assert service.observed is False
    dispose()
    assert service.observed is False


async def test_observed_stays_true_while_any_observer_remains() -> None:
    service = HookService()
    observer = recorder([])
    first = service.add_observer(observer)
    second = service.add_observer(observer)

    first()
    assert service.observed is True

    second()
    assert service.observed is False


async def test_failing_observer_does_not_clear_observed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = HookService()
    caplog.set_level(logging.ERROR)

    def failing(observation: HookObservation) -> None:
        raise RuntimeError("observer failed")

    service.add_observer(failing)

    assert service.observed is True
    assert await service.emit(AGENT_STATUS, PAYLOAD) is None
    assert service.observed is True
    assert "hook observer" in caplog.text


def test_hook_observation_is_frozen() -> None:
    observation = HookObservation(
        kind="invoked",
        hook=AGENT_STATUS,
        mode="emit",
        scope=None,
        payload={},
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        observation.kind = "resolved"  # type: ignore[misc]


def test_hook_observation_is_exported() -> None:
    from deeppyro import hooks

    assert "HookObservation" in hooks.__all__
    assert hooks.HookObservation is HookObservation
