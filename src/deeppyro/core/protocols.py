"""Frozen runtime protocols shared by session / llm / tools / agent / cli.

These are the seams between workstreams. Change them only with the Lead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Protocol, runtime_checkable

from pydantic import Field

from deeppyro.core.wire import Message, WireModel

FinishKind = Literal["stop", "tool-calls", "length", "error", "aborted", "content-filter"]


# --------------------------------------------------------------------------
# LLM request
# --------------------------------------------------------------------------


class ToolSchema(WireModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class LlmRequest(WireModel):
    provider: str
    model: str
    messages: list[Message] = Field(default_factory=list)
    system: str | None = None
    tools: list[ToolSchema] = Field(default_factory=list)
    max_tokens: int | None = None
    temperature: float | None = None
    context_window: int | None = None
    # stable per-session identifier used by providers that support prompt caching
    session_id: str | None = None


# --------------------------------------------------------------------------
# Stream events (provider -> loop)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TextDelta:
    text: str


@dataclass(slots=True)
class ReasoningDelta:
    text: str


@dataclass(slots=True)
class ToolCallStart:
    call_id: str
    name: str


@dataclass(slots=True)
class ToolCallArgsDelta:
    call_id: str
    delta: str


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int | None = None


@dataclass(slots=True)
class Finish:
    kind: FinishKind
    failure: dict[str, Any] | None = None


StreamEvent = TextDelta | ReasoningDelta | ToolCallStart | ToolCallArgsDelta | Usage | Finish


@runtime_checkable
class LlmProvider(Protocol):
    name: str

    def stream(self, req: LlmRequest) -> AsyncIterator[StreamEvent]: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ToolResult:
    text: str = ""
    is_error: bool = False
    meta: dict[str, Any] | None = None
    # optional non-text parts (images etc.) in wire form
    parts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ToolContext:
    cwd: Path
    session_id: str
    turn: int
    step: int
    call_id: str
    cancel: asyncio.Event
    log: Callable[[str, dict[str, Any]], None]
    # permission gate object owned by deeppyro.sandbox; declared structurally
    permission: Any | None = None
    # runtime services (jobs manager, session store, ...) attached by the app
    services: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    parallel_safe: bool

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


@runtime_checkable
class SessionLogLike(Protocol):
    session_id: str

    def append(self, type: str, data: dict[str, Any]) -> int:
        """Append a record; returns its seq."""
        ...

    def derive_messages(self) -> list[Message]:
        """Messages as the model should see them."""
        ...


# --------------------------------------------------------------------------
# Events (loop -> UI / --json stream)
# --------------------------------------------------------------------------


@runtime_checkable
class EventSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...
