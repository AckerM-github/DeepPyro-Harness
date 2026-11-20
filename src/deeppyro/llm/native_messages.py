"""Conversion from native core.messages to the OpenAI chat-completions payload.

The wire-free converter (KI-010): it consumes
:class:`deeppyro.core.messages.Message` and
:class:`deeppyro.core.llm.ToolSpec` instead of wire models, so the native provider
never imports `core.wire` or `core.protocols`.

The session log is the source of truth for message shape: assistant messages carry
reasoning + text + tool-call parts, tool results carry a `tool_call_id`, and tool
arguments are JSON strings.  OpenAI-compatible servers expect:

* instruction messages are a single role=system string at the front;
* assistant content is a string (or null when only tool calls follow) and tool calls
  live in tool_calls[].function.arguments as a JSON *string*;
* tool results are role=tool messages keyed by tool_call_id;
* reasoning is replayed through the DeepSeek reasoning_content field (docs/spec/llm.md);
* a provider whose thinking mode rejects replayed assistant turns without that field
  (`requires_reasoning_content`) gets the key even when no reasoning was captured, as an
  empty string; DeepSeek accepts that and requires it on every replayed tool-call turn.

Conversion is deliberately tolerant for everything except tool-call arguments: unknown
content parts are skipped, empty messages are dropped, and developer instructions become
system messages because llama.cpp and the DeepSeek API do not accept role=developer.
Argument strings are validated but forwarded byte-for-byte (re-serialising would
reorder keys/whitespace and break prompt-cache replays); invalid JSON is a request
error.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from deeppyro.core.llm import ModelRequest, ToolSpec
from deeppyro.core.messages import (
    ImagePart,
    Message,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolSource,
)
from deeppyro.llm.attachments import image_part_url
from deeppyro.llm.errors import CODE_BAD_REQUEST, LlmRequestError

__all__ = [
    "build_chat_payload",
    "convert_messages",
    "message_reasoning",
    "message_text",
    "message_to_chat",
    "message_tool_calls",
    "normalize_arguments",
    "tool_to_json",
]


def message_text(message: Message) -> str:
    """Concatenate text parts."""
    return "".join(part.text for part in message.content if isinstance(part, TextPart))


def message_reasoning(message: Message) -> str:
    """Concatenate reasoning parts (assistant-only)."""
    return "".join(part.text for part in message.content if isinstance(part, ReasoningPart))


def message_tool_calls(message: Message) -> list[ToolCallPart]:
    """Tool-call parts of a message, in order."""
    return [part for part in message.content if isinstance(part, ToolCallPart)]


def normalize_arguments(raw: str | None) -> str:
    """Validate tool-call arguments and return the original string unchanged.

    `None` becomes `"{}"`; invalid JSON raises LlmRequestError (code BAD_REQUEST,
    not retryable) instead of being hidden, exactly like the v4 converter.
    """
    if raw is None:
        return "{}"
    try:
        json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise LlmRequestError(
            f"tool-call arguments are not valid JSON: {raw[:120]!r}",
            code=CODE_BAD_REQUEST,
        ) from exc
    return raw


def tool_to_json(tool: ToolSpec) -> dict[str, Any]:
    """Convert a ToolSpec to an OpenAI tool definition."""
    parameters = (
        tool.parameters
        if isinstance(tool.parameters, dict) and tool.parameters
        else {
            "type": "object",
            "properties": {},
        }
    )
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def message_to_chat(
    message: Message,
    *,
    attachments: Any = None,
    requires_reasoning_content: bool = False,
) -> dict[str, Any] | None:
    """Convert one Message; returns None when the message would be empty on the wire.

    When `requires_reasoning_content` is set (a thinking-mode provider such as
    DeepSeek with tools in play), every assistant message carrying tool calls gets a
    `reasoning_content` key, empty when no reasoning was captured.  Strict servers
    reject such a turn with HTTP 400 otherwise; an empty string is accepted.
    """
    role = message.role
    if role in ("system", "developer"):
        text = message_text(message)
        if not text.strip():
            return None
        return {"role": "system", "content": text}
    if role == "user":
        text = message_text(message)
        images = [part for part in message.content if isinstance(part, ImagePart)]
        if not images:
            return {"role": "user", "content": text} if text else None
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for image in images:
            url = image_part_url(
                image,
                attachments=attachments,
                default_media_type=image.media_type,
            )
            if url is not None:
                content.append({"type": "image_url", "image_url": {"url": url}})
        if not content:
            return None
        return {"role": "user", "content": content}
    if role == "assistant":
        text = message_text(message)
        calls = message_tool_calls(message)
        if not text and not calls:
            return None
        payload: dict[str, Any] = {"role": "assistant", "content": text or None}
        reasoning = message_reasoning(message)
        if reasoning or (requires_reasoning_content and calls):
            # DeepSeek replays thinking through reasoning_content; see docs/spec/llm.md.
            # Thinking-mode servers also require the key on a replayed tool-call turn
            # that captured no reasoning, so send an empty string rather than omitting it.
            payload["reasoning_content"] = reasoning
        if calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": normalize_arguments(call.arguments)},
                }
                for call in calls
            ]
        return payload
    if role == "tool":
        call_id = message.tool_call_id
        if not call_id and isinstance(message.source, ToolSource):
            call_id = message.source.call_id
        text = message_text(message)
        return {"role": "tool", "tool_call_id": call_id or "", "content": text}
    return None


def convert_messages(
    messages: Iterable[Message],
    system: Message | None = None,
    *,
    attachments: Any = None,
    requires_reasoning_content: bool = False,
) -> list[dict[str, Any]]:
    """Build the messages array; the system instruction goes first and is not duplicated."""
    converted: list[dict[str, Any]] = []
    has_system = any(message.role == "system" for message in messages)
    if system is not None and not has_system:
        text = message_text(system)
        if text.strip():
            converted.append({"role": "system", "content": text})
    for message in messages:
        payload = message_to_chat(
            message,
            attachments=attachments,
            requires_reasoning_content=requires_reasoning_content,
        )
        if payload is not None:
            converted.append(payload)
    return converted


def build_chat_payload(
    request: ModelRequest,
    *,
    model_ctx: int | None = None,
    attachments: Any = None,
    requires_reasoning_content: bool = False,
) -> dict[str, Any]:
    """Build the JSON body for POST {baseURL}/chat/completions."""
    replay_reasoning = requires_reasoning_content and bool(request.tools)
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": convert_messages(
            request.messages,
            request.system,
            attachments=attachments,
            requires_reasoning_content=replay_reasoning,
        ),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        payload["tools"] = [tool_to_json(tool) for tool in request.tools]
    max_tokens = request.max_tokens
    context_window = request.context_window or model_ctx
    if max_tokens is not None and context_window is not None and max_tokens > context_window:
        max_tokens = context_window
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    return payload
