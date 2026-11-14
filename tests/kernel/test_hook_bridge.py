"""Tests for the kernel hook bridge (task-66, P2-1)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from deeppyro.hooks import AGENT_PRE_STEP, MODEL_SELECTION, TOOL_CALL, HookObservation, HookService
from deeppyro.hooks.records import HOT_HOOKS
from deeppyro.kernel.hook_bridge import EVENT_INVOKED, EVENT_RESULT, HookBridge, HookEventScope


class RecordingEmit:
    """Stand-in for ctx.emit that records the this-argument, event, and data."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, Any]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


def make_observation(
    *,
    kind: str = "invoked",
    hook: str = AGENT_PRE_STEP,
    mode: str = "serial",
    scope: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    result: Any = None,
    error: BaseException | None = None,
    duration_ms: float = 0.0,
) -> HookObservation:
    return HookObservation(
        kind=kind,
        hook=hook,
        mode=mode,
        scope=scope,
        payload=dict(payload or {}),
        result=result,
        error=error,
        duration_ms=duration_ms,
    )


# -- event names -----------------------------------------------------------
def test_bridged_event_names_match_the_plan() -> None:
    assert EVENT_INVOKED == "hook/invoked"
    assert EVENT_RESULT == "hook/result"


# -- record shapes ---------------------------------------------------------
def test_invoked_observation_emits_invoked_record() -> None:
    emit = RecordingEmit()
    bridge = HookBridge(emit)

    bridge(
        make_observation(
            kind="invoked",
            hook=TOOL_CALL,
            mode="emit",
            scope={"session": "session-1"},
            payload={"tool": "read", "turn": 2, "call": {"callId": "call-9"}, "frame": object()},
        )
    )

    assert len(emit.calls) == 1
    scope, event, data = emit.calls[0]
    assert isinstance(scope, HookEventScope)
    assert event == EVENT_INVOKED
    assert data == {
        "hook": TOOL_CALL,
        "mode": "emit",
        "scope": {"session": "session-1"},
        "turn": 2,
        "tool": "read",
        "callId": "call-9",
    }


def test_resolved_observation_emits_result_record_for_a_value() -> None:
    emit = RecordingEmit()
    bridge = HookBridge(emit)

    bridge(
        make_observation(
            kind="resolved",
            hook=MODEL_SELECTION,
            mode="waterfall",
            result={"provider": "local"},
            duration_ms=3.7,
        )
    )

    _scope, event, data = emit.calls[0]
    assert event == EVENT_RESULT
    assert data["hook"] == MODEL_SELECTION
    assert data["mode"] == "waterfall"
    assert data["ok"] is True
    assert data["outcome"] == "value"
    assert data["durationMs"] == 3
    assert data["valueType"] == "dict"
    assert data["preview"] == '{"provider": "local"}'


def test_resolved_observation_emits_error_record() -> None:
    emit = RecordingEmit()
    bridge = HookBridge(emit)

    bridge(
        make_observation(
            kind="resolved",
            hook=AGENT_PRE_STEP,
            mode="waterfall",
            error=RuntimeError("boom"),
        )
    )

    _scope, event, data = emit.calls[0]
    assert event == EVENT_RESULT
    assert data["ok"] is False
    assert data["outcome"] == "error"
    assert data["error"] == {"name": "RuntimeError", "message": "boom"}
    assert "preview" not in data


def test_resolved_observation_without_a_result_reports_none() -> None:
    emit = RecordingEmit()
    bridge = HookBridge(emit)

    bridge(make_observation(kind="resolved", hook=TOOL_CALL, mode="emit"))

    _scope, _event, data = emit.calls[0]
    assert data["ok"] is True
    assert data["outcome"] == "none"
    assert "preview" not in data
    assert "valueType" not in data


# -- filtering -------------------------------------------------------------
@pytest.mark.parametrize("hook", sorted(HOT_HOOKS))
@pytest.mark.parametrize("kind", ["invoked", "resolved"])
def test_hot_hooks_are_not_bridged(hook: str, kind: str) -> None:
    emit = RecordingEmit()
    bridge = HookBridge(emit)

    bridge(make_observation(kind=kind, hook=hook))

    assert emit.calls == []


def test_unknown_observation_kind_is_ignored() -> None:
    emit = RecordingEmit()
    bridge = HookBridge(emit)

    bridge(make_observation(kind="started"))

    assert emit.calls == []


def test_event_scope_exposes_the_dispatch_scope() -> None:
    scope = HookEventScope({"session": "session-1", "preset": "default"})

    assert scope.scope == {"session": "session-1", "preset": "default"}
    assert scope.__filter__(object()) is True
    assert "session-1" in repr(scope)


def test_event_scope_without_a_dispatch_scope_is_none() -> None:
    assert HookEventScope().scope is None
    assert HookEventScope(None).scope is None


def test_bridge_requires_a_callable_emit() -> None:
    with pytest.raises(TypeError):
        HookBridge(None)


# -- real dispatch through HookService -------------------------------------
async def test_bridge_observes_a_real_waterfall_dispatch() -> None:
    hooks = HookService()
    emit = RecordingEmit()
    bridge = HookBridge(emit)
    dispose = hooks.add_observer(bridge)

    await hooks.waterfall(MODEL_SELECTION, {}, fallback="fallback", scope={"session": "session-1"})

    assert [call[1] for call in emit.calls] == [EVENT_INVOKED, EVENT_RESULT]
    invoked_scope, _event, invoked = emit.calls[0]
    assert isinstance(invoked_scope, HookEventScope)
    assert invoked_scope.scope == {"session": "session-1"}
    assert invoked == {"hook": MODEL_SELECTION, "mode": "waterfall", "scope": {"session": "session-1"}}
    _scope, _event, resolved = emit.calls[1]
    assert resolved["ok"] is True
    assert resolved["outcome"] == "value"
    assert resolved["preview"] == "fallback"

    dispose()
    await hooks.waterfall(MODEL_SELECTION, {}, fallback="fallback")
    assert len(emit.calls) == 2


async def test_bridge_observes_a_real_emit_dispatch() -> None:
    hooks = HookService()
    emit = RecordingEmit()
    hooks.add_observer(HookBridge(emit))

    await hooks.emit(TOOL_CALL, {"tool": "read"})

    assert [call[1] for call in emit.calls] == [EVENT_INVOKED, EVENT_RESULT]
    assert emit.calls[0][0].scope is None
    assert emit.calls[0][2] == {"hook": TOOL_CALL, "mode": "emit", "tool": "read"}
    assert emit.calls[1][2]["outcome"] == "none"
