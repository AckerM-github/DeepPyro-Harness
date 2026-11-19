"""Typed stream events for the web host, plus their snake_case wire form.

This is the web layer's own event vocabulary, translated from native agent
hooks by deeppyro.web.hooks. Python names are snake_case; the HTTP/SSE JSON is
snake_case too (unlike the TUI dict contract, which uses camelCase). Every
event carries the session id so one journal can serve several session streams.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from deeppyro.web.wire import JsonValue


class WebEventType(StrEnum):
    """Event type tag on the wire."""

    SESSION = "session"
    STATUS = "status"
    STEP = "step"
    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL = "approval"
    FINAL = "final"
    USAGE = "usage"
    ERROR = "error"


class StatusPhase(StrEnum):
    """Turn phase reported by status events."""

    THINKING = "thinking"
    DONE = "done"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """The host attached a session; the UI should load its transcript."""

    session_id: str


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """Turn phase change."""

    session_id: str
    phase: StatusPhase


@dataclass(frozen=True, slots=True)
class StepEvent:
    """A new agent loop step started."""

    session_id: str
    turn: int
    step: int


@dataclass(frozen=True, slots=True)
class TextEvent:
    """Assistant text delta."""

    session_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingEvent:
    """Reasoning text delta."""

    session_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    """A tool call started."""

    session_id: str
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    """A tool call finished."""

    session_id: str
    call_id: str
    is_error: bool
    text: str


@dataclass(frozen=True, slots=True)
class ApprovalEvent:
    """A sandbox approval prompt appeared or was settled."""

    session_id: str
    approval_id: str
    tool: str
    summary: str
    detail: str
    status: str


@dataclass(frozen=True, slots=True)
class FinalEvent:
    """The turn produced its final assistant text."""

    session_id: str
    text: str


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """Token usage of the finished turn."""

    session_id: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    """The turn or the connection failed with a human-readable message."""

    session_id: str
    message: str


#: Any event a session stream can carry.
WebEvent = (
    SessionEvent
    | StatusEvent
    | StepEvent
    | TextEvent
    | ThinkingEvent
    | ToolCallEvent
    | ToolResultEvent
    | ApprovalEvent
    | FinalEvent
    | UsageEvent
    | ErrorEvent
)


def to_wire(event: WebEvent) -> dict[str, JsonValue]:
    """Serialize one event to its snake_case HTTP/SSE JSON object."""
    payload: dict[str, JsonValue] = {
        "type": str(WebEventType(_event_type(event))),
        "session_id": event.session_id,
    }
    match event:
        case SessionEvent():
            return payload
        case StatusEvent():
            payload["phase"] = str(event.phase)
        case StepEvent():
            payload["turn"] = event.turn
            payload["step"] = event.step
        case TextEvent() | ThinkingEvent() | FinalEvent():
            payload["text"] = event.text
        case ToolCallEvent():
            payload["call_id"] = event.call_id
            payload["name"] = event.name
            payload["arguments"] = event.arguments
        case ToolResultEvent():
            payload["call_id"] = event.call_id
            payload["is_error"] = event.is_error
            payload["text"] = event.text
        case ApprovalEvent():
            payload["approval_id"] = event.approval_id
            payload["tool"] = event.tool
            payload["summary"] = event.summary
            payload["detail"] = event.detail
            payload["status"] = event.status
        case UsageEvent():
            payload["input_tokens"] = event.input_tokens
            payload["output_tokens"] = event.output_tokens
            payload["cached_input_tokens"] = event.cached_input_tokens
        case ErrorEvent():
            payload["message"] = event.message
        case _:  # pragma: no cover - the union above is exhaustive
            raise TypeError(f"unsupported web event: {type(event).__name__}")
    return payload


def _event_type(event: WebEvent) -> WebEventType:
    return _TYPE_BY_CLASS[type(event)]


_TYPE_BY_CLASS: dict[type[object], WebEventType] = {
    SessionEvent: WebEventType.SESSION,
    StatusEvent: WebEventType.STATUS,
    StepEvent: WebEventType.STEP,
    TextEvent: WebEventType.TEXT,
    ThinkingEvent: WebEventType.THINKING,
    ToolCallEvent: WebEventType.TOOL_CALL,
    ToolResultEvent: WebEventType.TOOL_RESULT,
    ApprovalEvent: WebEventType.APPROVAL,
    FinalEvent: WebEventType.FINAL,
    UsageEvent: WebEventType.USAGE,
    ErrorEvent: WebEventType.ERROR,
}


def encode_sse_frame(seq: int, event: WebEvent) -> bytes:
    """Encode one SSE frame with a replayable event id."""
    payload = json.dumps(to_wire(event), ensure_ascii=False, separators=(",", ":"))
    return f"id: {seq}\ndata: {payload}\n\n".encode()


#: SSE comment frame used as a keep-alive heartbeat.
HEARTBEAT_FRAME = b": keep-alive\n\n"


__all__ = [
    "HEARTBEAT_FRAME",
    "ApprovalEvent",
    "ErrorEvent",
    "FinalEvent",
    "SessionEvent",
    "StatusEvent",
    "StatusPhase",
    "StepEvent",
    "TextEvent",
    "ThinkingEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "UsageEvent",
    "WebEvent",
    "WebEventType",
    "encode_sse_frame",
    "to_wire",
]
