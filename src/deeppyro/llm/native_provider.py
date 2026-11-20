"""Native streaming OpenAI chat-completions provider (KI-010).

The wire-free `/chat/completions` transport:
it implements :class:`deeppyro.core.llm.LlmProvider` and emits native stream
events instead of the legacy `core.protocols` ones, so the native agent loop never
depends on `core.wire`-shaped types.

Design highlights (full rationale in docs/spec/llm.md):

* Stream-only: POST /chat/completions with stream=true and
  stream_options={include_usage:true}; SSE frames are decoded incrementally by
  deeppyro.llm.sse.
* Text -> TextDelta, reasoning/reasoning_content -> ReasoningDelta, tool calls ->
  ToolCallStart + ToolCallArgsDelta fragments (arguments are forwarded verbatim).
* A Finish event is always the last event of a successful stream; usage is emitted
  before it when the server sends it.
* Failures before the first content token are raised as LlmRequestError(retryable=...)
  so RetryPolicy can retry them; failures after content produce
  Finish(StopReason.ERROR, message=..., code=...) instead.
* Only the acquisition of the first event is wrapped in RetryPolicy (INTERFACES 5.9).
  Every scheduled retry is reported as a RetryScheduled event so the agent loop can
  record a truthful retry_count in the trace.
* The provider receives an already-resolved API key; ProviderConfig.resolve_api_key
  stays the only secret reader.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx

from deeppyro.core.errors import LlmError
from deeppyro.core.ids import new_call_id
from deeppyro.core.llm import (
    Finish,
    ModelRequest,
    ReasoningDelta,
    RetryScheduled,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
    UsageEvent,
)
from deeppyro.llm.catalog import ModelInfo, parse_model_listing
from deeppyro.llm.discovery import fetch_listing, openai_listing_url
from deeppyro.llm.errors import (
    CODE_EMPTY_RESPONSE,
    CODE_PROTOCOL,
    CODE_TIMEOUT,
    CODE_TRANSPORT,
    LlmHttpError,
    LlmRequestError,
    http_error,
    shorten,
)
from deeppyro.llm.http import build_client, headers
from deeppyro.llm.native_messages import build_chat_payload
from deeppyro.llm.retry import RETRYABLE_CODES, RetryPolicy
from deeppyro.llm.sse import iter_sse

_CONTENT_EVENTS = (TextDelta, ReasoningDelta, ToolCallStart, ToolCallArgsDelta)

_FINISH_REASONS: dict[str, StopReason] = {
    "stop": StopReason.STOP,
    "end_turn": StopReason.STOP,
    "tool_calls": StopReason.TOOL_CALLS,
    "tool_use": StopReason.TOOL_CALLS,
    "function_call": StopReason.TOOL_CALLS,
    "length": StopReason.LENGTH,
    "max_tokens": StopReason.LENGTH,
    "content_filter": StopReason.CONTENT_FILTER,
    "error": StopReason.ERROR,
}

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


class _ToolCallState:
    """Accumulates one streamed tool call across delta fragments."""

    __slots__ = ("call_id", "name", "started")

    def __init__(self) -> None:
        self.call_id = new_call_id()
        self.name = ""
        self.started = False


class NativeOpenAICompatProvider:
    """Native LlmProvider for OpenAI-compatible chat completion endpoints."""

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
        attachments: Any = None,
        requires_reasoning_content: bool = False,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_ctx = model_ctx
        self.timeout_s = float(timeout_s)
        self.retry_policy = retry_policy or RetryPolicy()
        # Optional duck-typed attachment resolver (see llm/attachments.py); the registry
        # does not inject one yet, so bare attachment ids are dropped when no resolver is
        # supplied.
        self.attachments = attachments
        #: Thinking-mode servers (DeepSeek, see docs/spec/llm.md) reject replayed
        #: tool-call turns that omit reasoning_content, so the field is written even
        #: when no reasoning was captured.  Off by default: other OpenAI-compatible
        #: endpoints may not know the field.
        self.requires_reasoning_content = bool(requires_reasoning_content)
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    # -- plumbing -----------------------------------------------------------
    @property
    def endpoint(self) -> str:
        """chat/completions URL; a base_url that already names it is used verbatim."""
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
            self._client = build_client(self.timeout_s)
        return self._client

    def _headers(self) -> dict[str, str]:
        return headers(self.api_key)

    async def aclose(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        if self._closed:
            return
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # -- public API ---------------------------------------------------------
    async def list_models(self) -> list[ModelInfo]:
        """Discover the models this endpoint advertises (GET <base>/models); 404/405 -> []."""
        client = self._ensure_client()
        request_headers = {"accept": "application/json"}
        if self.api_key:
            request_headers["authorization"] = f"Bearer {self.api_key}"
        url = openai_listing_url(self.base_url)
        try:
            payload = await fetch_listing(client, url, headers=request_headers, provider=self.name)
        except LlmHttpError as exc:
            if exc.status in (404, 405):
                return []
            raise
        return parse_model_listing(self.name, payload, source=f"listing from {url}")

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Stream one completion as native core.llm.StreamEvent values."""
        payload = build_chat_payload(
            request,
            model_ctx=self.model_ctx,
            attachments=self.attachments,
            requires_reasoning_content=self.requires_reasoning_content,
        )
        state: dict[str, AsyncIterator[StreamEvent] | None] = {"gen": None}
        pending: list[RetryScheduled] = []

        def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            code = getattr(exc, "code", None)
            pending.append(
                RetryScheduled(
                    attempt=attempt,
                    code=code if isinstance(code, str) else None,
                    delay_s=delay,
                )
            )

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

        try:
            async with self.retry_policy as retry:
                first = await retry.run(fetch_first, on_retry=on_retry)
        except BaseException:
            for event in pending:
                yield event
            raise
        for event in pending:
            yield event
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
                    raise http_error(
                        self.name,
                        response.status_code,
                        headers=response.headers,
                        body=body,
                    )
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type and "event-stream" not in content_type:
                    body = await _read_body(response)
                    raise LlmRequestError(
                        f"{self.name}: expected an event stream, got {content_type!r}: {shorten(body)}",
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
            yield Finish(
                StopReason.ERROR,
                message=str(exc),
                code=getattr(exc, "code", None) or CODE_PROTOCOL,
            )
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
                yield Finish(StopReason.ERROR, message=str(error), code=error.code)

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
                    f"{self.name}: invalid JSON in SSE data: {shorten(data)}",
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
                yield UsageEvent(_usage_event(usage))
        if finish_reason is None and not done:
            raise LlmRequestError(
                f"{self.name}: stream ended without finish_reason",
                provider=self.name,
                retryable=True,
                code=CODE_PROTOCOL if produced else CODE_EMPTY_RESPONSE,
            )
        stop_reason = (
            _map_finish(finish_reason)
            if finish_reason is not None
            else StopReason.TOOL_CALLS
            if calls
            else StopReason.STOP
        )
        if not produced and stop_reason in (StopReason.STOP, StopReason.TOOL_CALLS):
            raise LlmRequestError(
                f"{self.name}: provider returned an empty response",
                provider=self.name,
                retryable=True,
                code=CODE_EMPTY_RESPONSE,
            )
        yield Finish(stop_reason)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _delta_events(
    delta: dict[str, Any],
    calls: dict[int, _ToolCallState],
) -> Iterator[StreamEvent]:
    """Translate one choices[].delta object into native stream events."""
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
            yield ToolCallStart(call_id=state.call_id, name=state.name, index=index)
        if arguments:
            yield ToolCallArgsDelta(call_id=state.call_id, delta=arguments, index=index)


def _map_finish(reason: str) -> StopReason:
    return _FINISH_REASONS.get(reason, StopReason.STOP)


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


def _normalize_error_code(raw: str) -> str:
    value = raw.strip().lower()
    if "rate" in value and "limit" in value:
        return "RATE_LIMIT"
    if "timeout" in value or value == "408":
        return CODE_TIMEOUT
    if "server" in value or "internal" in value or "overload" in value or "unavailable" in value:
        return "SERVER"
    if value in ("auth", "authentication", "invalid_api_key", "401", "403"):
        return "AUTH"
    if value in ("bad_request", "invalid_request", "400", "422"):
        return "BAD_REQUEST"
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


async def _read_body(response: httpx.Response) -> str:
    try:
        raw = await response.aread()
    except httpx.HTTPError:
        return ""
    return raw.decode("utf-8", "replace")


__all__ = ["NativeOpenAICompatProvider"]
