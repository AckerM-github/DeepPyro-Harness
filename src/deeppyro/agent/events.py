"""Typed loop hooks (M2 task 1).

The native loop never raises string-keyed bus events for its own lifecycle. It
calls an :class:`AgentHooks` implementation with frozen dataclass payloads
instead; the default :class:`NullAgentHooks` makes hooks entirely optional.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from deeppyro.agent.budget import BudgetKind
from deeppyro.core.messages import Message
from deeppyro.core.protocols import ToolResult


@dataclass(frozen=True, slots=True)
class RunStarted:
    """A native run began; the session log already contains run.started."""

    run_id: str
    trace_id: str
    session_id: str
    resumed: bool = False


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """A conversation turn began."""

    run_id: str
    trace_id: str
    turn_id: str
    prompt_message_id: str | None


@dataclass(frozen=True, slots=True)
class StepStarted:
    """A model step began."""

    run_id: str
    trace_id: str
    turn_id: str
    step_id: str
    step: int


@dataclass(frozen=True, slots=True)
class TextDeltaEvent:
    """One streamed assistant text fragment."""

    run_id: str
    trace_id: str
    turn_id: str
    step_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDeltaEvent:
    """One streamed assistant reasoning fragment."""

    run_id: str
    trace_id: str
    turn_id: str
    step_id: str
    text: str


@dataclass(frozen=True, slots=True)
class AssistantMessageEvent:
    """A complete assistant message was persisted."""

    run_id: str
    trace_id: str
    turn_id: str
    step_id: str
    message: Message
    finish_reason: str


@dataclass(frozen=True, slots=True)
class ToolCallStartedEvent:
    """A tool call was recorded and is about to be dispatched."""

    run_id: str
    trace_id: str
    turn_id: str
    step_id: str
    call_id: str
    name: str
    arguments: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ToolCallFinishedEvent:
    """A tool call returned a result (including error results)."""

    run_id: str
    trace_id: str
    turn_id: str
    step_id: str
    call_id: str
    name: str
    result: ToolResult
    duration_ms: int


@dataclass(frozen=True, slots=True)
class CheckpointWrittenEvent:
    """A run checkpoint file was atomically replaced."""

    run_id: str
    trace_id: str
    session_id: str
    path: str
    last_seq: int
    status: str


@dataclass(frozen=True, slots=True)
class BudgetExceededEvent:
    """A budget limit was crossed and the loop is aborting the run."""

    run_id: str
    trace_id: str
    kind: BudgetKind
    limit: int | float
    actual: int | float


@dataclass(frozen=True, slots=True)
class RunFinishedEvent:
    """The run reached a terminal status."""

    run_id: str
    trace_id: str
    session_id: str
    status: str
    reason: Mapping[str, object] | None


class AgentHooks(Protocol):
    """Typed lifecycle observer for one native loop."""

    def run_started(self, event: RunStarted) -> None:
        """Called after run.started is written."""

    def turn_started(self, event: TurnStarted) -> None:
        """Called after turn.started is written."""

    def step_started(self, event: StepStarted) -> None:
        """Called after step.started is written."""

    def text_delta(self, event: TextDeltaEvent) -> None:
        """Called for every streamed assistant text fragment."""

    def reasoning_delta(self, event: ReasoningDeltaEvent) -> None:
        """Called for every streamed assistant reasoning fragment."""

    def assistant_message(self, event: AssistantMessageEvent) -> None:
        """Called after assistant.message is written."""

    def tool_call_started(self, event: ToolCallStartedEvent) -> None:
        """Called after tool.call is written, before dispatch."""

    def tool_call_finished(self, event: ToolCallFinishedEvent) -> None:
        """Called after the tool result is available."""

    def checkpoint_written(self, event: CheckpointWrittenEvent) -> None:
        """Called after a checkpoint file was written."""

    def budget_exceeded(self, event: BudgetExceededEvent) -> None:
        """Called right before the run aborts on a budget limit."""

    def run_finished(self, event: RunFinishedEvent) -> None:
        """Called after run.finished is written."""


class NullAgentHooks:
    """No-op hooks; the loop's default."""

    def run_started(self, event: RunStarted) -> None:
        """No-op."""

    def turn_started(self, event: TurnStarted) -> None:
        """No-op."""

    def step_started(self, event: StepStarted) -> None:
        """No-op."""

    def text_delta(self, event: TextDeltaEvent) -> None:
        """No-op."""

    def reasoning_delta(self, event: ReasoningDeltaEvent) -> None:
        """No-op."""

    def assistant_message(self, event: AssistantMessageEvent) -> None:
        """No-op."""

    def tool_call_started(self, event: ToolCallStartedEvent) -> None:
        """No-op."""

    def tool_call_finished(self, event: ToolCallFinishedEvent) -> None:
        """No-op."""

    def checkpoint_written(self, event: CheckpointWrittenEvent) -> None:
        """No-op."""

    def budget_exceeded(self, event: BudgetExceededEvent) -> None:
        """No-op."""

    def run_finished(self, event: RunFinishedEvent) -> None:
        """No-op."""


__all__ = [
    "AgentHooks",
    "AssistantMessageEvent",
    "BudgetExceededEvent",
    "CheckpointWrittenEvent",
    "NullAgentHooks",
    "ReasoningDeltaEvent",
    "RunFinishedEvent",
    "RunStarted",
    "StepStarted",
    "TextDeltaEvent",
    "ToolCallFinishedEvent",
    "ToolCallStartedEvent",
    "TurnStarted",
]
