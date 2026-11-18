"""Support types and helpers shared by the split agent-session modules."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, cast

from deeppyro.core.errors import LlmError, SessionError
from deeppyro.core.ids import new_message_id
from deeppyro.core.protocols import Finish, ToolResult, Usage
from deeppyro.core.wire import LogRecord, Message, ReasoningPart, SessionHeader, TextPart, ToolCallPart
from deeppyro.hooks.records import INVOKED_RECORD_TYPE, RESULT_RECORD_TYPE

# Keep the pre-split logger name so log routing is unchanged.
logger = logging.getLogger("deeppyro.agent.session")

T = TypeVar("T")


#: The only record types :meth:`AgentSession.record_hook_event` may append.
HOOK_RECORD_TYPES = frozenset({INVOKED_RECORD_TYPE, RESULT_RECORD_TYPE})

#: Tool-abort codes persisted in tool/result.data.error (reference pipeline).
TOOL_ABORTED = "ABORTED"
TOOL_ABORTED_BEFORE_DISPATCH = "ABORTED_BEFORE_DISPATCH"


def _abort_error(code: str) -> dict[str, Any]:
    """Build the structured error persisted for an aborted tool call."""
    return {"name": "AbortError", "code": code}


@dataclass(slots=True)
class ToolCallDraft:
    """A tool call while its argument fragments are still streaming in."""

    call_id: str
    name: str
    arguments: str = ""


@dataclass(slots=True)
class StreamAccumulator:
    """Mutable state for one provider stream attempt."""

    reasoning: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    calls: dict[str, ToolCallDraft] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    usage: Usage | None = None
    finish: Finish | None = None

    def start_call(self, call_id: str, name: str) -> None:
        if call_id not in self.calls:
            self.calls[call_id] = ToolCallDraft(call_id=call_id, name=name)
            self.order.append(call_id)

    def add_arguments(self, call_id: str, delta: str) -> None:
        draft = self.calls.get(call_id)
        if draft is None:
            raise LlmError(f"provider sent argument fragments for unknown tool call {call_id}")
        draft.arguments += delta

    def text_value(self) -> str:
        return "".join(self.text)

    def has_content(self) -> bool:
        return bool(self.reasoning or self.text or self.order)

    def parts(self, *, include_tool_calls: bool = True) -> list[Any]:
        parts: list[Any] = []
        reasoning = "".join(self.reasoning)
        if reasoning:
            parts.append(ReasoningPart(text=reasoning))
        text = "".join(self.text)
        if text:
            parts.append(TextPart(text=text))
        if include_tool_calls:
            for call_id in self.order:
                draft = self.calls[call_id]
                parts.append(
                    ToolCallPart(id=draft.call_id, name=draft.name, arguments=draft.arguments or "{}")
                )
        return parts

    def ordered_calls(self) -> list[ToolCallDraft]:
        return [self.calls[call_id] for call_id in self.order]


@dataclass(slots=True)
class _BatchOutcome:
    """Fallback tool batch result (the tools pipeline is normally available)."""

    call_id: str
    result: ToolResult
    call_seq: int | None = None


def _usage_dict(usage: Usage) -> dict[str, int]:
    payload: dict[str, int] = {
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
        "cacheReadTokens": usage.cached_input_tokens or 0,
        "cacheWriteTokens": usage.cache_write_tokens or 0,
        "totalTokens": usage.total_tokens,
    }
    if usage.reasoning_tokens is not None:
        payload["reasoningTokens"] = usage.reasoning_tokens
    return payload


def _truncation_message(max_tokens: int | None, dropped_calls: int) -> str:
    """Explain a max-tokens stop instead of silently ending the turn."""
    limit = max_tokens if max_tokens is not None else "the configured limit"
    message = f"response truncated at max_tokens={limit}"
    if dropped_calls:
        message += f"; {dropped_calls} tool call(s) were not executed"
    return message + "; raise maxTokens or split the work into smaller steps"


def _finish_reason(finish: Finish) -> dict[str, Any]:
    reason: dict[str, Any] = {"kind": finish.kind}
    if finish.failure is not None:
        reason["failure"] = dict(finish.failure)
    if finish.replay_state is not None:
        reason["replayState"] = dict(finish.replay_state)
    return reason


def _dump_messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [message.model_dump(exclude_none=True) for message in messages]


def _coerce_message(value: Any) -> Message:
    if isinstance(value, Message):
        return value
    if isinstance(value, Mapping):
        return Message.model_validate(dict(value))
    raise SessionError(f"agent/pre-step message must be a Message, got {type(value).__name__}")


def _new_user_message(text: str, *, source: Mapping[str, Any] | None = None) -> Message:
    payload = dict(source) if source is not None else {"kind": "user"}
    return Message(role="user", content=[TextPart(text=text)], id=new_message_id(), source=payload)


class SessionHandle(Protocol):
    """The session-log surface that AgentSession drives.

    The loop appends durable records and folds them back, so it relies on the
    log exposing its header, its records and append/derive_messages.  Both the
    durable Session from deeppyro.session.session and the lighter session
    doubles used by tests satisfy this Protocol; it is a static contract only
    (no runtime isinstance check), which replaces shape probing with plain
    attribute access.
    """

    @property
    def session_id(self) -> str: ...

    @property
    def header(self) -> SessionHeader: ...

    records: list[LogRecord]

    def append(self, type: str, data: dict[str, Any], **envelope: object) -> int: ...

    def derive_messages(self, *, strict: bool | None = None) -> list[Message]: ...


def _tool_names(registry: Any) -> tuple[str, ...] | None:
    """Return the registry tool names for hook payloads, when exposed."""
    names = getattr(registry, "names", None)
    if not callable(names):
        return None
    try:
        listed = cast(Iterable[Any], names())
        return tuple(sorted(str(name) for name in listed))
    # boundary: the surface registry may be a caller-supplied (custom) registry
    except Exception:  # hook metadata must never break a tool call
        logger.warning("failed to list tool names for the tool hook payload", exc_info=True)
        return None


def _tool_request(call_id: str, name: str, arguments: str) -> Any:
    """Build a tools.pipeline ToolCallRequest without importing it eagerly."""
    from deeppyro.tools.pipeline import ToolCallRequest

    return ToolCallRequest(call_id=call_id, name=name, arguments=arguments)
