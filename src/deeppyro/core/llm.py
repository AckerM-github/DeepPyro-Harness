"""Native LLM protocol (KI-010).

This module is the wire-free contract between a native provider and the native
agent loop. It must stay importable without `deeppyro.core.wire` and without
`deeppyro.core.protocols` (which re-exports the v4 types and imports wire), so
it only depends on the standard library plus :mod:`deeppyro.core.messages`.

The v4 equivalents live in :mod:`deeppyro.core.protocols` and survive until
the v4 agent/session stack is deleted; the native loop and its providers use the
types below exclusively.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from deeppyro.core.messages import Message

__all__ = [
    "Finish",
    "LlmProvider",
    "ModelRequest",
    "ModelResponse",
    "NativeToolCall",
    "ReasoningDelta",
    "RetryScheduled",
    "StopReason",
    "StreamEvent",
    "TextDelta",
    "ToolCallArgsDelta",
    "ToolCallStart",
    "ToolSpec",
    "Usage",
    "UsageEvent",
]


class StopReason(StrEnum):
    """Native finish reasons; str-valued for session-log fidelity."""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    ERROR = "error"
    ABORTED = "aborted"
    CONTENT_FILTER = "content_filter"


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for one model call.

    `cached_input_tokens` is the provider's prompt-cache hit count, surfaced today
    by OpenAI-compatible APIs as `prompt_tokens_details.cached_tokens` or the
    DeepSeek `prompt_cache_hit_tokens` field. `None` means "not reported".
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Native tool declaration replacing the v4 `ToolSchema` wire model."""

    name: str
    description: str = ""
    parameters: dict = field(default_factory=dict)
    output: dict | None = None

    def to_dict(self) -> dict:
        """Return the canonical JSON projection used for prompt hashing."""

        payload: dict = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.output is not None:
            payload["output"] = self.output
        return payload


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One native model call request.

    `session_id` is the prompt-cache affinity key; it is carried for trace/hash
    today and reserved for future cache-affinity headers.
    """

    provider: str
    model: str
    messages: tuple[Message, ...]
    system: Message | None = None
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int | None = None
    temperature: float | None = None
    context_window: int | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str
    index: int = 0


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    text: str
    index: int = 0


@dataclass(frozen=True, slots=True)
class ToolCallStart:
    call_id: str
    name: str
    index: int = 0


@dataclass(frozen=True, slots=True)
class ToolCallArgsDelta:
    call_id: str
    delta: str
    name: str | None = None
    index: int = 0


@dataclass(frozen=True, slots=True)
class UsageEvent:
    usage: Usage


@dataclass(frozen=True, slots=True)
class RetryScheduled:
    """A transport retry was scheduled; emitted by providers before sleeping."""

    attempt: int
    code: str | None = None
    delay_s: float = 0.0


@dataclass(frozen=True, slots=True)
class Finish:
    stop_reason: StopReason
    message: str | None = None
    code: str | None = None
    replay_state: dict | None = None


StreamEvent = (
    TextDelta | ReasoningDelta | ToolCallStart | ToolCallArgsDelta | UsageEvent | RetryScheduled | Finish
)


@dataclass(frozen=True, slots=True)
class NativeToolCall:
    """A fully assembled tool call.

    `arguments` is the raw provider JSON fragment join (defaults to `"{}"`), not a
    parsed object: callers own parsing and error handling.
    """

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """The complete result of one native model call."""

    text: str
    reasoning: str
    tool_calls: tuple[NativeToolCall, ...]
    finish_reason: StopReason
    usage: Usage
    latency_ms: int
    retries: int = 0
    replay_state: dict | None = None


@runtime_checkable
class LlmProvider(Protocol):
    """Native provider protocol; stream events end with exactly one `Finish`."""

    name: str

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield stream events for `request`."""
        ...

    async def aclose(self) -> None:
        """Release transport resources."""
        ...
