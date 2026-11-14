"""Scope filtering and global listener tests for HookService."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from deeppyro.hooks import HookAggregateError, HookService
from deeppyro.hooks.types import AGENT_PRE_STEP, AGENT_STATUS, HOOK_NAMES

PAYLOAD: dict[str, Any] = {"turn": 1, "step": 1}


def recorder(label: str, calls: list[str], result: Any = None) -> Any:
    """Build a handler that records its label and returns result."""

    def handler(payload: Any, *args: Any) -> Any:
        calls.append(label)
        return result

    return handler


async def test_scoped_listeners_do_not_cross_sessions() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("session-a", calls), scope={"session": "a"})
    service.on(AGENT_STATUS, recorder("session-b", calls), scope={"session": "b"})
    service.on(AGENT_STATUS, recorder("global", calls), global_=True)
    service.on(AGENT_STATUS, recorder("any", calls))

    assert await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"}) == [
        None,
        None,
        None,
    ]
    assert calls == ["session-a", "global", "any"]

    calls.clear()
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "b"})
    assert calls == ["session-b", "global", "any"]


async def test_global_listener_receives_every_dispatch() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("global", calls), scope={"agent": "lead"}, global_=True)

    await service.serial(AGENT_STATUS, PAYLOAD)
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"agent": "other"})
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"agent": "lead"})
    assert calls == ["global", "global", "global"]


async def test_unscoped_listener_receives_every_dispatch() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("any", calls))

    await service.serial(AGENT_STATUS, PAYLOAD)
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert calls == ["any", "any"]


async def test_unscoped_dispatch_skips_scoped_listeners() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("scoped", calls), scope={"session": "a"})
    service.on(AGENT_STATUS, recorder("any", calls))

    await service.serial(AGENT_STATUS, PAYLOAD)
    assert calls == ["any"]


async def test_scope_matches_when_listener_scope_is_a_subset() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(
        AGENT_STATUS,
        recorder("scoped", calls),
        scope={"session": "a", "agent": "x"},
    )

    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a", "agent": "x", "preset": "p"})
    assert calls == ["scoped"]

    calls.clear()
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert calls == []

    calls.clear()
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a", "agent": "y"})
    assert calls == []


async def test_scope_is_copied_at_registration_time() -> None:
    service = HookService()
    calls: list[str] = []
    scope = {"session": "a"}
    service.on(AGENT_STATUS, recorder("scoped", calls), scope=scope)
    scope["session"] = "b"

    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "b"})
    assert calls == ["scoped"]


async def test_empty_scope_mapping_means_unscoped() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("any", calls), scope={})

    await service.serial(AGENT_STATUS, PAYLOAD)
    assert calls == ["any"]


async def test_waterfall_filters_scope() -> None:
    service = HookService()
    calls: list[str] = []

    async def first(payload: Any, next_step: Any) -> Any:
        calls.append("a")
        return await next_step()

    async def second(payload: Any, next_step: Any) -> Any:
        calls.append("b")
        return "second"

    service.on(AGENT_PRE_STEP, first, scope={"session": "a"})
    service.on(AGENT_PRE_STEP, second, scope={"session": "b"})

    assert await service.waterfall(AGENT_PRE_STEP, PAYLOAD, "fallback", scope={"session": "a"}) == "fallback"
    assert calls == ["a"]


async def test_serial_and_emit_filter_scope() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("a", calls), scope={"session": "a"})
    service.on(AGENT_STATUS, recorder("b", calls), scope={"session": "b"})

    assert await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "b"}) == [None]
    assert calls == ["b"]

    calls.clear()
    await service.emit(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert calls == ["a"]


async def test_bail_filters_scope() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("a", calls, "stop-a"), scope={"session": "a"})
    service.on(AGENT_STATUS, recorder("b", calls, "stop-b"), scope={"session": "b"})

    assert await service.bail(AGENT_STATUS, PAYLOAD, scope={"session": "b"}) == "stop-b"
    assert calls == ["b"]


async def test_parallel_filters_scope_and_aggregates_only_matching_errors() -> None:
    service = HookService()
    seen: list[str] = []

    async def ok(payload: Any) -> None:
        await asyncio.sleep(0)
        seen.append("ok")

    def boom(payload: Any) -> None:
        seen.append("boom")
        raise RuntimeError("should not run")

    service.on(AGENT_STATUS, ok, scope={"session": "a"})
    service.on(AGENT_STATUS, boom, scope={"session": "b"})

    await service.parallel(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert seen == ["ok"]

    with pytest.raises(HookAggregateError):
        await service.parallel(AGENT_STATUS, PAYLOAD, scope={"session": "b"})


async def test_prepend_applies_after_scope_filtering() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("late", calls), scope={"session": "a"})
    service.on(AGENT_STATUS, recorder("early", calls), prepend=True, scope={"session": "a"})
    service.on(AGENT_STATUS, recorder("other", calls), prepend=True, scope={"session": "b"})

    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert calls == ["early", "late"]


async def test_once_with_scope_stays_dormant_for_other_scopes() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("once", calls), once=True, scope={"session": "a"})

    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "b"})
    assert calls == []
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert calls == ["once"]
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert calls == ["once"]


async def test_disposer_removes_scoped_listener() -> None:
    service = HookService()
    calls: list[str] = []
    dispose = service.on(AGENT_STATUS, recorder("scoped", calls), scope={"session": "a"})

    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    dispose()
    dispose()
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert calls == ["scoped"]
    assert service.has(AGENT_STATUS) is False


async def test_listeners_still_reports_all_handlers_and_respects_prepend() -> None:
    service = HookService()
    calls: list[str] = []

    def first(payload: Any) -> None:
        calls.append("first")

    def second(payload: Any) -> None:
        calls.append("second")

    service.on(AGENT_STATUS, first, scope={"session": "a"})
    service.on(AGENT_STATUS, second, prepend=True)

    assert service.listeners(AGENT_STATUS) == (second, first)
    assert service.has(AGENT_STATUS) is True


@pytest.mark.parametrize(
    "scope",
    [
        ["session", "a"],
        "session=a",
        {1: "a"},
        {"session": 1.5},
        {"session": None},
        {"session": ["a"]},
        {"session": {"nested": "a"}},
    ],
)
def test_invalid_listener_scope_raises(scope: Any) -> None:
    service = HookService()
    with pytest.raises(TypeError):
        service.on(AGENT_STATUS, lambda payload: None, scope=scope)


@pytest.mark.parametrize("scope", [["session", "a"], "session=a", {1: "a"}, {"session": 1.5}])
async def test_invalid_dispatch_scope_raises(scope: Any) -> None:
    service = HookService()
    with pytest.raises(TypeError):
        await service.serial(AGENT_STATUS, PAYLOAD, scope=scope)


async def test_unknown_hook_name_still_raises_for_scoped_on_and_dispatch() -> None:
    service = HookService()
    with pytest.raises(ValueError):
        service.on("agent/unknown", lambda payload: None, scope={"session": "a"})
    with pytest.raises(ValueError):
        await service.serial("agent/unknown", PAYLOAD, scope={"session": "a"})


async def test_global_flag_takes_precedence_over_scope() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("global", calls), scope={"agent": "lead"}, global_=True)

    await service.serial(AGENT_STATUS, PAYLOAD, scope={"agent": "other"})
    await service.serial(AGENT_STATUS, PAYLOAD)
    assert calls == ["global", "global"]


async def test_cancelled_dispatch_does_not_invoke_matching_scoped_listener() -> None:
    service = HookService()
    calls: list[str] = []
    service.on(AGENT_STATUS, recorder("scoped", calls), scope={"session": "a"})
    signal = asyncio.Event()
    signal.set()
    payload = {**PAYLOAD, "signal": signal}

    assert await service.serial(AGENT_STATUS, payload, scope={"session": "a"}) == []
    assert calls == []


async def test_payload_is_frozen_for_scoped_listener() -> None:
    service = HookService()
    seen: list[Any] = []

    def handler(payload: Any) -> None:
        seen.append(payload)
        with pytest.raises(TypeError):
            payload["turn"] = 2

    service.on(AGENT_STATUS, handler, scope={"session": "a"})
    await service.serial(AGENT_STATUS, PAYLOAD, scope={"session": "a"})
    assert seen[0]["turn"] == 1


def test_all_hook_names_accept_scope_registration() -> None:
    service = HookService()
    for name in HOOK_NAMES:
        dispose = service.on(name, lambda payload: None, scope={"session": "a"})
        assert service.has(name)
        dispose()
        assert not service.has(name)
