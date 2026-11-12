"""Shared helpers: fixture-shaped SSE documents and wire messages.

The stream shapes mirror fixtures/records/by-type/assistant_message.jsonl: an assistant
answer is reasoning chunks, then text chunks, then an optional usage chunk and a finish
chunk, terminated by [DONE].  Tool calls arrive as OpenAI tool_calls fragments.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import httpx

from deeppyro.core.protocols import LlmRequest, ToolSchema
from deeppyro.core.wire import ImagePart, Message, ReasoningPart, TextPart, ToolCallPart

SSE_HEADERS = {"content-type": "text/event-stream"}


def sse_doc(chunks: Iterable[Any], *, done: bool = True) -> str:
    """Render chunk dicts (or raw strings) as an SSE document."""
    body = []
    for chunk in chunks:
        payload = chunk if isinstance(chunk, str) else json.dumps(chunk, ensure_ascii=False)
        body.append("data: " + payload + "\n\n")
    if done:
        body.append("data: [DONE]\n\n")
    return "".join(body)


def chat_chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def usage_chunk(
    input_tokens: int = 10,
    output_tokens: int = 5,
    *,
    total_tokens: int | None = None,
    cached_tokens: int | None = None,
    deepseek_cache_field: bool = False,
) -> dict[str, Any]:
    total = total_tokens if total_tokens is not None else input_tokens + output_tokens
    usage: dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total,
    }
    if cached_tokens is not None:
        if deepseek_cache_field:
            usage["prompt_cache_hit_tokens"] = cached_tokens
        else:
            usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
    return {"id": "chatcmpl-test", "object": "chat.completion.chunk", "choices": [], "usage": usage}


def text_stream(*parts: str, finish: str = "stop", usage: bool = False) -> str:
    """A plain assistant answer with one content chunk per part."""
    chunks: list[Any] = [chat_chunk({"role": "assistant"})]
    chunks.extend(chat_chunk({"content": part}) for part in parts)
    if usage:
        chunks.append(usage_chunk())
    chunks.append(chat_chunk({}, finish))
    return sse_doc(chunks)


def sse_response(body: str, *, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    merged = dict(SSE_HEADERS)
    if headers:
        merged.update(headers)
    return httpx.Response(status, text=body, headers=merged)


def user_message(text: str) -> Message:
    return Message(role="user", content=[TextPart(text=text)])


def make_request(
    *,
    model: str = "deepseek-chat",
    messages: list[Message] | None = None,
    system: str | None = None,
    tools: list[ToolSchema] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    context_window: int | None = None,
    session_id: str | None = None,
) -> LlmRequest:
    return LlmRequest(
        provider="test",
        model=model,
        messages=messages if messages is not None else [user_message("hi")],
        system=system,
        tools=tools or [],
        max_tokens=max_tokens,
        temperature=temperature,
        context_window=context_window,
        session_id=session_id,
    )


def text_tool(name: str = "read", description: str = "Read a file") -> ToolSchema:
    return ToolSchema(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )


__all__ = [
    "SSE_HEADERS",
    "ImagePart",
    "Message",
    "ReasoningPart",
    "TextPart",
    "ToolCallPart",
    "chat_chunk",
    "make_request",
    "sse_doc",
    "sse_response",
    "text_stream",
    "text_tool",
    "usage_chunk",
    "user_message",
]
