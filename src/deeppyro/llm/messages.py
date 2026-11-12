"""Conversion from core wire messages to the OpenAI chat-completions payload.

The session log is the source of truth for message shape (see fixtures:: assistant
messages carry reasoning + text + tool-call parts, tool results carry toolCallId, and
arguments are JSON strings).  OpenAI-compatible servers expect a different shape:

* instruction messages are a single role=system string at the front;
* assistant content is a string (or null when only tool calls follow) and tool calls live
  in tool_calls[].function.arguments as a JSON *string*;
* tool results are role=tool messages keyed by tool_call_id;
* reasoning is replayed through the DeepSeek reasoning_content field (see docs/spec/llm.md).

Conversion is deliberately tolerant: unknown content parts are skipped, empty messages are
dropped, and developer instructions become system messages because llama.cpp and the
DeepSeek API do not accept role=developer.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from deeppyro.core.protocols import LlmRequest, ToolSchema
from deeppyro.core.wire import (
    ImagePart,
    Message,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)


def _part_type(part: Any) -> str:
    if isinstance(part, dict):
        return str(part.get("type", ""))
    return str(getattr(part, "type", ""))


def _part_text(part: Any) -> str:
    if isinstance(part, dict):
        value = part.get("text", "")
    else:
        value = getattr(part, "text", "")
    return value if isinstance(value, str) else ""


def message_text(message: Message) -> str:
    """Concatenate text parts without relying on pydantic union coercion."""
    return "".join(_part_text(part) for part in message.content if _part_type(part) == "text")


def message_reasoning(message: Message) -> str:
    return "".join(_part_text(part) for part in message.content if _part_type(part) == "reasoning")


def message_tool_calls(message: Message) -> list[ToolCallPart]:
    """Tool-call parts of a message (pydantic models, when present)."""
    calls: list[ToolCallPart] = []
    for part in message.content:
        if isinstance(part, ToolCallPart):
            calls.append(part)
        elif isinstance(part, dict) and part.get("type") == "tool-call":
            calls.append(ToolCallPart.model_validate(part))
    return calls


def normalize_arguments(raw: str | None) -> str:
    """Return tool-call arguments as a canonical JSON string.

    The wire contract stores arguments as a JSON string (fixtures tool/call record:
    arguments is stringified JSON).  OpenAI also wants a string, so we only validate and
    re-serialise; malformed input is forwarded unchanged instead of failing the request.
    """
    if raw is None:
        return "{}"
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if isinstance(parsed, str):
        return raw
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def image_part_url(part: ImagePart | dict[str, Any]) -> str | None:
    """Resolve an ImagePart to a URL the API accepts.

    Supported shapes: a ready data:/http(s) URL string, or a dict with url, or a dict with
    base64 data plus mimeType/mediaType.  A bare attachment id cannot be resolved at this
    layer, so such parts are skipped (documented in docs/spec/llm.md).
    """
    attachment: Any = part.get("attachment") if isinstance(part, dict) else part.attachment
    if isinstance(attachment, str):
        if attachment.startswith(("data:", "http://", "https://")):
            return attachment
        return None
    if isinstance(attachment, dict):
        url = attachment.get("url")
        if isinstance(url, str) and url:
            return url
        data = attachment.get("data") or attachment.get("base64")
        mime = attachment.get("mimeType") or attachment.get("mediaType") or "image/png"
        if isinstance(data, str) and data:
            return f"data:{mime};base64,{data}"
    return None


def tool_to_json(tool: ToolSchema) -> dict[str, Any]:
    """Convert a ToolSchema to an OpenAI tool definition."""
    parameters = tool.parameters if isinstance(tool.parameters, dict) and tool.parameters else {
        "type": "object",
        "properties": {},
    }
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": parameters,
        },
    }


def message_to_chat(message: Message) -> dict[str, Any] | None:
    """Convert one Message; returns None when the message would be empty on the wire."""
    role = message.role
    if role in ("system", "developer"):
        text = message_text(message)
        if not text.strip():
            return None
        return {"role": "system", "content": text}
    if role == "user":
        text = message_text(message)
        images = [part for part in message.content if _part_type(part) == "image"]
        if not images:
            return {"role": "user", "content": text} if text else None
        content: list[dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": text})
        for image in images:
            url = image_part_url(image)
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
        if reasoning:
            # DeepSeek replays thinking through reasoning_content; see docs/spec/llm.md.
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
        call_id = message.toolCallId
        if not call_id and isinstance(message.source, dict):
            candidate = message.source.get("callId")
            call_id = candidate if isinstance(candidate, str) else None
        text = message_text(message)
        return {"role": "tool", "tool_call_id": call_id or "", "content": text}
    return None


def convert_messages(messages: Iterable[Message], system: str | None = None) -> list[dict[str, Any]]:
    """Build the messages array; the system instruction goes first and is not duplicated."""
    converted: list[dict[str, Any]] = []
    has_system = any(message.role == "system" for message in messages)
    if system and not has_system:
        converted.append({"role": "system", "content": system})
    for message in messages:
        payload = message_to_chat(message)
        if payload is not None:
            converted.append(payload)
    return converted


def build_chat_payload(req: LlmRequest, *, model_ctx: int | None = None) -> dict[str, Any]:
    """Build the JSON body for POST {baseURL}/chat/completions."""
    payload: dict[str, Any] = {
        "model": req.model,
        "messages": convert_messages(req.messages, req.system),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if req.tools:
        payload["tools"] = [tool_to_json(tool) for tool in req.tools]
    max_tokens = req.max_tokens
    context_window = req.context_window or model_ctx
    if max_tokens is not None and context_window is not None and max_tokens > context_window:
        max_tokens = context_window
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    return payload


__all__ = [
    "ReasoningPart",
    "TextPart",
    "build_chat_payload",
    "convert_messages",
    "image_part_url",
    "message_reasoning",
    "message_text",
    "message_to_chat",
    "message_tool_calls",
    "normalize_arguments",
    "tool_to_json",
]
