"""Deterministic context planning for one model step (M2 task 2).

:class:`ContextPlan` is a frozen, pure snapshot of everything a model call needs:
the optional system message, the conversation messages in order, the tool
schemas, a token estimate and the source message ids for trace correlation.
The loop builds a new plan each step; nothing here has side effects.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from deeppyro.core.messages import ImagePart, Message, ReasoningPart, TextPart, ToolCallPart
from deeppyro.core.protocols import Tool, ToolSchema

_TOKENS_PER_CHAR = 4
_IMAGE_TOKEN_ESTIMATE = 256
_MESSAGE_OVERHEAD_TOKENS = 4
_TOOL_OVERHEAD_TOKENS = 8


def estimate_text_tokens(text: str) -> int:
    """Estimate tokens for a text fragment (roughly one token per four characters)."""
    return (len(text) + _TOKENS_PER_CHAR - 1) // _TOKENS_PER_CHAR


def estimate_message_tokens(message: Message) -> int:
    """Estimate the tokens contributed by one native message."""
    tokens = _MESSAGE_OVERHEAD_TOKENS
    for part in message.content:
        if isinstance(part, (TextPart, ReasoningPart)):
            tokens += estimate_text_tokens(part.text)
        elif isinstance(part, ToolCallPart):
            tokens += estimate_text_tokens(part.name) + estimate_text_tokens(part.arguments)
        elif isinstance(part, ImagePart):
            tokens += _IMAGE_TOKEN_ESTIMATE
    return tokens


def _tool_schema(tool: Tool) -> ToolSchema:
    parameters = copy.deepcopy(dict(tool.parameters))
    output_raw = getattr(tool, "output_schema", None)
    output = copy.deepcopy(dict(output_raw)) if isinstance(output_raw, dict) else None
    return ToolSchema(name=tool.name, description=tool.description, parameters=parameters, output=output)


def _estimate_tool_schema(schema: ToolSchema) -> int:
    payload = json.dumps(
        schema.model_dump(exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _TOOL_OVERHEAD_TOKENS + estimate_text_tokens(payload)


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """Immutable request context for exactly one model call."""

    system: Message | None
    messages: tuple[Message, ...]
    tools: tuple[ToolSchema, ...]
    estimated_tokens: int
    source_message_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.system, (Message, type(None))):
            raise ValueError("context plan field 'system' must be a Message or None")
        if not isinstance(self.messages, tuple) or not all(
            isinstance(item, Message) for item in self.messages
        ):
            raise ValueError("context plan field 'messages' must be a tuple of Messages")
        if not isinstance(self.tools, tuple) or not all(isinstance(item, ToolSchema) for item in self.tools):
            raise ValueError("context plan field 'tools' must be a tuple of ToolSchema")
        if not isinstance(self.estimated_tokens, int) or isinstance(self.estimated_tokens, bool):
            raise ValueError("context plan field 'estimated_tokens' must be an integer")
        if self.estimated_tokens < 0:
            raise ValueError("context plan field 'estimated_tokens' must be non-negative")
        expected_ids = (0 if self.system is None else 1) + len(self.messages)
        if not isinstance(self.source_message_ids, tuple) or len(self.source_message_ids) != expected_ids:
            raise ValueError("context plan field 'source_message_ids' must cover system and messages")

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Names of the tools offered in this plan."""
        return tuple(schema.name for schema in self.tools)

    @property
    def empty(self) -> bool:
        """Whether the plan carries no messages (only possible in tests)."""
        return not self.messages


def build_context_plan(
    messages: Sequence[Message],
    *,
    system: Message | None = None,
    tools: Iterable[Tool] = (),
) -> ContextPlan:
    """Build a deterministic :class:`ContextPlan`.

    Args:
        messages: Conversation messages in session order.
        system: Optional system message; it is not part of `messages`.
        tools: Tools offered to the model; their order is preserved.
    """
    message_tuple = tuple(messages)
    schemas = tuple(_tool_schema(tool) for tool in tools)
    source_ids: list[str] = []
    if system is not None:
        source_ids.append(system.id)
    source_ids.extend(message.id for message in message_tuple)
    estimated = 0 if system is None else estimate_message_tokens(system)
    estimated += sum(estimate_message_tokens(message) for message in message_tuple)
    estimated += sum(_estimate_tool_schema(schema) for schema in schemas)
    return ContextPlan(
        system=system,
        messages=message_tuple,
        tools=schemas,
        estimated_tokens=estimated,
        source_message_ids=tuple(source_ids),
    )


__all__ = [
    "ContextPlan",
    "build_context_plan",
    "estimate_message_tokens",
    "estimate_text_tokens",
]
