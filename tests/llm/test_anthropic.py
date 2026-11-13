"""AnthropicProvider streaming tests.

Every request is mocked with respx: no test touches the network. The SSE documents mirror
the Anthropic Messages event protocol (message_start / content_block_* / message_delta /
message_stop) described in docs/spec/llm.md section 14.
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
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
)
from deeppyro.llm.anthropic import AnthropicProvider
from deeppyro.llm.errors import (
    CODE_AUTH,
    CODE_CONTEXT_WINDOW_EXCEEDED,
    CODE_EMPTY_RESPONSE,
    CODE_PROTOCOL,
    CODE_QUOTA,
    CODE_STREAM_CLOSED,
    LlmRequestError,
)
from deeppyro.llm.retry import RetryPolicy
from tests.llm.helpers import SSE_HEADERS, make_request, user_message

BASE = "http://127.0.0.1:8100"
MESSAGES_URL = BASE + "/v1/messages"
LISTING_URL = BASE + "/v1/models?limit=1000"


# --------------------------------------------------------------------------
# SSE document builders
# --------------------------------------------------------------------------


def sse_event(payload: dict[str, Any], *, name: str | None = None) -> str:
    event_name = name or str(payload.get("type", "message"))
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def message_start(
    *,
    input_tokens: int = 10,
    output_tokens: int = 1,
    cache_read: int = 0,
    cache_creation: int = 0,
    usage: bool = True,
) -> str:
    message: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [],
        "stop_reason": None,
    }
    if usage:
        message["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        }
    return sse_event({"type": "message_start", "message": message})


def block_start(index: int, block: dict[str, Any]) -> str:
    return sse_event({"type": "content_block_start", "index": index, "content_block": block})


def block_delta(index: int, delta: dict[str, Any]) -> str:
    return sse_event({"type": "content_block_delta", "index": index, "delta": delta})


def block_stop(index: int) -> str:
    return sse_event({"type": "content_block_stop", "index": index})


def message_delta(
    stop_reason: str | None,
    *,
    output_tokens: int | None = None,
    input_tokens: int | None = None,
    cache_read: int | None = None,
) -> str:
    payload: dict[str, Any] = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason},
    }
    usage: dict[str, Any] = {}
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if cache_read is not None:
        usage["cache_read_input_tokens"] = cache_read
    if usage:
        payload["usage"] = usage
    return sse_event(payload)


def message_stop() -> str:
    return sse_event({"type": "message_stop"})


def text_reply(
    *parts: str,
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 2,
    cache_read: int = 0,
    cache_creation: int = 0,
    usage: bool = True,
) -> str:
    events = [
        message_start(
            input_tokens=input_tokens,
            cache_read=cache_read,
            cache_creation=cache_creation,
            usage=usage,
        ),
        block_start(0, {"type": "text", "text": ""}),
    ]
    events.extend(block_delta(0, {"type": "text_delta", "text": part}) for part in parts)
    events.extend(
        [
            block_stop(0),
            message_delta(
                stop_reason,
                output_tokens=output_tokens,
                input_tokens=input_tokens if usage else None,
                cache_read=cache_read if usage else None,
            ),
            message_stop(),
        ]
    )
    return "".join(events)


def sse_response(body: str, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers=dict(SSE_HEADERS))


def make_provider(
    api_key: str | None = "test-key", **retry_kwargs: Any
) -> tuple[AnthropicProvider, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    retry_kwargs.setdefault("max_attempts", 3)
    retry_kwargs.setdefault("base_delay", 0.0)
    retry_kwargs.setdefault("max_delay", 60.0)
    retry_kwargs.setdefault("jitter", 0.0)
    provider = AnthropicProvider(
        "claude",
        BASE,
        api_key,
        retry_policy=RetryPolicy(sleep=fake_sleep, **retry_kwargs),
    )
    return provider, sleeps


def request() -> Any:
    return make_request(model="claude-test", messages=[user_message("hi")])


async def collect(provider: AnthropicProvider, req: Any | None = None) -> list[Any]:
    events = [event async for event in provider.stream(req or request())]
    await provider.aclose()
    return events


# --------------------------------------------------------------------------
# URL / constructor
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        (
            "https://api.anthropic.com/v1/messages",
            "https://api.anthropic.com/v1/messages",
        ),
        ("https://proxy.example/anthropic", "https://proxy.example/anthropic/v1/messages"),
    ],
)
def test_messages_url(base_url: str, expected: str) -> None:
    provider = AnthropicProvider("claude", base_url, "key")
    assert provider.messages_url == expected


def test_rejects_unknown_reasoning_effort() -> None:
    from deeppyro.core.errors import ConfigError

    with pytest.raises(ConfigError, match="reasoning effort"):
        AnthropicProvider("claude", BASE, "key", reasoning_effort="turbo")


# --------------------------------------------------------------------------
# plain text streams
# --------------------------------------------------------------------------


@respx.mock
async def test_text_stream_maps_to_deltas_usage_and_finish() -> None:
    body = "".join(
        [
            message_start(input_tokens=100, output_tokens=1, cache_read=50, cache_creation=20),
            block_start(0, {"type": "text", "text": ""}),
            block_delta(0, {"type": "text_delta", "text": "Hel"}),
            block_delta(0, {"type": "text_delta", "text": "lo"}),
            block_stop(0),
            message_delta("end_turn", output_tokens=7),
            message_stop(),
        ]
    )
    route = respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)

    assert [type(event) for event in events] == [TextDelta, TextDelta, Usage, Finish]
    assert [event.text for event in events[:2]] == ["Hel", "lo"]
    usage = events[2]
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (170, 7, 177)
    assert usage.cached_input_tokens == 50
    finish = events[3]
    assert finish.kind == "stop"

    sent = route.calls[0].request
    assert sent.headers["x-api-key"] == "test-key"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in sent.headers
    payload = json.loads(sent.content)
    assert payload["model"] == "claude-test"
    assert payload["stream"] is True
    assert payload["max_tokens"] == 8192
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


@respx.mock
async def test_request_sends_system_and_tools_and_effort() -> None:
    body = text_reply("ok")
    route = respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    req = make_request(
        model="claude-test",
        messages=[user_message("hi")],
        system="Be brief.",
        tools=[
            __import__("deeppyro.core.protocols", fromlist=["ToolSchema"]).ToolSchema(
                name="read",
                description="Read a file",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
        temperature=0.2,
        max_tokens=512,
    )
    provider, _ = make_provider()
    provider.reasoning_effort = "low"  # bypass the config seam for this request-shape test
    events = await collect(provider, req)
    assert events[-1].kind == "stop"
    payload = json.loads(route.calls[0].request.content)
    assert payload["system"] == "Be brief."
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 512
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["output_config"] == {"effort": "low"}
    assert payload["tools"] == [
        {
            "name": "read",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


@respx.mock
async def test_thinking_deltas_map_to_reasoning_and_signatures_are_dropped() -> None:
    body = "".join(
        [
            message_start(),
            block_start(0, {"type": "thinking", "thinking": "", "signature": ""}),
            block_delta(0, {"type": "thinking_delta", "thinking": "Let me "}),
            block_delta(0, {"type": "signature_delta", "signature": "abc"}),
            block_delta(0, {"type": "thinking_delta", "thinking": "think."}),
            block_stop(0),
            block_start(1, {"type": "text", "text": ""}),
            block_delta(1, {"type": "text_delta", "text": "Done"}),
            block_stop(1),
            message_delta("end_turn", output_tokens=4),
            message_stop(),
        ]
    )
    respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)
    reasoning = [event.text for event in events if isinstance(event, ReasoningDelta)]
    texts = [event.text for event in events if isinstance(event, TextDelta)]
    assert reasoning == ["Let me ", "think."]
    assert texts == ["Done"]
    assert events[-1].kind == "stop"


# --------------------------------------------------------------------------
# tool calls
# --------------------------------------------------------------------------


@respx.mock
async def test_tool_call_fragments_are_forwarded_verbatim() -> None:
    body = "".join(
        [
            message_start(usage=False),
            block_start(0, {"type": "tool_use", "id": "call_1", "name": "read", "input": {}}),
            block_delta(0, {"type": "input_json_delta", "partial_json": '{"path": '}),
            block_delta(0, {"type": "input_json_delta", "partial_json": '"a.txt"}'}),
            block_stop(0),
            message_delta("tool_use"),
            message_stop(),
        ]
    )
    respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)

    assert isinstance(events[0], ToolCallStart)
    assert (events[0].call_id, events[0].name) == ("call_1", "read")
    fragments = [event.delta for event in events if isinstance(event, ToolCallArgsDelta)]
    assert fragments == ['{"path": ', '"a.txt"}']
    assert "".join(fragments) == '{"path": "a.txt"}'
    assert events[-1].kind == "tool-calls"


@respx.mock
async def test_tool_use_block_with_full_input_emits_one_argument_delta() -> None:
    body = "".join(
        [
            message_start(usage=False),
            block_start(0, {"type": "tool_use", "id": "call_2", "name": "read", "input": {"path": "x"}}),
            block_stop(0),
            message_delta("tool_use"),
            message_stop(),
        ]
    )
    respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)
    assert isinstance(events[0], ToolCallStart)
    assert events[1] == ToolCallArgsDelta(call_id="call_2", delta='{"path":"x"}')
    assert events[-1].kind == "tool-calls"


@respx.mock
async def test_invalid_tool_json_is_a_protocol_error_after_content() -> None:
    body = "".join(
        [
            message_start(usage=False),
            block_start(0, {"type": "tool_use", "id": "call_1", "name": "read", "input": {}}),
            block_delta(0, {"type": "input_json_delta", "partial_json": '{"path"'}),
            block_stop(0),
            message_delta("tool_use"),
            message_stop(),
        ]
    )
    route = respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, sleeps = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "error"
    assert events[-1].failure["code"] == CODE_PROTOCOL
    assert "invalid JSON" in events[-1].failure["message"]
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
async def test_non_object_tool_input_is_a_protocol_error() -> None:
    body = "".join(
        [
            message_start(usage=False),
            block_start(0, {"type": "tool_use", "id": "call_1", "name": "read", "input": {}}),
            block_delta(0, {"type": "input_json_delta", "partial_json": "[1, 2]"}),
            block_stop(0),
            message_delta("tool_use"),
            message_stop(),
        ]
    )
    respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "error"
    assert "not a JSON object" in events[-1].failure["message"]


@respx.mock
async def test_truncated_tool_input_is_allowed_when_max_tokens_stopped_the_turn() -> None:
    body = "".join(
        [
            message_start(usage=False),
            block_start(0, {"type": "tool_use", "id": "call_1", "name": "read", "input": {}}),
            block_delta(0, {"type": "input_json_delta", "partial_json": '{"path"'}),
            block_stop(0),
            message_delta("max_tokens", output_tokens=10),
            message_stop(),
        ]
    )
    respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "length"


# --------------------------------------------------------------------------
# stop reasons, usage
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_reason", "kind"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("pause_turn", "stop"),
        ("tool_use", "tool-calls"),
        ("max_tokens", "length"),
        ("refusal", "content-filter"),
    ],
)
@respx.mock
async def test_stop_reason_mapping(stop_reason: str, kind: str) -> None:
    respx.post(MESSAGES_URL).mock(return_value=sse_response(text_reply("hi", stop_reason=stop_reason)))
    provider, _ = make_provider()
    events = await collect(provider)
    assert events[-1].kind == kind


@respx.mock
async def test_unknown_stop_reason_is_a_protocol_error() -> None:
    body = text_reply("hi", stop_reason="teleported")
    respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "error"
    assert events[-1].failure["code"] == CODE_PROTOCOL
    assert "stop_reason" in events[-1].failure["message"]


@respx.mock
async def test_usage_can_come_from_message_delta_only() -> None:
    body = "".join(
        [
            message_start(usage=False),
            block_start(0, {"type": "text", "text": ""}),
            block_delta(0, {"type": "text_delta", "text": "hi"}),
            block_stop(0),
            message_delta("end_turn", output_tokens=5, input_tokens=12),
            message_stop(),
        ]
    )
    respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    events = await collect(provider)
    usage = next(event for event in events if isinstance(event, Usage))
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (12, 5, 17)


@respx.mock
async def test_usage_counts_cache_reads_and_writes_as_input() -> None:
    respx.post(MESSAGES_URL).mock(
        return_value=sse_response(
            text_reply("hi", input_tokens=30, output_tokens=4, cache_read=100, cache_creation=40)
        )
    )
    provider, _ = make_provider()
    events = await collect(provider)
    usage = next(event for event in events if isinstance(event, Usage))
    assert usage.input_tokens == 170
    assert usage.cached_input_tokens == 100
    assert usage.total_tokens == 174


# --------------------------------------------------------------------------
# failure modes and retries
# --------------------------------------------------------------------------


@respx.mock
async def test_stream_without_message_stop_after_content_finishes_with_error() -> None:
    body = "".join(
        [
            message_start(),
            block_start(0, {"type": "text", "text": ""}),
            block_delta(0, {"type": "text_delta", "text": "partial"}),
        ]
    )
    route = respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, sleeps = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "error"
    assert events[-1].failure["code"] == CODE_STREAM_CLOSED
    assert "message_stop" in events[-1].failure["message"]
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
async def test_stream_without_message_stop_before_content_is_retried() -> None:
    body = message_start()
    route = respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, sleeps = make_provider(max_attempts=2)
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    assert getattr(excinfo.value, "code", None) == CODE_STREAM_CLOSED
    assert getattr(excinfo.value, "retryable", False) is True
    assert route.call_count == 2
    assert sleeps == [0.0]


@respx.mock
async def test_empty_response_is_retried_and_raises() -> None:
    body = message_start() + message_delta("end_turn") + message_stop()
    route = respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, sleeps = make_provider(max_attempts=2)
    with pytest.raises(LlmError) as excinfo:
        await collect(provider)
    assert getattr(excinfo.value, "code", None) == CODE_EMPTY_RESPONSE
    assert route.call_count == 2
    assert sleeps == [0.0]


@respx.mock
async def test_http_401_auth_error_is_not_retried() -> None:
    body = json.dumps(
        {
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        }
    )
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(401, text=body))
    provider, sleeps = make_provider()
    with pytest.raises(LlmRequestError) as excinfo:
        await collect(provider)
    assert excinfo.value.code == CODE_AUTH
    assert excinfo.value.retryable is False
    assert excinfo.value.status == 401
    assert "invalid x-api-key" in str(excinfo.value)
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
async def test_http_429_honours_retry_after_and_recovers() -> None:
    rate_limited = httpx.Response(
        429,
        headers={"retry-after": "3"},
        json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
    )
    route = respx.post(MESSAGES_URL).mock(side_effect=[rate_limited, sse_response(text_reply("hi"))])
    provider, sleeps = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "stop"
    assert route.call_count == 2
    assert sleeps == [3.0]


@respx.mock
async def test_http_500_overloaded_is_retried() -> None:
    server_error = httpx.Response(
        529,
        json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
    )
    route = respx.post(MESSAGES_URL).mock(side_effect=[server_error, sse_response(text_reply("hi"))])
    provider, _ = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "stop"
    assert route.call_count == 2


@respx.mock
async def test_context_window_error_is_not_retried() -> None:
    body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "prompt is too long: 250000 tokens > 200000 maximum",
            },
        }
    )
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(400, text=body))
    provider, _ = make_provider()
    with pytest.raises(LlmRequestError) as excinfo:
        await collect(provider)
    assert excinfo.value.code == CODE_CONTEXT_WINDOW_EXCEEDED
    assert excinfo.value.retryable is False
    assert route.call_count == 1


@respx.mock
async def test_http_402_maps_to_quota_error() -> None:
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(402, text="payment required"))
    provider, _ = make_provider()
    with pytest.raises(LlmRequestError) as excinfo:
        await collect(provider)
    assert excinfo.value.code == CODE_QUOTA
    assert excinfo.value.retryable is False


@respx.mock
async def test_non_event_stream_response_is_a_protocol_error() -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})
    )
    provider, _ = make_provider()
    with pytest.raises(LlmRequestError) as excinfo:
        await collect(provider)
    assert excinfo.value.code == CODE_PROTOCOL
    assert route.call_count == 1


@respx.mock
async def test_stream_error_event_before_content_is_retried() -> None:
    error_doc = "".join(
        [
            message_start(),
            sse_event(
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "busy"},
                }
            ),
        ]
    )
    route = respx.post(MESSAGES_URL).mock(
        side_effect=[sse_response(error_doc), sse_response(text_reply("hi"))]
    )
    provider, sleeps = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "stop"
    assert route.call_count == 2
    assert sleeps == [0.0]


@respx.mock
async def test_stream_error_event_after_content_finishes_with_error() -> None:
    error_doc = "".join(
        [
            message_start(),
            block_start(0, {"type": "text", "text": ""}),
            block_delta(0, {"type": "text_delta", "text": "partial"}),
            sse_event(
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "busy"},
                }
            ),
        ]
    )
    route = respx.post(MESSAGES_URL).mock(return_value=sse_response(error_doc))
    provider, _ = make_provider()
    events = await collect(provider)
    assert events[-1].kind == "error"
    assert events[-1].failure["code"] == "SERVER"
    assert "busy" in events[-1].failure["message"]
    assert route.call_count == 1


@respx.mock
async def test_invalid_json_in_sse_fails_as_protocol_error() -> None:
    body = message_start() + "event: content_block_delta\ndata: {not json}\n\n"
    respx.post(MESSAGES_URL).mock(return_value=sse_response(body))
    provider, _ = make_provider()
    with pytest.raises(LlmRequestError) as excinfo:
        await collect(provider)
    assert excinfo.value.code == CODE_PROTOCOL


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
        yield message_start().encode()
        yield block_start(0, {"type": "text", "text": ""}).encode()
        yield block_delta(0, {"type": "text_delta", "text": "first"}).encode()
        await asyncio.Event().wait()

    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, content=hanging_body(), headers=dict(SSE_HEADERS))
    )
    provider, _ = make_provider()
    stream = provider.stream(request())
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
# model discovery (GET <base>/v1/models?limit=1000)
# --------------------------------------------------------------------------


@respx.mock
async def test_list_models_parses_anthropic_listing() -> None:
    route = respx.get(LISTING_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "type": "model",
                        "id": "claude-sonnet-4-5",
                        "display_name": "Claude Sonnet 4.5",
                        "created_at": "2025-09-29T00:00:00Z",
                    },
                    {"type": "model", "id": "claude-haiku-4-5"},
                ],
                "has_more": False,
            },
        )
    )
    provider, _ = make_provider()
    models = await provider.list_models()
    assert [(model.provider, model.id) for model in models] == [
        ("claude", "claude-sonnet-4-5"),
        ("claude", "claude-haiku-4-5"),
    ]
    assert models[0].name == "Claude Sonnet 4.5"
    sent = route.calls[0].request
    assert sent.headers["x-api-key"] == "test-key"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    await provider.aclose()


@respx.mock
async def test_list_models_404_means_no_listing_endpoint() -> None:
    respx.get(LISTING_URL).mock(return_value=httpx.Response(404, text="not found"))
    provider, _ = make_provider()
    assert await provider.list_models() == []
    await provider.aclose()
