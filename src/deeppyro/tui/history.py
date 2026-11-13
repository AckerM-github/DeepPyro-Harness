"""Reasoning history helpers for the terminal UI.

Reasoning is already part of the durable session log: every assistant/message
stores its thinking parts inside the message content. This module rebuilds a
per-turn view for the collapsed progress line, the full-screen thinking viewer
and the history replay performed by /resume. No new storage format is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReasoningStep:
    """One assistant step's reasoning inside a user turn."""

    step: int = 0
    text: str = ""


@dataclass(slots=True)
class TurnReasoning:
    """All reasoning recorded for one user turn."""

    turn: int = 0
    steps: list[ReasoningStep] = field(default_factory=list)
    finished: bool = False

    @property
    def has_reasoning(self) -> bool:
        return bool(self.steps)

    @property
    def sentence_count(self) -> int:
        from deeppyro.tui.widgets.thinking import split_sentences

        return sum(len(split_sentences(step.text)) for step in self.steps)

    def text_for(self, step: int) -> str:
        for item in self.steps:
            if item.step == step:
                return item.text
        return ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _part_text(part: Any) -> str:
    """Return the text of a reasoning content part, or an empty string."""
    if isinstance(part, dict):
        if part.get("type") == "reasoning":
            return str(part.get("text") or "")
        return ""
    if getattr(part, "type", "") == "reasoning":
        return str(getattr(part, "text", "") or "")
    return ""


def reasoning_text(message: Any) -> str:
    """Join every reasoning part of one assistant message."""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if not isinstance(content, list):
        return ""
    chunks = [_part_text(part) for part in content]
    return "".join(chunk for chunk in chunks if chunk)


def reasoning_from_session(session: Any, *, last_turns: int | None = 1) -> list[TurnReasoning]:
    """Rebuild reasoning grouped by user turn from durable session records.

    Args:
        session: an object with a records list (the Session or a fake).
        last_turns: keep only the newest N turns with reasoning; None keeps all.

    Returns:
        Turns in chronological order, each with assistant steps in order.
    """
    records = getattr(session, "records", None)
    if not isinstance(records, list):
        return []
    turns: list[TurnReasoning] = []
    current: TurnReasoning | None = None
    fallback_turn = 1
    for record in records:
        record_type = str(getattr(record, "type", ""))
        data = getattr(record, "data", None)
        if not isinstance(data, dict):
            if record_type == "turn/start":
                current = TurnReasoning(turn=fallback_turn)
                turns.append(current)
            continue
        if record_type == "turn/start":
            number = _as_int(data.get("turn"), fallback_turn)
            fallback_turn = number + 1
            current = TurnReasoning(turn=number)
            turns.append(current)
        elif record_type == "assistant/message":
            if current is None:
                current = TurnReasoning(turn=fallback_turn)
                turns.append(current)
            message = data.get("message")
            text = reasoning_text(message)
            if not text:
                continue
            step = _as_int(data.get("step"), len(current.steps) + 1)
            current.steps.append(ReasoningStep(step=step, text=text))
        elif record_type == "turn/end" and current is not None:
            current.finished = True
            current = None
    filtered = [turn for turn in turns if turn.steps]
    if last_turns is None:
        return filtered
    if last_turns <= 0:
        return []
    return filtered[-last_turns:]


def reasoning_map(session: Any) -> dict[int, TurnReasoning]:
    """Turn number -> reasoning for replay and /thinking lookups."""
    return {turn.turn: turn for turn in reasoning_from_session(session, last_turns=None)}


# --------------------------------------------------------------------------
# Agent process history: reasoning steps and tool calls in one ordered list
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TurnTool:
    """One tool call recorded inside a user turn."""

    call_id: str = ""
    name: str = ""
    arguments: str = ""
    result_text: str = ""
    is_error: bool = False
    has_result: bool = False


@dataclass(slots=True)
class ProcessEntry:
    """One ordered line inside an agent process: a thinking step or a tool call."""

    kind: str = "step"  # "step" | "tool"
    step: int = 0
    text: str = ""
    tool: TurnTool | None = None


@dataclass(slots=True)
class TurnProcess:
    """Everything the agent did during one user turn."""

    turn: int = 0
    reasoning: TurnReasoning = field(default_factory=TurnReasoning)
    entries: list[ProcessEntry] = field(default_factory=list)
    tools: list[TurnTool] = field(default_factory=list)
    finished: bool = False

    @property
    def has_content(self) -> bool:
        return self.reasoning.has_reasoning or bool(self.tools)


def _tool_call_part(part: Any) -> tuple[str, str, str] | None:
    """Return (callId, name, arguments) for a tool-call content part."""
    if isinstance(part, dict):
        if part.get("type") != "tool-call":
            return None
        return (
            str(part.get("id") or ""),
            str(part.get("name") or ""),
            str(part.get("arguments") or "{}"),
        )
    if getattr(part, "type", "") == "tool-call":
        return (
            str(getattr(part, "id", "") or ""),
            str(getattr(part, "name", "") or ""),
            str(getattr(part, "arguments", "{}") or "{}"),
        )
    return None


def _message_content(message: Any) -> list[Any] | None:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return content if isinstance(content, list) else None


def _tool_result_fields(message: Any) -> tuple[str, str, bool]:
    """Return (callId, text, isError) for a recorded tool message."""
    if isinstance(message, dict):
        call_id = str(message.get("toolCallId") or message.get("callId") or "")
        content = message.get("content")
        is_error = bool(message.get("isError"))
    else:
        call_id = str(getattr(message, "toolCallId", "") or "")
        content = getattr(message, "content", None)
        is_error = bool(getattr(message, "isError", False))
    text = ""
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text += str(part.get("text") or "")
            elif getattr(part, "type", "") == "text":
                text += str(getattr(part, "text", "") or "")
    elif isinstance(content, str):
        text = content
    return call_id, text, is_error


def processes_from_session(session: Any, *, last_turns: int | None = None) -> list[TurnProcess]:
    """Rebuild per-turn agent processes (thinking steps and tool calls) from a session.

    The returned order matches the durable log: a thinking step is appended when
    an assistant message carries reasoning, and tool calls are appended in message
    order. Tool results update the matching call in place, so replay keeps the
    original call/result grouping.
    """
    records = getattr(session, "records", None)
    if not isinstance(records, list):
        return []
    processes: list[TurnProcess] = []
    current: TurnProcess | None = None
    fallback_turn = 1
    for record in records:
        record_type = str(getattr(record, "type", ""))
        data = getattr(record, "data", None)
        if not isinstance(data, dict):
            if record_type == "turn/start":
                reasoning = TurnReasoning(turn=fallback_turn)
                current = TurnProcess(turn=fallback_turn, reasoning=reasoning)
                processes.append(current)
            continue
        if record_type == "turn/start":
            number = _as_int(data.get("turn"), fallback_turn)
            fallback_turn = number + 1
            reasoning = TurnReasoning(turn=number)
            current = TurnProcess(turn=number, reasoning=reasoning)
            processes.append(current)
            continue
        if record_type == "assistant/message":
            if current is None:
                reasoning = TurnReasoning(turn=fallback_turn)
                current = TurnProcess(turn=fallback_turn, reasoning=reasoning)
                processes.append(current)
            message = data.get("message")
            step = _as_int(data.get("step"), len(current.reasoning.steps) + 1)
            text = reasoning_text(message)
            if text:
                current.reasoning.steps.append(ReasoningStep(step=step, text=text))
                current.entries.append(ProcessEntry(kind="step", step=step, text=text))
            for part in _message_content(message) or []:
                call = _tool_call_part(part)
                if call is None:
                    continue
                tool = TurnTool(call_id=call[0], name=call[1], arguments=call[2])
                current.tools.append(tool)
                current.entries.append(ProcessEntry(kind="tool", step=step, tool=tool))
            continue
        if record_type == "tool/result":
            if current is None:
                reasoning = TurnReasoning(turn=fallback_turn)
                current = TurnProcess(turn=fallback_turn, reasoning=reasoning)
                processes.append(current)
            message = data.get("message")
            call_id, text, is_error = _tool_result_fields(message)
            for tool in current.tools:
                if tool.call_id == call_id:
                    tool.result_text = text
                    tool.is_error = is_error
                    tool.has_result = True
                    break
            continue
        if record_type == "turn/end" and current is not None:
            current.finished = True
            current = None
    filtered = [process for process in processes if process.has_content]
    if last_turns is None:
        return filtered
    if last_turns <= 0:
        return []
    return filtered[-last_turns:]


def process_map(session: Any) -> dict[int, TurnProcess]:
    """Turn number -> agent process for /resume replay."""
    return {process.turn: process for process in processes_from_session(session, last_turns=None)}


__all__ = [
    "ProcessEntry",
    "ReasoningStep",
    "TurnProcess",
    "TurnReasoning",
    "TurnTool",
    "process_map",
    "processes_from_session",
    "reasoning_from_session",
    "reasoning_map",
    "reasoning_text",
]
