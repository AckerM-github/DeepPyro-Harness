"""Agent hook adapter: native agent events re-emitted as TUI dict events.

The widgets, slots and status bar consume one dict vocabulary (see
app_events.py). The native agent loop reports typed hook events instead. This
adapter is the single translation seam: every dict emission happens here or in
the turn layer, so there are no dead events and no duplicated emissions.
"""

from __future__ import annotations

from typing import Any

from deeppyro.agent.events import (
    AssistantMessageEvent,
    BudgetExceededEvent,
    CheckpointWrittenEvent,
    NullAgentHooks,
    ReasoningDeltaEvent,
    RunFinishedEvent,
    RunStarted,
    StepStarted,
    TextDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    TurnStarted,
)

#: Hard cap on dict events emitted in one run. A runaway provider stream stops
#: flooding the UI instead of growing unbounded state.
DEFAULT_EVENT_LIMIT = 4096


class TuiAgentHooks(NullAgentHooks):
    """Translate native hook payloads into the TUI dict event vocabulary.

    Only the events the widgets consume are forwarded: status (run started),
    step, text, thinking, tool_call and tool_result. Turn lifecycle, checkpoints
    and budget events stay as facts in the session log and are surfaced by the
    turn layer, so nothing is emitted twice.
    """

    def __init__(self, sink: Any, *, event_limit: int = DEFAULT_EVENT_LIMIT) -> None:
        self._sink = sink
        self._event_limit = max(int(event_limit), 1)
        self._active = False
        self._turn = 0
        self._step = 0
        self._emitted = 0

    @property
    def active(self) -> bool:
        """Whether a run is currently streaming to the sink."""
        return self._active

    @property
    def turn(self) -> int:
        """Turn counter of the active run."""
        return self._turn

    @property
    def step(self) -> int:
        """Step counter of the active run."""
        return self._step

    @property
    def emitted(self) -> int:
        """How many dict events this adapter emitted in the active run."""
        return self._emitted

    def reset(self) -> None:
        """Forget the active run state (used when a session is detached)."""
        self._active = False
        self._turn = 0
        self._step = 0
        self._emitted = 0

    def _emit(self, event: dict[str, Any]) -> None:
        if self._emitted >= self._event_limit:
            return
        self._sink.emit(event)
        self._emitted += 1

    def run_started(self, event: RunStarted) -> None:
        if self._active:
            return
        self._active = True
        self._turn = 0
        self._step = 0
        self._emitted = 0
        self._emit({"type": "status", "phase": "thinking"})

    def turn_started(self, event: TurnStarted) -> None:
        if not self._active:
            return
        self._turn += 1
        self._step = 0

    def step_started(self, event: StepStarted) -> None:
        if not self._active:
            return
        step = getattr(event, "step", 0)
        self._step = int(step) if isinstance(step, int) and step > 0 else self._step + 1
        self._emit({"type": "step", "turn": self._turn, "step": self._step})

    def text_delta(self, event: TextDeltaEvent) -> None:
        text = str(getattr(event, "text", "") or "")
        if not text or not self._active:
            return
        self._emit({"type": "text", "text": text})

    def reasoning_delta(self, event: ReasoningDeltaEvent) -> None:
        text = str(getattr(event, "text", "") or "")
        if not text or not self._active:
            return
        self._emit({"type": "thinking", "text": text})

    def tool_call_started(self, event: ToolCallStartedEvent) -> None:
        if not self._active:
            return
        self._emit(
            {
                "type": "tool_call",
                "callId": str(getattr(event, "call_id", "") or ""),
                "name": str(getattr(event, "name", "") or ""),
                "arguments": str(getattr(event, "arguments", "") or ""),
            }
        )

    def tool_call_finished(self, event: ToolCallFinishedEvent) -> None:
        if not self._active:
            return
        result = getattr(event, "result", None)
        self._emit(
            {
                "type": "tool_result",
                "callId": str(getattr(event, "call_id", "") or ""),
                "isError": bool(getattr(result, "is_error", False)),
                "text": str(getattr(result, "text", "") or ""),
            }
        )

    def assistant_message(self, event: AssistantMessageEvent) -> None:
        # The assistant message is a fact in the log; the turn layer emits final.
        return None

    def checkpoint_written(self, event: CheckpointWrittenEvent) -> None:
        return None

    def budget_exceeded(self, event: BudgetExceededEvent) -> None:
        # The turn layer turns this into an error event with the budget text.
        return None

    def run_finished(self, event: RunFinishedEvent) -> None:
        self._active = False


__all__ = ["DEFAULT_EVENT_LIMIT", "TuiAgentHooks"]
