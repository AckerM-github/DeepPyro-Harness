"""OpenAICompatProvider tests against mocked SSE servers (no network).

Every request/response shape here mirrors fixtures/records/by-type/request_header.jsonl
and assistant_message.jsonl: stream=true, include_usage, reasoning_content chunks,
fragmented tool_calls, then a finish chunk.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

from deeppyro.core.errors import LlmError
from deeppyro.core.protocols import (
    Finish,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
)
from deeppyro.llm import OpenAICompatProvider
from deeppyro.llm.errors import CODE_AUTH, CODE_PROTOCOL, LlmHttpError
from deeppyro.llm.retry import RetryPolicy
from tests.llm.helpers import (
    SSE_HEADERS,
    chat_chunk,
    make_request,
    sse_doc,
    sse_response,
    text_stream,
    text_tool,
    usage_chunk,
    user_message,
)

URL = "http://127.0.0.1:8000/v1/chat/completions"


def make_provider(
    *, api_key: str | None = "test-key", **retry_kwargs: Any
) -> tuple[OpenAICompatProvider, list[float]]:
    """Provider whose retry policy records sleeps instead of waiting."""
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    retry_kwargs.setdefault("max_attempts", 3)
    retry_kwargs.setdefault("base_delay", 0.0)
    retry_kwargs.setdefault("max_delay", 60.0)
    retry_kwargs.setdefault("jitter", 0.0)
    policy = RetryPolicy(sleep=sleep, **retry_kwargs)
    provider = OpenAICompatProvider("local", "http://127.0.0.1:8000/v1", api_key, retry_policy=policy)
    return provider, sleeps


async def collect(provider: OpenAICompatProvider, request: Any | None = None) -> list[StreamEvent]:
    events: list[StreamEvent] = []
    async for event in provider.stream(request or make_request()):
        events.append(event)
    await provider.aclose()
    return events


def texts(events: list[StreamEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def index_of(events: list[StreamEvent], event_type: type) -> int:
    for index, event in enumerate(events):
        if isinstance(event, event_type):
            return index
    raise AssertionError(f"no {event_type.__name__} in stream")


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------


@respx.mock
async def test_plain_text_stream() -> None:
    route = respx.post(URL).mock(return_value=sse_response(text_stream("Hel", "lo", usage=True)))
    provider, _ = make_provider()
    events = await collect(provider)

    assert texts(events) == "Hello"
    assert isinstance(events[-1], Finish)
    assert events[-1].kind == "stop"
    assert events[-1].failure is None
    usages = [event for event in events if isinstance(event, Usage)]
    assert len(usages) == 1
    assert (usages[0].input_tokens, usages[0].output_tokens, usages[0].total_tokens) == (10, 5, 15)
    assert usages[0].cached_input_tokens is None
    assert index_of(events, Usage) < index_of(events, Finish)
    assert route.call_count == 1


@respx.mock
async def test_request_payload_and_headers() -> None:
    route = respx.post(URL).mock(return_value=sse_response(text_stream("ok")))
    provider, _ = make_provider()
    await collect(
        provider,
        make_request(
            system="be brief",
            messages=[user_message("hello")],
            tools=[text_tool()],
            max_tokens=100,
            temperature=0.2,
        ),
    )
    request = route.calls[0].request
    body = json.loads(request.content)

    assert body["model"] == "deepseek-chat"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["messages"][0] == {"role": "system", "content": "be brief"}
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert body["tools"][0]["function"]["name"] == "read"
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.2
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["accept"] == "text/event-stream"


@respx.mock
async def test_no_authorization_header_without_api_key() -> None:
    route = respx.post(URL).mock(return_value=sse_response(text_stream("ok")))
    provider, _ = make_provider(api_key=None)
    await collect(provider)
    assert "authorization" not in route.calls[0].request.headers


def test_endpoint_construction() -> None:
    assert OpenAICompatProvider("p", "http://127.0.0.1:8000/v1", None).endpoint == URL
    assert OpenAICompatProvider("p", "http://127.0.0.1:8000/v1/", None).endpoint == URL
    assert (
        OpenAICompatProvider("p", "https://api.deepseek.com/chat/completions", None).endpoint
        == "https://api.deepseek.com/chat/completions"
    )


@respx.mock
async def test_reasoning_deltas() -> None:
    body = sse_doc(
        [
            chat_chunk({"role": "assistant"}),
            chat_chunk({"reasoning_content": "think "}),
            chat_chunk({"reasoning_content": "hard"}),
            chat_chunk({"reasoning": " alt"}),
            chat_chunk({"content": "answer"}),
            chat_chunk({}, "stop"),
        ]
    )
    respx.post(URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)

    assert [event.text for event in events if isinstance(event, ReasoningDelta)] == [
        "think ",
        "hard",
        " alt",
    ]
    assert texts(events) == "answer"
    assert events[-1].kind == "stop"


@respx.mock
async def test_single_fragmented_tool_call() -> None:
    body = sse_doc(
        [
            chat_chunk({"role": "assistant"}),
            chat_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_00_abc",
                            "type": "function",
                            "function": {"name": "read", "arguments": ""},
                        }
                    ]
                }
            ),
            chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"pa'}}]}),
            chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}]}),
            chat_chunk({}, "tool_calls"),
        ]
    )
    respx.post(URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    assert len(starts) == 1
    assert starts[0].call_id == "call_00_abc"
    assert starts[0].name == "read"
    arguments = "".join(event.delta for event in events if isinstance(event, ToolCallArgsDelta))
    assert arguments == '{"path": "a.txt"}'
    assert index_of(events, ToolCallStart) < index_of(events, ToolCallArgsDelta)
    assert isinstance(events[-1], Finish)
    assert events[-1].kind == "tool-calls"


@respx.mock
async def test_multiple_parallel_tool_calls_are_keyed_by_index() -> None:
    body = sse_doc(
        [
            chat_chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path": "a"}'},
                        },
                        {
                            "index": 1,
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "write", "arguments": '{"path": "b"}'},
                        },
                    ]
                }
            ),
            chat_chunk(
                {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": ","}},
                        {"index": 1, "function": {"arguments": ","}},
                    ]
                }
            ),
            chat_chunk({}, "tool_calls"),
        ]
    )
    respx.post(URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)

    starts = {event.call_id: event.name for event in events if isinstance(event, ToolCallStart)}
    assert starts == {"call_a": "read", "call_b": "write"}
    arguments: dict[str, str] = {}
    for event in events:
        if isinstance(event, ToolCallArgsDelta):
            arguments[event.call_id] = arguments.get(event.call_id, "") + event.delta
    assert arguments == {"call_a": '{"path": "a"},', "call_b": '{"path": "b"},'}
    assert events[-1].kind == "tool-calls"


@pytest.mark.parametrize("deepseek_field", [False, True])
@respx.mock
async def test_usage_cached_tokens(deepseek_field: bool) -> None:
    body = sse_doc(
        [
            chat_chunk({"role": "assistant"}),
            chat_chunk({"content": "x"}),
            usage_chunk(cached_tokens=384, deepseek_cache_field=deepseek_field),
            chat_chunk({}, "stop"),
        ]
    )
    respx.post(URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)

    usage = next(event for event in events if isinstance(event, Usage))
    assert usage.cached_input_tokens == 384


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("stop", "stop"),
        ("length", "length"),
        ("content_filter", "content-filter"),
        ("unknown_vendor_reason", "stop"),
    ],
)
@respx.mock
async def test_finish_reason_mapping(reason: str, kind: str) -> None:
    body = sse_doc([chat_chunk({"role": "assistant"}), chat_chunk({"content": "x"}), chat_chunk({}, reason)])
    respx.post(URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)
    assert isinstance(events[-1], Finish)
    assert events[-1].kind == kind


@respx.mock
async def test_done_marker_without_finish_reason_infers_stop() -> None:
    body = sse_doc([chat_chunk({"content": "done"})], done=True)
    respx.post(URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)
    assert texts(events) == "done"
    assert isinstance(events[-1], Finish)
    assert events[-1].kind == "stop"


# --------------------------------------------------------------------------
# mid-stream failures
# --------------------------------------------------------------------------


@respx.mock
async def test_mid_stream_cut_after_content_emits_error_finish() -> None:
    body = sse_doc([chat_chunk({"role": "assistant"}), chat_chunk({"content": "partial"})], done=False)
    route = respx.post(URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)

    assert texts(events) == "partial"
    assert isinstance(events[-1], Finish)
    assert events[-1].kind == "error"
    assert events[-1].failure is not None
    assert events[-1].failure["code"] == "PROTOCOL"
    assert route.call_count == 1  # content already produced -> no retry


@respx.mock
async def test_invalid_json_sse_data_is_protocol_error() -> None:
    respx.post(URL).mock(return_value=sse_response("data: {not json}\n\n"))
    provider, _ = make_provider(max_attempts=1)
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    assert getattr(excinfo.value, "code", None) == "PROTOCOL"
    assert excinfo.value.retryable is False


@respx.mock
async def test_non_event_stream_content_type_is_protocol_error() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json={"choices": [], "object": "chat.completion"})
    )
    provider, _ = make_provider()
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    assert getattr(excinfo.value, "code", None) == "PROTOCOL"
    assert route.call_count == 1


@respx.mock
async def test_unterminated_stream_without_content_is_retryable() -> None:
    body = sse_doc([chat_chunk({"role": "assistant"})], done=False)
    route = respx.post(URL).mock(return_value=sse_response(body))
    provider, _ = make_provider(max_attempts=1)
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    assert getattr(excinfo.value, "code", None) == "EMPTY_RESPONSE"
    assert excinfo.value.retryable is True
    assert route.call_count == 1


# --------------------------------------------------------------------------
# HTTP-level failures and retries
# --------------------------------------------------------------------------


@respx.mock
async def test_http_500_is_retried_then_succeeds() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(500, json={"error": {"message": "boom"}}),
            sse_response(text_stream("recovered")),
        ]
    )
    provider, sleeps = make_provider()
    events = await collect(provider)

    assert texts(events) == "recovered"
    assert route.call_count == 2
    assert len(sleeps) == 1


@respx.mock
async def test_retry_after_header_is_honoured() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow"}}),
            sse_response(text_stream("ok")),
        ]
    )
    provider, sleeps = make_provider()
    events = await collect(provider)

    assert texts(events) == "ok"
    assert route.call_count == 2
    assert sleeps == [7.0]


@respx.mock
async def test_non_retryable_http_error_raises_once() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad request shape"}})
    )
    provider, sleeps = make_provider()
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    error = excinfo.value
    assert error.status == 400
    assert error.retryable is False
    assert error.provider == "local"
    assert "bad request shape" in str(error)
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
async def test_auth_error_is_not_retried() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}})
    )
    provider, _ = make_provider()
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    assert getattr(excinfo.value, "code", None) == "AUTH"
    assert excinfo.value.retryable is False
    assert route.call_count == 1


@respx.mock
async def test_stream_error_event_is_retried() -> None:
    error_body = sse_doc([{"error": {"message": "overloaded", "type": "server_error"}}], done=False)
    route = respx.post(URL).mock(side_effect=[sse_response(error_body), sse_response(text_stream("ok"))])
    provider, _ = make_provider()
    events = await collect(provider)
    assert texts(events) == "ok"
    assert route.call_count == 2


@respx.mock
async def test_stream_error_event_exhausts_and_raises() -> None:
    error_body = sse_doc([{"error": {"message": "rate limited", "type": "rate_limit_error"}}], done=False)
    route = respx.post(URL).mock(return_value=sse_response(error_body))
    provider, _ = make_provider(max_attempts=1)
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    assert getattr(excinfo.value, "code", None) == "RATE_LIMIT"
    assert excinfo.value.retryable is True
    assert route.call_count == 1


@respx.mock
async def test_empty_response_is_retried_then_succeeds() -> None:
    empty_body = sse_doc([chat_chunk({"role": "assistant"}), chat_chunk({}, "stop")])
    route = respx.post(URL).mock(side_effect=[sse_response(empty_body), sse_response(text_stream("ok"))])
    provider, _ = make_provider()
    events = await collect(provider)
    assert texts(events) == "ok"
    assert route.call_count == 2


@respx.mock
async def test_empty_response_exhausts_and_raises() -> None:
    empty_body = sse_doc([chat_chunk({"role": "assistant"}), chat_chunk({}, "stop")])
    route = respx.post(URL).mock(return_value=sse_response(empty_body))
    provider, _ = make_provider(max_attempts=2)
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    assert getattr(excinfo.value, "code", None) == "EMPTY_RESPONSE"
    assert route.call_count == 2


# --------------------------------------------------------------------------
# lifecycle / cancellation
# --------------------------------------------------------------------------


async def test_aclose_is_idempotent_and_rejects_streams() -> None:
    provider, _ = make_provider()
    await provider.aclose()
    await provider.aclose()
    with pytest.raises(LlmError, match="closed"):
        await collect(provider)


@respx.mock
async def test_cancellation_propagates_mid_stream() -> None:
    first_chunk_sent = asyncio.Event()

    async def hanging_body():
        first_chunk_sent.set()
        yield b'data: {"choices":[{"index":0,"delta":{"content":"first"}}]}\n\n'
        await asyncio.Event().wait()

    respx.post(URL).mock(return_value=httpx.Response(200, content=hanging_body(), headers=SSE_HEADERS))
    provider, _ = make_provider()
    stream = provider.stream(make_request())
    try:
        assert isinstance(await anext(stream), TextDelta)
        await first_chunk_sent.wait()
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        await stream.aclose()
        await provider.aclose()


# --------------------------------------------------------------------------
# model discovery (GET <base>/models)
# --------------------------------------------------------------------------

LISTING_URL = "http://127.0.0.1:8000/v1/models"


@respx.mock
async def test_list_models_parses_openai_listing() -> None:
    route = respx.get(LISTING_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "deepseek-chat", "context_length": 65536, "name": "DeepSeek Chat"},
                    {"id": "local-9b"},
                ],
            },
        )
    )
    provider, _ = make_provider()
    models = await provider.list_models()
    assert [(model.provider, model.id) for model in models] == [
        ("local", "deepseek-chat"),
        ("local", "local-9b"),
    ]
    assert models[0].name == "DeepSeek Chat"
    assert models[0].context_window == 65536
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["accept"] == "application/json"
    await provider.aclose()


@respx.mock
async def test_list_models_405_means_no_listing_endpoint() -> None:
    respx.get(LISTING_URL).mock(return_value=httpx.Response(405, text="method not allowed"))
    provider, _ = make_provider()
    assert await provider.list_models() == []
    await provider.aclose()


@respx.mock
async def test_list_models_is_sent_without_api_key() -> None:
    route = respx.get(LISTING_URL).mock(return_value=httpx.Response(200, json={"data": [{"id": "m"}]}))
    provider, _ = make_provider(api_key=None)
    models = await provider.list_models()
    assert [model.id for model in models] == ["m"]
    assert "authorization" not in route.calls[0].request.headers
    await provider.aclose()


@respx.mock
async def test_list_models_surfaces_auth_errors() -> None:
    respx.get(LISTING_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "invalid api key"}})
    )
    provider, _ = make_provider()
    with pytest.raises(LlmHttpError) as excinfo:
        await provider.list_models()
    assert excinfo.value.code == CODE_AUTH
    assert excinfo.value.retryable is False
    assert "invalid api key" in str(excinfo.value)
    await provider.aclose()


@respx.mock
async def test_list_models_rejects_html_payload() -> None:
    respx.get(LISTING_URL).mock(return_value=httpx.Response(200, text="<html>login</html>"))
    provider, _ = make_provider()
    with pytest.raises(LlmError) as excinfo:
        await provider.list_models()
    assert getattr(excinfo.value, "code", None) == CODE_PROTOCOL
    await provider.aclose()
