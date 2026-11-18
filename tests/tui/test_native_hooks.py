"""Tests for the native hooks to v4 TUI event adapter."""

from __future__ import annotations

from typing import Any

from deeppyro.agent.events import (
    AssistantMessageEvent,
    BudgetExceededEvent,
    CheckpointWrittenEvent,
    ReasoningDeltaEvent,
    RunFinishedEvent,
    RunStarted,
    StepStarted,
    TextDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    TurnStarted,
)
from deeppyro.core.protocols import ToolResult
from deeppyro.tui.native_hooks import DEFAULT_EVENT_LIMIT, TuiAgentHooks

RUN = "run_" + "1" * 16
TRACE = "a" * 32
SESSION = "session-" + "2" * 8
TURN = "turn_" + "3" * 16
STEP = "step_" + "4" * 16


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))


def _run_started() -> RunStarted:
    return RunStarted(run_id=RUN, trace_id=TRACE, session_id=SESSION)


def _turn_started() -> TurnStarted:
    return TurnStarted(run_id=RUN, trace_id=TRACE, turn_id=TURN, prompt_message_id=None)


def _step_started(step: int = 1) -> StepStarted:
    return StepStarted(run_id=RUN, trace_id=TRACE, turn_id=TURN, step_id=STEP, step=step)


def _text(text: str) -> TextDeltaEvent:
    return TextDeltaEvent(run_id=RUN, trace_id=TRACE, turn_id=TURN, step_id=STEP, text=text)


def _reasoning(text: str) -> ReasoningDeltaEvent:
    return ReasoningDeltaEvent(run_id=RUN, trace_id=TRACE, turn_id=TURN, step_id=STEP, text=text)


def _tool_started() -> ToolCallStartedEvent:
    return ToolCallStartedEvent(
        run_id=RUN,
        trace_id=TRACE,
        turn_id=TURN,
        step_id=STEP,
        call_id="call_1",
        name="read",
        arguments="{}",
        idempotency_key="key",
    )


def _tool_finished(*, is_error: bool = False, text: str = "ok") -> ToolCallFinishedEvent:
    return ToolCallFinishedEvent(
        run_id=RUN,
        trace_id=TRACE,
        turn_id=TURN,
        step_id=STEP,
        call_id="call_1",
        name="read",
        result=ToolResult(text=text, is_error=is_error),
        duration_ms=3,
    )


def test_hooks_emit_the_exact_v4_event_sequence() -> None:
    sink = _RecordingSink()
    hooks = TuiAgentHooks(sink)

    hooks.run_started(_run_started())
    hooks.turn_started(_turn_started())
    hooks.step_started(_step_started(step=1))
    hooks.text_delta(_text("hello "))
    hooks.text_delta(_text("world"))
    hooks.reasoning_delta(_reasoning("thinking..."))
    hooks.tool_call_started(_tool_started())
    hooks.tool_call_finished(_tool_finished())
    hooks.assistant_message(
        AssistantMessageEvent(
            run_id=RUN,
            trace_id=TRACE,
            turn_id=TURN,
            step_id=STEP,
            message=object(),
            finish_reason="stop",
        )
    )
    hooks.checkpoint_written(
        CheckpointWrittenEvent(
            run_id=RUN,
            trace_id=TRACE,
            session_id=SESSION,
            path="checkpoint.json",
            last_seq=4,
            status="ok",
        )
    )
    hooks.budget_exceeded(BudgetExceededEvent(run_id=RUN, trace_id=TRACE, kind="steps", limit=1, actual=2))
    hooks.run_finished(
        RunFinishedEvent(run_id=RUN, trace_id=TRACE, session_id=SESSION, status="completed", reason=None)
    )

    assert sink.events == [
        {"type": "status", "phase": "thinking"},
        {"type": "step", "turn": 1, "step": 1},
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world"},
        {"type": "thinking", "text": "thinking..."},
        {"type": "tool_call", "callId": "call_1", "name": "read", "arguments": "{}"},
        {"type": "tool_result", "callId": "call_1", "isError": False, "text": "ok"},
    ]
    assert hooks.active is False


def test_hooks_report_tool_errors() -> None:
    sink = _RecordingSink()
    hooks = TuiAgentHooks(sink)
    hooks.run_started(_run_started())
    hooks.turn_started(_turn_started())
    hooks.step_started(_step_started())
    hooks.tool_call_finished(_tool_finished(is_error=True, text="boom"))
    hooks.run_finished(
        RunFinishedEvent(run_id=RUN, trace_id=TRACE, session_id=SESSION, status="failed", reason=None)
    )
    tool_results = [event for event in sink.events if event["type"] == "tool_result"]
    assert tool_results == [{"type": "tool_result", "callId": "call_1", "isError": True, "text": "boom"}]


def test_hooks_ignore_events_outside_a_run() -> None:
    sink = _RecordingSink()
    hooks = TuiAgentHooks(sink)

    hooks.text_delta(_text("stray"))
    hooks.step_started(_step_started())
    hooks.turn_started(_turn_started())
    assert sink.events == []

    hooks.run_started(_run_started())
    hooks.run_started(_run_started())
    assert sink.events == [{"type": "status", "phase": "thinking"}]

    hooks.run_finished(
        RunFinishedEvent(run_id=RUN, trace_id=TRACE, session_id=SESSION, status="completed", reason=None)
    )
    hops = len(sink.events)
    hooks.text_delta(_text("after"))
    assert len(sink.events) == hops


def test_hooks_track_turn_and_step_counters() -> None:
    sink = _RecordingSink()
    hooks = TuiAgentHooks(sink)
    hooks.run_started(_run_started())
    hooks.turn_started(_turn_started())
    hooks.step_started(_step_started(step=1))
    hooks.step_started(_step_started(step=2))
    hooks.turn_started(_turn_started())
    hooks.step_started(_step_started(step=1))

    steps = [event for event in sink.events if event["type"] == "step"]
    assert steps == [
        {"type": "step", "turn": 1, "step": 1},
        {"type": "step", "turn": 1, "step": 2},
        {"type": "step", "turn": 2, "step": 1},
    ]
    assert hooks.turn == 2
    assert hooks.step == 1


def test_hooks_bound_their_event_queue() -> None:
    sink = _RecordingSink()
    hooks = TuiAgentHooks(sink, event_limit=3)
    hooks.run_started(_run_started())
    for index in range(10):
        hooks.text_delta(_text(str(index)))

    assert len(sink.events) == 3
    assert hooks.emitted == 3
    assert hooks.active is True
    hooks.reset()
    assert hooks.active is False
    assert hooks.emitted == 0


def test_default_event_limit_is_finite() -> None:
    assert DEFAULT_EVENT_LIMIT > 0
    assert DEFAULT_EVENT_LIMIT <= 100_000
