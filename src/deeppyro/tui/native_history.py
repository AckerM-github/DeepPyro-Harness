"""Reasoning and process history helpers for native sessions.

Native sessions store schema 1 events: assistant messages carry reasoning and
tool-call content parts, and the agent loop appends matching event types
(`turn.started`, `step.started`, `assistant.message`, `tool.call`,
`tool.result`, `turn.ended`). This module rebuilds the per-turn view used by the
collapsed progress line, the full-screen thinking viewer, the history replay
performed by /resume and the agent process widget. No extra storage format is
needed and no v4 session records are read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from deeppyro.core.payload import field_of

#: User-message source kind used by the PTC transport for image attachments.
PTC_MODE_SOURCE_KIND = "ptc-mode"


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


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def source_kind(source: Any) -> str:
    """Return the kind of a message source, or an empty string.

    Native messages carry the source as a dataclass; hand-built objects and test
    doubles may expose the same field as a mapping. Anything else is treated as
    having no source.
    """
    if source is None:
        return ""
    return str(field_of(source, "kind") or "").strip()


def is_ptc_mode_source(source: Any) -> bool:
    """True when a source marks an internal PTC-mode user message.

    The PTC transport surfaces image parts as a user message whose source kind
    is ptc-mode. Those messages are model context, not something the user typed,
    so the UI skips them while the durable log keeps them untouched.
    """
    return source_kind(source) == PTC_MODE_SOURCE_KIND


def is_ptc_mode_message(message: Any) -> bool:
    """True when a Message (object or mapping) carries a PTC-mode source."""
    if message is None:
        return False
    if isinstance(message, Mapping):
        return is_ptc_mode_source(message.get("source"))
    return is_ptc_mode_source(getattr(message, "source", None))


def _part_field(part: Any, name: str) -> Any:
    if isinstance(part, Mapping):
        return part.get(name)
    return getattr(part, name, None)


def reasoning_text(message: Any) -> str:
    """Join every reasoning part of one assistant message."""
    if message is None:
        return ""
    content = _part_field(message, "content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if str(_part_field(part, "type") or "") != "reasoning":
            continue
        chunks.append(str(_part_field(part, "text") or ""))
    return "".join(chunk for chunk in chunks if chunk)


def _tool_call_fields(part: Any) -> tuple[str, str, str] | None:
    kind = str(_part_field(part, "type") or "")
    if kind not in ("tool-call", "tool_call"):
        return None
    return (
        str(_part_field(part, "id") or ""),
        str(_part_field(part, "name") or ""),
        str(_part_field(part, "arguments") or "{}"),
    )


def _tool_result_fields(message: Any) -> tuple[str, str, bool]:
    """Return (callId, text, isError) for a recorded tool message."""
    call_id = str(
        _part_field(message, "tool_call_id")
        or _part_field(message, "toolCallId")
        or _part_field(message, "callId")
        or ""
    )
    is_error = bool(_part_field(message, "is_error") or _part_field(message, "isError"))
    content = _part_field(message, "content")
    text = ""
    if isinstance(content, list):
        for part in content:
            if str(_part_field(part, "type") or "") == "text":
                text += str(_part_field(part, "text") or "")
    elif isinstance(content, str):
        text = content
    return call_id, text, is_error


def _session_events(session: Any) -> tuple[Any, ...]:
    """Return the schema 1 events of a native session or handle."""
    if session is None:
        return ()
    events = getattr(session, "events", None)
    if events is not None:
        try:
            return tuple(events)
        except TypeError:
            return ()
    handle = getattr(session, "handle", None)
    if handle is not None and handle is not session:
        return _session_events(handle)
    return ()


def _event_parts(event: Any) -> tuple[str, dict[str, Any]]:
    kind = str(getattr(event, "type", "") or "")
    data = getattr(event, "data", None)
    return kind, data if isinstance(data, dict) else {}


def reasoning_from_session(session: Any, *, last_turns: int | None = 1) -> list[TurnReasoning]:
    """Rebuild reasoning grouped by user turn from one native session.

    Args:
        session: a native session handle or an object exposing schema 1 events.
        last_turns: keep only the newest N turns with reasoning; None keeps all.

    Returns:
        Turns in chronological order, each with assistant steps in order.
    """
    turns: list[TurnReasoning] = []
    current: TurnReasoning | None = None
    turn_number = 0
    step_number = 0
    for event in _session_events(session):
        kind, data = _event_parts(event)
        if kind == "turn.started":
            turn_number += 1
            step_number = 0
            current = TurnReasoning(turn=turn_number)
            turns.append(current)
        elif kind == "step.started":
            step_number += 1
        elif kind == "assistant.message":
            if current is None:
                turn_number += 1
                current = TurnReasoning(turn=turn_number)
                turns.append(current)
            text = reasoning_text(data.get("message"))
            if not text:
                continue
            step = step_number or len(current.steps) + 1
            current.steps.append(ReasoningStep(step=step, text=text))
        elif kind == "turn.ended" and current is not None:
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


def _ensure_process(
    processes: list[TurnProcess],
    current: TurnProcess | None,
    turn_number: int,
) -> TurnProcess:
    """Return the open turn process, opening a fallback one when needed."""
    if current is None:
        reasoning = TurnReasoning(turn=turn_number)
        current = TurnProcess(turn=turn_number, reasoning=reasoning)
        processes.append(current)
    return current


def processes_from_session(session: Any, *, last_turns: int | None = None) -> list[TurnProcess]:
    """Rebuild per-turn agent processes (thinking steps and tool calls).

    The returned order matches the durable log: a thinking step is appended when
    an assistant message carries reasoning, tool calls are appended when the loop
    records them, and tool results update the matching call in place so replay
    keeps the original call/result grouping.
    """
    processes: list[TurnProcess] = []
    current: TurnProcess | None = None
    turn_number = 0
    step_number = 0
    for event in _session_events(session):
        kind, data = _event_parts(event)
        if kind == "turn.started":
            turn_number += 1
            step_number = 0
            reasoning = TurnReasoning(turn=turn_number)
            current = TurnProcess(turn=turn_number, reasoning=reasoning)
            processes.append(current)
            continue
        if kind == "step.started":
            step_number += 1
            continue
        if kind == "assistant.message":
            current = _ensure_process(processes, current, turn_number)
            step = step_number or len(current.reasoning.steps) + 1
            text = reasoning_text(data.get("message"))
            if text:
                current.reasoning.steps.append(ReasoningStep(step=step, text=text))
                current.entries.append(ProcessEntry(kind="step", step=step, text=text))
            for part in _content_parts(data.get("message")):
                call = _tool_call_fields(part)
                if call is None:
                    continue
                tool = TurnTool(call_id=call[0], name=call[1], arguments=call[2])
                current.tools.append(tool)
                current.entries.append(ProcessEntry(kind="tool", step=step, tool=tool))
            continue
        if kind == "tool.call":
            current = _ensure_process(processes, current, turn_number)
            call_id = str(data.get("call_id") or data.get("callId") or "")
            if any(tool.call_id == call_id for tool in current.tools):
                continue
            step = step_number or len(current.reasoning.steps) + 1
            tool = TurnTool(
                call_id=call_id,
                name=str(data.get("name") or ""),
                arguments=str(data.get("arguments") or "{}"),
            )
            current.tools.append(tool)
            current.entries.append(ProcessEntry(kind="tool", step=step, tool=tool))
            continue
        if kind == "tool.result":
            message = data.get("message")
            call_id, text, is_error = _tool_result_fields(message)
            if not call_id:
                call_id = str(data.get("call_id") or data.get("callId") or "")
            if data.get("error_code"):
                is_error = True
            target = _find_tool(processes, call_id)
            if target is not None:
                target.result_text = text
                target.is_error = is_error
                target.has_result = True
            continue
        if kind == "turn.ended" and current is not None:
            current.finished = True
            current = None
    filtered = [process for process in processes if process.has_content]
    if last_turns is None:
        return filtered
    if last_turns <= 0:
        return []
    return filtered[-last_turns:]


def _content_parts(message: Any) -> list[Any]:
    content = _part_field(message, "content")
    return content if isinstance(content, list) else []


def _find_tool(processes: list[TurnProcess], call_id: str) -> TurnTool | None:
    """Find the tool call one result belongs to (newest turn first)."""
    if not call_id:
        return None
    for process in reversed(processes):
        for tool in reversed(process.tools):
            if tool.call_id == call_id:
                return tool
    return None


def process_map(session: Any) -> dict[int, TurnProcess]:
    """Turn number -> agent process for /resume replay."""
    return {process.turn: process for process in processes_from_session(session, last_turns=None)}


__all__ = [
    "PTC_MODE_SOURCE_KIND",
    "ProcessEntry",
    "ReasoningStep",
    "TurnProcess",
    "TurnReasoning",
    "TurnTool",
    "is_ptc_mode_message",
    "is_ptc_mode_source",
    "process_map",
    "processes_from_session",
    "reasoning_from_session",
    "reasoning_map",
    "reasoning_text",
    "source_kind",
]
