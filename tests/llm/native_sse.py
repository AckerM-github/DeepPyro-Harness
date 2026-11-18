"""Wire-free SSE fixtures for the native provider tests.

Only standard library + httpx types: the native test suites must not import any
removed wire shapes. The stream shapes mirror the recorded native provider
transcripts: reasoning chunks, then text chunks, then an optional usage chunk and a
finish chunk, terminated by [DONE].
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

import httpx

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


__all__ = [
    "SSE_HEADERS",
    "chat_chunk",
    "sse_doc",
    "sse_response",
    "text_stream",
    "usage_chunk",
]
