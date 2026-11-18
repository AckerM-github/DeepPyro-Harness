"""Native agent hooks to typed web stream events.

A TUI-free analog of tui/native_hooks.py: only the events the browser consumes
are forwarded (status, step, text, thinking, tool_call, tool_result). Turn
lifecycle, usage, final text and errors are synthesized by the web session host
after the native agent returns, so they are not emitted twice.
"""

from __future__ import annotations

from collections.abc import Callable

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
from deeppyro.web.events import (
    StatusEvent,
    StatusPhase,
    StepEvent,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    WebEvent,
)

#: Hard cap on events emitted in one run, so a runaway provider stream cannot
#: grow unbounded UI state.
DEFAULT_EVENT_LIMIT = 4096


class WebAgentHooks(NullAgentHooks):
    """Translate native hook payloads into typed web events."""

    def __init__(
        self,
        session_id: str,
        emit: Callable[[WebEvent], None],
        *,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> None:
        self._session_id = session_id
        self._emit_event = emit
        self._event_limit = max(int(event_limit), 1)
        self._active = False
        self._turn = 0
        self._step = 0
        self._emitted = 0

    @property
    def active(self) -> bool:
        """Whether a run is currently streaming events."""
        return self._active

    @property
    def emitted(self) -> int:
        """How many events this adapter emitted during the active run."""
        return self._emitted

    def reset(self) -> None:
        """Forget the active run state (used when a session is detached)."""
        self._active = False
        self._turn = 0
        self._step = 0
        self._emitted = 0

    def _emit(self, event: WebEvent) -> None:
        if self._emitted >= self._event_limit:
            return
        self._emit_event(event)
        self._emitted += 1

    def run_started(self, event: RunStarted) -> None:
        if self._active:
            return
        self._active = True
        self._turn = 0
        self._step = 0
        self._emitted = 0
        self._emit(StatusEvent(session_id=self._session_id, phase=StatusPhase.THINKING))

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
        self._emit(StepEvent(session_id=self._session_id, turn=self._turn, step=self._step))

    def text_delta(self, event: TextDeltaEvent) -> None:
        text = str(getattr(event, "text", "") or "")
        if not text or not self._active:
            return
        self._emit(TextEvent(session_id=self._session_id, text=text))

    def reasoning_delta(self, event: ReasoningDeltaEvent) -> None:
        text = str(getattr(event, "text", "") or "")
        if not text or not self._active:
            return
        self._emit(ThinkingEvent(session_id=self._session_id, text=text))

    def tool_call_started(self, event: ToolCallStartedEvent) -> None:
        if not self._active:
            return
        self._emit(
            ToolCallEvent(
                session_id=self._session_id,
                call_id=str(getattr(event, "call_id", "") or ""),
                name=str(getattr(event, "name", "") or ""),
                arguments=str(getattr(event, "arguments", "") or ""),
            )
        )

    def tool_call_finished(self, event: ToolCallFinishedEvent) -> None:
        if not self._active:
            return
        result = getattr(event, "result", None)
        self._emit(
            ToolResultEvent(
                session_id=self._session_id,
                call_id=str(getattr(event, "call_id", "") or ""),
                is_error=bool(getattr(result, "is_error", False)),
                text=str(getattr(result, "text", "") or ""),
            )
        )

    def assistant_message(self, event: AssistantMessageEvent) -> None:
        """The assistant message is a native fact; the turn layer emits final."""
        return None

    def checkpoint_written(self, event: CheckpointWrittenEvent) -> None:
        """Checkpoints are trace facts, not chat events."""
        return None

    def budget_exceeded(self, event: BudgetExceededEvent) -> None:
        """The turn layer turns this into an error event with the budget text."""
        return None

    def run_finished(self, event: RunFinishedEvent) -> None:
        self._active = False


__all__ = ["DEFAULT_EVENT_LIMIT", "WebAgentHooks"]
