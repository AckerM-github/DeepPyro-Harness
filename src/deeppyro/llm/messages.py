"""Conversion from core wire messages to the OpenAI chat-completions payload.

The session log is the source of truth for message shape (see fixtures:: assistant
messages carry reasoning + text + tool-call parts, tool results carry toolCallId, and
arguments are JSON strings).  OpenAI-compatible servers expect a different shape:

* instruction messages are a single role=system string at the front;
* assistant content is a string (or null when only tool calls follow) and tool calls live
  in tool_calls[].function.arguments as a JSON *string*;
* tool results are role=tool messages keyed by tool_call_id;
* reasoning is replayed through the DeepSeek reasoning_content field (see docs/spec/llm.md).

Conversion is deliberately tolerant for everything except tool-call arguments: unknown
content parts are skipped, empty messages are dropped, and developer instructions become
system messages because llama.cpp and the DeepSeek API do not accept role=developer.
Argument strings are validated but forwarded byte-for-byte (json.loads would otherwise
reorder keys/whitespace and break prompt-cache replays); invalid JSON is a request error.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from deeppyro.core.protocols import LlmRequest, ToolSchema
from deeppyro.core.wire import (
    ImagePart,
    Message,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)
from deeppyro.llm.errors import CODE_BAD_REQUEST, LlmRequestError


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
    """Validate tool-call arguments and return the original string unchanged.

    The session log stores arguments as a JSON string (fixtures tool/call record:
    arguments is stringified JSON) and OpenAI also wants a string, so the bytes are
    forwarded verbatim: re-serialising would reorder keys/whitespace and make prompt-cache
    replays diverge.  A string that is not valid JSON cannot be sent to the API at all, so
    it raises LlmRequestError (code BAD_REQUEST, not retryable) instead of being hidden.
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


def image_part_url(part: ImagePart | dict[str, Any], *, attachments: Any = None) -> str | None:
    """Resolve an ImagePart to a URL the API accepts.

    Supported shapes:

    * a ready data:/http(s) URL string (forwarded unchanged);
    * a dict with url, or a dict with base64 data plus mimeType/mediaType;
    * a bare attachment reference (string id or reference dict) resolved through the
      optional *attachments* resolver.

    The resolver is duck-typed after the attachment store service (see
    docs/spec/attachments.md): it only needs `read_image_bytes(ref) -> bytes` or
    `image_host_path(ref) -> Path`; an optional `lookup(attachment_id)` maps a bare id
    to its reference dict.  Resolved bytes become an inline data URL in memory -- the
    durable session log never stores base64.

    The old one-argument call keeps working: without a resolver a bare id is skipped and
    the part is dropped, exactly as before.  Resolver failures degrade to None so a
    broken attachment can never surface as a provider exception.
    """
    attachment: Any = part.get("attachment") if isinstance(part, dict) else part.attachment
    if isinstance(attachment, str):
        if attachment.startswith(("data:", "http://", "https://")):
            return attachment
        if attachments is None:
            return None
        ref = _lookup_attachment(attachments, attachment)
        return _resolved_data_url(attachments, ref if ref is not None else attachment)
    if isinstance(attachment, dict):
        url = attachment.get("url")
        if isinstance(url, str) and url:
            return url
        data = attachment.get("data") or attachment.get("base64")
        mime = attachment.get("mimeType") or attachment.get("mediaType") or "image/png"
        if isinstance(data, str) and data:
            return f"data:{mime};base64,{data}"
        if attachments is not None:
            return _resolved_data_url(attachments, attachment)
    return None


def _lookup_attachment(attachments: Any, attachment_id: str) -> Any | None:
    """Map a bare attachment id to its reference; None when the resolver cannot."""
    lookup = getattr(attachments, "lookup", None)
    if not callable(lookup):
        return None
    try:
        return lookup(attachment_id)
    except Exception:  # noqa: BLE001 - an unresolvable attachment is simply skipped
        return None


def _resolved_data_url(attachments: Any, ref: Any) -> str | None:
    """Read one resolved reference as bytes and encode it as an inline data URL."""
    try:
        raw = _read_attachment_bytes(attachments, ref)
    except Exception:  # noqa: BLE001 - provider conversion must never fail on a bad ref
        return None
    if not raw:
        return None
    mime = _ref_media_type(ref) or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _read_attachment_bytes(attachments: Any, ref: Any) -> bytes | None:
    reader = getattr(attachments, "read_image_bytes", None)
    if callable(reader):
        data = reader(ref)
        if isinstance(data, (bytes, bytearray, memoryview)):
            return bytes(data)
        return None
    host_path = getattr(attachments, "image_host_path", None)
    if callable(host_path):
        path = host_path(ref)
        if path is None:
            return None
        return Path(path).read_bytes()
    return None


def _ref_media_type(ref: Any) -> str | None:
    if isinstance(ref, dict):
        value = ref.get("mediaType") or ref.get("mimeType")
    else:
        value = getattr(ref, "mediaType", None)
    return value if isinstance(value, str) and value else None


def tool_to_json(tool: ToolSchema) -> dict[str, Any]:
    """Convert a ToolSchema to an OpenAI tool definition."""
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
