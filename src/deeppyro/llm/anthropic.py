"""Streaming Anthropic Messages provider.

Implements core.protocols.LlmProvider for POST <base>/v1/messages with stream=true.
The wire format differs from OpenAI chat-completions in several places (docs/spec/llm.md
section 14):

* instructions travel in a top-level `system` string, tool results are `tool_result`
  blocks inside user messages, and tool_use inputs are JSON objects rather than strings;
* the stream is a typed event protocol (message_start / content_block_start /
  content_block_delta / content_block_stop / message_delta / message_stop) instead of
  OpenAI's choices[].delta frames; SSE framing itself is shared via deeppyro.llm.sse;
* usage is split between message_start and message_delta and reports cache reads and
  cache writes separately, which this provider folds into core Usage.

Contract mapping (INTERFACES 5): text_delta -> TextDelta, thinking_delta -> ReasoningDelta,
tool_use -> ToolCallStart + ToolCallArgsDelta (input_json_delta fragments forwarded
verbatim), Usage before the final Finish. A stream that ends without message_stop maps to
Finish(error, code STREAM_CLOSED) when content was already emitted and to a retryable
LlmError(STREAM_CLOSED) before that. Transport failures and provider error events inside
the stream follow the same produced/not-produced split as OpenAICompatProvider.

P1 scope (Lead decision, docs/plan/p1-providers.md): thinking blocks are streamed as
ReasoningDelta but history reasoning is not replayed with a signature, and the baseURL must
be configured explicitly because Anthropic has no default endpoint here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from deeppyro.core.errors import ConfigError, LlmError
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
from deeppyro.llm.catalog import ModelInfo, parse_model_listing
from deeppyro.llm.discovery import (
    DEFAULT_ANTHROPIC_VERSION,
    anthropic_listing_url,
    fetch_listing,
)
from deeppyro.llm.errors import (
    CODE_EMPTY_RESPONSE,
    CODE_PROTOCOL,
    CODE_STREAM_CLOSED,
    CODE_TIMEOUT,
    CODE_TRANSPORT,
    LlmHttpError,
    LlmRequestError,
    anthropic_error,
    failure_dict,
    shorten,
)
from deeppyro.llm.messages_anthropic import REASONING_EFFORTS, build_anthropic_payload
from deeppyro.llm.retry import RetryPolicy
from deeppyro.llm.sse import iter_sse

_CONTENT_EVENTS = (TextDelta, ReasoningDelta, ToolCallStart, ToolCallArgsDelta)

#: Anthropic stop_reason -> core FinishKind. pause_turn means "the server paused a long
#: turn; send the conversation back to continue", which the agent loop treats as a normal
#: stop (P1 has no continuation machinery).
_STOP_REASONS: dict[str, FinishKind] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool-calls",
    "max_tokens": "length",
    "refusal": "content-filter",
    "pause_turn": "stop",
}

_MESSAGES_SUFFIX = "/messages"


class _BlockState:
    """State of one open content block (text, thinking or tool_use)."""

    __slots__ = ("args_text", "call_id", "kind", "name")

    def __init__(
        self,
        kind: str,
        *,
        call_id: str = "",
        name: str = "",
        args_text: str = "",
    ) -> None:
        self.kind = kind
        self.call_id = call_id
        self.name = name
        self.args_text = args_text


class AnthropicProvider:
    """LlmProvider for the Anthropic Messages API (and compatibles)."""

    name: str

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None,
        *,
        model_ctx: int | None = None,
        model_max_tokens: int | None = None,
        reasoning_effort: str = "off",
        timeout_s: float = 600.0,
        idle_timeout_s: float = 300.0,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        if reasoning_effort not in REASONING_EFFORTS:
            raise ConfigError(
                f"provider {name!r}: unsupported reasoning effort {reasoning_effort!r}; "
                f"expected one of {REASONING_EFFORTS}"
            )
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_ctx = model_ctx
        self.model_max_tokens = model_max_tokens
        self.reasoning_effort = reasoning_effort
        self.timeout_s = float(timeout_s)
        self.idle_timeout_s = float(idle_timeout_s)
        self.retry_policy = retry_policy or RetryPolicy()
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    # -- plumbing -----------------------------------------------------------
    @property
    def messages_url(self) -> str:
        """POST target; a baseURL that already names the endpoint is used verbatim."""
        base = self.base_url
        if base.endswith(_MESSAGES_SUFFIX):
            return base
        if base.endswith("/v1"):
            return base + _MESSAGES_SUFFIX
        return base + "/v1" + _MESSAGES_SUFFIX

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise LlmRequestError(
                f"provider {self.name!r} is closed",
                provider=self.name,
                code=CODE_PROTOCOL,
            )
        if self._client is None:
            timeout = httpx.Timeout(
                self.timeout_s,
                connect=min(30.0, self.timeout_s),
                # A stalled stream is detected by the read (idle) timeout, not the total one.
                read=self.idle_timeout_s,
            )
            self._client = httpx.AsyncClient(timeout=timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "text/event-stream",
            "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
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
    async def list_models(self) -> list[ModelInfo]:
        """Discover models via GET <base>/v1/models (Anthropic paginated listing).

        Discovery is advisory metadata (registry merges it over the configured catalog).
        A 404/405 means the endpoint or the listing is not available, which is not an
        error, so it maps to an empty list and the registry falls back to config.
        """
        client = self._ensure_client()
        headers = {
            "accept": "application/json",
            "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        url = anthropic_listing_url(self.base_url)
        try:
            payload = await fetch_listing(client, url, headers=headers, provider=self.name)
        except LlmHttpError as exc:
            if exc.status in (404, 405):
                return []
            raise
        return parse_model_listing(self.name, payload, source=f"listing from {url}")

    async def stream(self, req: LlmRequest) -> AsyncIterator[StreamEvent]:
        """Stream one completion as core.protocols.StreamEvent values."""
        payload = build_anthropic_payload(
            req,
            reasoning_effort=self.reasoning_effort,
            model_max_tokens=self.model_max_tokens,
        )
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
                self.messages_url,
                json=payload,
                headers=self._headers(),
            ) as response:
                if response.status_code >= 400:
                    body = await _read_body(response)
                    raise anthropic_error(
                        self.name,
                        status=response.status_code,
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
            yield Finish(kind="error", failure=failure_dict(_code_of(exc), str(exc)))
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

    # -- SSE consumption ----------------------------------------------------
    async def _consume(self, response: httpx.Response) -> AsyncIterator[StreamEvent]:
        produced = False
        stopped = False
        stop_reason: str | None = None
        blocks: dict[int, _BlockState] = {}
        usage_seen = False
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_creation = 0

        async for sse in iter_sse(response.aiter_bytes()):
            data = sse.data.strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise LlmRequestError(
                    f"{self.name}: invalid JSON in SSE data: {shorten(data)}",
                    provider=self.name,
                    code=CODE_PROTOCOL,
                ) from exc
            if not isinstance(event, dict):
                raise LlmRequestError(
                    f"{self.name}: SSE data is not a JSON object",
                    provider=self.name,
                    code=CODE_PROTOCOL,
                )
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type:
                raise LlmRequestError(
                    f"{self.name}: SSE payload has no event type",
                    provider=self.name,
                    code=CODE_PROTOCOL,
                )

            if event_type == "ping":
                continue
            if event_type == "error":
                raise _stream_error(self.name, event)
            if event_type == "message_start":
                message = event.get("message")
                if isinstance(message, dict):
                    raw_usage = message.get("usage")
                    if isinstance(raw_usage, dict):
                        usage_seen = True
                        input_tokens = _as_int(raw_usage.get("input_tokens"))
                        output_tokens = _as_int(raw_usage.get("output_tokens"))
                        cache_read = _as_int(raw_usage.get("cache_read_input_tokens"))
                        cache_creation = _as_int(raw_usage.get("cache_creation_input_tokens"))
                continue
            if event_type == "content_block_start":
                index = event.get("index")
                if not isinstance(index, int):
                    raise _protocol(self.name, "content_block_start without an integer index")
                block = event.get("content_block")
                if not isinstance(block, dict):
                    raise _protocol(self.name, "content_block_start without a content_block")
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        produced = True
                        yield TextDelta(text)
                    blocks[index] = _BlockState("text")
                elif block_type == "thinking":
                    thinking = block.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        produced = True
                        yield ReasoningDelta(thinking)
                    blocks[index] = _BlockState("thinking")
                elif block_type == "tool_use":
                    call_id = block.get("id")
                    name = block.get("name")
                    if not isinstance(call_id, str) or not call_id:
                        raise _protocol(self.name, "tool_use block without an id")
                    if not isinstance(name, str) or not name:
                        raise _protocol(self.name, "tool_use block without a name")
                    initial = block.get("input")
                    args_text = ""
                    if isinstance(initial, dict) and initial:
                        # Non-streaming servers may put the whole input object in the
                        # start event; forward it like one complete argument fragment.
                        args_text = json.dumps(initial, ensure_ascii=False, separators=(",", ":"))
                    blocks[index] = _BlockState(
                        "tool_use",
                        call_id=call_id,
                        name=name,
                        args_text=args_text,
                    )
                    produced = True
                    yield ToolCallStart(call_id=call_id, name=name)
                    if args_text:
                        yield ToolCallArgsDelta(call_id=call_id, delta=args_text)
                else:
                    raise _protocol(
                        self.name,
                        f"unsupported content block type {block_type!r}",
                    )
                continue
            if event_type == "content_block_delta":
                index = event.get("index")
                state = blocks.get(index) if isinstance(index, int) else None
                if state is None:
                    raise _protocol(self.name, "content_block_delta for an unknown block index")
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    raise _protocol(self.name, "content_block_delta without a delta")
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        produced = True
                        yield TextDelta(text)
                elif delta_type == "thinking_delta":
                    thinking = delta.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        produced = True
                        yield ReasoningDelta(thinking)
                elif delta_type == "signature_delta":
                    # P1 never replays thinking signatures (Lead decision); drop it.
                    continue
                elif delta_type == "input_json_delta":
                    if state.kind != "tool_use":
                        raise _protocol(
                            self.name,
                            "input_json_delta outside a tool_use block",
                        )
                    partial = delta.get("partial_json")
                    if isinstance(partial, str) and partial:
                        state.args_text += partial
                        produced = True
                        yield ToolCallArgsDelta(call_id=state.call_id, delta=partial)
                # Unknown delta kinds (citations, future extensions) are ignored so a
                # newer server feature cannot break basic text/tool streaming.
                continue
            if event_type == "content_block_stop":
                continue
            if event_type == "message_delta":
                delta = event.get("delta")
                if isinstance(delta, dict):
                    reason = delta.get("stop_reason")
                    if isinstance(reason, str) and reason:
                        stop_reason = reason
                raw_usage = event.get("usage")
                if isinstance(raw_usage, dict):
                    usage_seen = True
                    output_tokens = max(output_tokens, _as_int(raw_usage.get("output_tokens")))
                    input_tokens = max(input_tokens, _as_int(raw_usage.get("input_tokens")))
                    cache_read = max(cache_read, _as_int(raw_usage.get("cache_read_input_tokens")))
                    cache_creation = max(
                        cache_creation,
                        _as_int(raw_usage.get("cache_creation_input_tokens")),
                    )
                continue
            if event_type == "message_stop":
                stopped = True
                break
            # Unknown top-level event types are ignored for forward compatibility.

        if not stopped:
            raise LlmRequestError(
                f"{self.name}: stream ended without message_stop",
                provider=self.name,
                retryable=True,
                code=CODE_STREAM_CLOSED,
            )
        if stop_reason is None:
            raise _protocol(self.name, "message_stop without a stop_reason")
        kind = _map_stop_reason(self.name, stop_reason)
        _validate_tool_inputs(self.name, blocks, stop_reason)
        if not produced and kind in ("stop", "tool-calls"):
            raise LlmRequestError(
                f"{self.name}: provider returned an empty response",
                provider=self.name,
                retryable=True,
                code=CODE_EMPTY_RESPONSE,
            )
        if usage_seen:
            total_input = input_tokens + cache_read + cache_creation
            yield Usage(
                input_tokens=total_input,
                output_tokens=output_tokens,
                total_tokens=total_input + output_tokens,
                cached_input_tokens=cache_read,
            )
        yield Finish(kind=kind)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _protocol(provider: str, message: str) -> LlmRequestError:
    return LlmRequestError(
        f"{provider}: {message}",
        provider=provider,
        code=CODE_PROTOCOL,
    )


def _code_of(exc: LlmError) -> str:
    return getattr(exc, "code", "") or CODE_PROTOCOL


def _map_stop_reason(provider: str, reason: str) -> FinishKind:
    kind = _STOP_REASONS.get(reason)
    if kind is None:
        # A new stop reason must not silently look like a clean stop: the agent would
        # treat a truncated/unknown outcome as success.
        raise _protocol(provider, f"unsupported stop_reason {reason!r}")
    return kind


def _validate_tool_inputs(
    provider: str,
    blocks: dict[int, _BlockState],
    stop_reason: str,
) -> None:
    """Every completed tool_use input must be a JSON object (unless max_tokens cut it)."""
    if stop_reason == "max_tokens":
        return
    for state in blocks.values():
        if state.kind != "tool_use":
            continue
        text = state.args_text.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _protocol(
                provider,
                f"invalid JSON in tool_use input for {state.name!r}: {shorten(text)}",
            ) from exc
        if not isinstance(parsed, dict):
            raise _protocol(
                provider,
                f"tool_use input for {state.name!r} is not a JSON object",
            )


def _stream_error(provider: str, payload: dict[str, Any]) -> LlmRequestError:
    error = payload.get("error")
    error_type = ""
    message = ""
    if isinstance(error, dict):
        raw_type = error.get("type")
        raw_message = error.get("message")
        error_type = raw_type if isinstance(raw_type, str) else ""
        message = raw_message if isinstance(raw_message, str) else ""
    elif isinstance(error, str):
        message = error
    return anthropic_error(
        provider,
        error_type=error_type,
        message=message,
        body=shorten(json.dumps(payload, ensure_ascii=False)),
    )


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def _read_body(response: httpx.Response) -> str:
    try:
        raw = await response.aread()
    except httpx.HTTPError:
        return ""
    return raw.decode("utf-8", "replace")


__all__ = ["AnthropicProvider"]
