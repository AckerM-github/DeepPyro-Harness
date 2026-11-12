"""Streaming OpenAI chat-completions provider.

Implements the core.protocols.LlmProvider seam for every server that speaks the OpenAI
chat completions API: DeepSeek official, llama.cpp's llama-server, vLLM, LM Studio, ...

Design highlights (full rationale in docs/spec/llm.md):

* Stream-only: POST /chat/completions with stream=true and
  stream_options={include_usage:true}; SSE frames are decoded incrementally by
  deeppyro.llm.sse.
* Text -> TextDelta, reasoning/reasoning_content -> ReasoningDelta, tool calls ->
  ToolCallStart + ToolCallArgsDelta fragments (arguments are forwarded verbatim).
* A Finish event is always the last event of a successful stream; usage is emitted
  before it when the server sends it.
* Failures before the first content token are raised as LlmError(retryable=...) so
  RetryPolicy can retry them; failures after content produce Finish(kind="error",
  failure={"message","code"}) instead.
* Only the acquisition of the first event is wrapped in RetryPolicy (INTERFACES 5.9).
"""

from __future__ import annotations

import email.utils
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from deeppyro.core.errors import LlmError
from deeppyro.core.ids import new_call_id
from deeppyro.core.protocols import (
    Finish,
    FinishKind,
    LlmRequest,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
)
from deeppyro.llm.errors import (
    CODE_AUTH,
    CODE_BAD_REQUEST,
    CODE_EMPTY_RESPONSE,
    CODE_NOT_FOUND,
    CODE_PROTOCOL,
    CODE_RATE_LIMIT,
    CODE_SERVER,
    CODE_TIMEOUT,
    CODE_TRANSPORT,
    LlmHttpError,
    LlmRequestError,
    failure_dict,
)
from deeppyro.llm.messages import build_chat_payload
from deeppyro.llm.retry import RETRYABLE_CODES, RETRYABLE_HTTP_STATUSES, RetryPolicy
from deeppyro.llm.sse import iter_sse

_CONTENT_EVENTS = (TextDelta, ReasoningDelta, ToolCallStart, ToolCallArgsDelta)

_FINISH_KINDS: dict[str, FinishKind] = {
    "stop": "stop",
    "end_turn": "stop",
    "tool_calls": "tool-calls",
    "tool_use": "tool-calls",
    "function_call": "tool-calls",
    "length": "length",
    "max_tokens": "length",
    "content_filter": "content-filter",
    "error": "error",
}

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


class _ToolCallState:
    """Accumulates one streamed tool call across delta fragments."""

    __slots__ = ("call_id", "name", "started")

    def __init__(self) -> None:
        self.call_id = new_call_id()
        self.name = ""
        self.started = False


class OpenAICompatProvider:
    """LlmProvider for OpenAI-compatible chat completion endpoints."""

    name: str

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        *,
        model_ctx: int | None = None,
        timeout_s: float = 600.0,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_ctx = model_ctx
        self.timeout_s = float(timeout_s)
        self.retry_policy = retry_policy or RetryPolicy()
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    # -- plumbing -----------------------------------------------------------
    @property
    def endpoint(self) -> str:
        """chat/completions URL; a baseURL that already names it is used verbatim."""
        if self.base_url.endswith(_CHAT_COMPLETIONS_SUFFIX):
            return self.base_url
        return self.base_url + _CHAT_COMPLETIONS_SUFFIX

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise LlmRequestError(
                f"provider {self.name!r} is closed",
                provider=self.name,
                code=CODE_PROTOCOL,
            )
        if self._client is None:
            timeout = httpx.Timeout(self.timeout_s, connect=min(30.0, self.timeout_s))
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json", "accept": "text/event-stream"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def aclose(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        if self._closed:
            return
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # -- public API ---------------------------------------------------------
    async def stream(self, req: LlmRequest) -> AsyncIterator[StreamEvent]:
        """Stream one completion as core.protocols.StreamEvent values."""
        payload = build_chat_payload(req, model_ctx=self.model_ctx)
        state: dict[str, AsyncIterator[StreamEvent] | None] = {"gen": None}

        async def fetch_first() -> StreamEvent:
            gen = self._attempt(payload)
            state["gen"] = gen
            try:
                return await anext(gen)
            except StopAsyncIteration as exc:
                raise LlmRequestError(
                    f"{self.name}: stream produced no events",
                    provider=self.name,
                    retryable=True,
                    code=CODE_EMPTY_RESPONSE,
                ) from exc

        async with self.retry_policy as retry:
            first = await retry.run(fetch_first)
        yield first
        gen = state["gen"]
        if gen is None:  # pragma: no cover - defensive: fetch_first always sets it
            return
        async for event in gen:
            yield event

    # -- one HTTP attempt ---------------------------------------------------
    async def _attempt(self, payload: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        client = self._ensure_client()
        produced = False
        finished = False
        try:
            async with client.stream(
                "POST",
                self.endpoint,
                json=payload,
                headers=self._headers(),
            ) as response:
                if response.status_code >= 400:
                    body = await _read_body(response)
                    raise self._http_error(response, body)
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type and "event-stream" not in content_type:
                    body = await _read_body(response)
                    raise LlmRequestError(
                        f"{self.name}: expected an event stream, got {content_type!r}: {_shorten(body)}",
                        provider=self.name,
                        code=CODE_PROTOCOL,
                    )
                async for event in self._consume(response):
                    if isinstance(event, Finish):
                        finished = True
                    if isinstance(event, _CONTENT_EVENTS):
                        produced = True
                    yield event
        except LlmError as exc:
            if not produced:
                raise
            if finished:
                return
            yield Finish(kind="error", failure=_failure_of(exc))
        except httpx.HTTPError as exc:
            error = LlmRequestError(
                f"{self.name}: transport error: {type(exc).__name__}: {exc}",
                provider=self.name,
                retryable=True,
                code=CODE_TIMEOUT if isinstance(exc, httpx.TimeoutException) else CODE_TRANSPORT,
            )
            if not produced:
                raise error from exc
            if not finished:
                yield Finish(kind="error", failure=error.failure())

    def _http_error(self, response: httpx.Response, body: str) -> LlmHttpError:
        status = response.status_code
        retryable = status in RETRYABLE_HTTP_STATUSES or 500 <= status < 600
        detail = _extract_error_message(body)
        message = f"{self.name}: HTTP {status}" + (f": {detail}" if detail else "")
        return LlmHttpError(
            message,
            provider=self.name,
            status=status,
            retryable=retryable,
            code=_status_code(status),
            retry_after=_parse_retry_after(response.headers.get("retry-after")),
            body=body,
        )

    # -- SSE consumption ----------------------------------------------------
    async def _consume(self, response: httpx.Response) -> AsyncIterator[StreamEvent]:
        produced = False
        finish_reason: str | None = None
        done = False
        calls: dict[int, _ToolCallState] = {}
        async for sse in iter_sse(response.aiter_bytes()):
            data = sse.data.strip()
            if not data:
                continue
            if data == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as exc:
                raise LlmRequestError(
                    f"{self.name}: invalid JSON in SSE data: {_shorten(data)}",
                    provider=self.name,
                    code=CODE_PROTOCOL,
                ) from exc
            if not isinstance(chunk, dict):
                raise LlmRequestError(
                    f"{self.name}: SSE data is not a JSON object",
                    provider=self.name,
                    code=CODE_PROTOCOL,
                )
            error = chunk.get("error")
            if error:
                raise _stream_error(self.name, error)
            choices = chunk.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta")
                    if isinstance(delta, dict):
                        for event in _delta_events(delta, calls):
                            if isinstance(event, _CONTENT_EVENTS):
                                produced = True
                            yield event
                    reason = choice.get("finish_reason")
                    if isinstance(reason, str) and reason:
                        finish_reason = reason
            usage = chunk.get("usage")
            if isinstance(usage, dict) and usage:
                yield _usage_event(usage)
        if finish_reason is None and not done:
            raise LlmRequestError(
                f"{self.name}: stream ended without finish_reason",
                provider=self.name,
                retryable=True,
                code=CODE_PROTOCOL if produced else CODE_EMPTY_RESPONSE,
            )
        kind: FinishKind
        if finish_reason is not None:
            kind = _map_finish(finish_reason)
        else:
            kind = "tool-calls" if calls else "stop"
        if not produced and kind in ("stop", "tool-calls"):
            raise LlmRequestError(
                f"{self.name}: provider returned an empty response",
                provider=self.name,
                retryable=True,
                code=CODE_EMPTY_RESPONSE,
            )
        yield Finish(kind=kind)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _delta_events(
    delta: dict[str, Any],
    calls: dict[int, _ToolCallState],
) -> Iterator[StreamEvent]:
    """Translate one choices[].delta object into stream events."""
    reasoning = delta.get("reasoning_content")
    if not isinstance(reasoning, str):
        reasoning = delta.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        yield ReasoningDelta(reasoning)
    content = delta.get("content")
    if isinstance(content, str) and content:
        yield TextDelta(content)
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for raw in tool_calls:
        if not isinstance(raw, dict):
            continue
        index = raw.get("index")
        if not isinstance(index, int):
            index = len(calls)
        state = calls.get(index)
        if state is None:
            state = _ToolCallState()
            calls[index] = state
        raw_id = raw.get("id")
        if isinstance(raw_id, str) and raw_id and not state.started:
            state.call_id = raw_id
        function = raw.get("function")
        arguments: str | None = None
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                state.name += name
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, str):
                arguments = raw_arguments
        if not state.started and (state.name or arguments):
            state.started = True
            yield ToolCallStart(call_id=state.call_id, name=state.name)
        if arguments:
            yield ToolCallArgsDelta(call_id=state.call_id, delta=arguments)


def _map_finish(reason: str) -> FinishKind:
    return _FINISH_KINDS.get(reason, "stop")


def _usage_event(raw: dict[str, Any]) -> Usage:
    def as_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    input_tokens = as_int(raw.get("prompt_tokens"))
    output_tokens = as_int(raw.get("completion_tokens"))
    total_tokens = as_int(raw.get("total_tokens")) or input_tokens + output_tokens
    cached: int | None = None
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        cached = as_int(details.get("cached_tokens"))
    elif raw.get("prompt_cache_hit_tokens") is not None:
        cached = as_int(raw.get("prompt_cache_hit_tokens"))
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached,
    )


def _status_code(status: int) -> str:
    if status == 429:
        return CODE_RATE_LIMIT
    if status == 408:
        return CODE_TIMEOUT
    if status in (401, 403):
        return CODE_AUTH
    if status == 404:
        return CODE_NOT_FOUND
    if 500 <= status < 600:
        return CODE_SERVER
    if 400 <= status < 500:
        return CODE_BAD_REQUEST
    return f"HTTP_{status}"


def _normalize_error_code(raw: str) -> str:
    value = raw.strip().lower()
    if "rate" in value and "limit" in value:
        return CODE_RATE_LIMIT
    if "timeout" in value or value == "408":
        return CODE_TIMEOUT
    if "server" in value or "internal" in value or "overload" in value or "unavailable" in value:
        return CODE_SERVER
    if value in ("auth", "authentication", "invalid_api_key", "401", "403"):
        return CODE_AUTH
    if value in ("bad_request", "invalid_request", "400", "422"):
        return CODE_BAD_REQUEST
    if value:
        return raw.strip()
    return CODE_PROTOCOL


def _stream_error(provider: str, error: Any) -> LlmRequestError:
    if isinstance(error, dict):
        message = error.get("message") or json.dumps(error, ensure_ascii=False)
        raw_code = error.get("code") or error.get("type") or ""
    else:
        message = str(error)
        raw_code = ""
    code = _normalize_error_code(str(raw_code))
    return LlmRequestError(
        f"{provider}: provider stream error: {message}",
        provider=provider,
        code=code,
        retryable=code in RETRYABLE_CODES,
    )


def _failure_of(exc: LlmError) -> dict[str, Any]:
    return failure_dict(getattr(exc, "code", "") or CODE_PROTOCOL, str(exc))


async def _read_body(response: httpx.Response) -> str:
    try:
        raw = await response.aread()
    except httpx.HTTPError:
        return ""
    return raw.decode("utf-8", "replace")


def _shorten(text: str, limit: int = 400) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _extract_error_message(body: str) -> str:
    if not body.strip():
        return ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return _shorten(body)
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
            if isinstance(message, str) and message:
                return message
        for key in ("message", "detail", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
    return _shorten(body)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After: delta-seconds or an HTTP date."""
    if not value:
        return None
    candidate = value.strip()
    try:
        return max(0.0, float(candidate))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


__all__ = ["OpenAICompatProvider"]
