"""Native Anthropic Messages provider tests (KI-010 port).

Every HTTP interaction goes through httpx.MockTransport, so nothing here touches the
network. The SSE documents mirror the real Messages stream protocol: message_start,
content_block_start/delta/stop, message_delta and message_stop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from deeppyro.agent.model_call import complete_model_call
from deeppyro.core.errors import LlmError
from deeppyro.core.ids import new_message_id
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
    ToolSpec,
    Usage,
    UsageEvent,
)
from deeppyro.core.messages import (
    ImagePart,
    Message,
    ModelSource,
    SystemSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.llm.errors import (
    CODE_BAD_REQUEST,
    CODE_PROTOCOL,
    CODE_RATE_LIMIT,
    CODE_STREAM_CLOSED,
    LlmRequestError,
)
from deeppyro.llm.native_anthropic import NativeAnthropicProvider
from deeppyro.llm.retry import RetryPolicy

BASE_URL = "https://api.anthropic.com"
URL = BASE_URL + "/v1/messages"
MODEL = "claude-sonnet-4-5"
CALL_ID = "call_0123456789abcdef"


# --------------------------------------------------------------------------
# request / message helpers
# --------------------------------------------------------------------------


def _user(text: str) -> Message:
    return Message(
        id=new_message_id(),
        role="user",
        content=(TextPart(text=text),),
        created_at=1,
        source=UserSource(),
    )


def _system(text: str) -> Message:
    return Message(
        id=new_message_id(),
        role="system",
        content=(TextPart(text=text),),
        created_at=1,
        source=SystemSource(),
    )


def _request(**kwargs: Any) -> ModelRequest:
    fields: dict[str, Any] = {
        "provider": "claude",
        "model": MODEL,
        "messages": (_user("hello"),),
    }
    fields.update(kwargs)
    return ModelRequest(**fields)


# --------------------------------------------------------------------------
# Anthropic SSE event helpers
# --------------------------------------------------------------------------


def _event(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload) + "\n\n"


def _sse(events: list[dict[str, Any]]) -> str:
    return "".join(_event(payload) for payload in events)


def message_start(
    *,
    input_tokens: int = 10,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def content_block_start(index: int, block: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_start", "index": index, "content_block": block}


def text_block(text: str = "") -> dict[str, Any]:
    return {"type": "text", "text": text}


def thinking_block(thinking: str = "") -> dict[str, Any]:
    return {"type": "thinking", "thinking": thinking, "signature": ""}


def tool_use_block(call_id: str, name: str, input: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": input or {}}


def content_block_delta(index: int, delta: dict[str, Any]) -> dict[str, Any]:
    return {"type": "content_block_delta", "index": index, "delta": delta}


def text_delta(index: int, text: str) -> dict[str, Any]:
    return content_block_delta(index, {"type": "text_delta", "text": text})


def thinking_delta(index: int, thinking: str) -> dict[str, Any]:
    return content_block_delta(index, {"type": "thinking_delta", "thinking": thinking})


def signature_delta(index: int, signature: str = "sig") -> dict[str, Any]:
    return content_block_delta(index, {"type": "signature_delta", "signature": signature})


def input_json_delta(index: int, partial_json: str) -> dict[str, Any]:
    return content_block_delta(index, {"type": "input_json_delta", "partial_json": partial_json})


def content_block_stop(index: int) -> dict[str, Any]:
    return {"type": "content_block_stop", "index": index}


def message_delta(
    stop_reason: str | None, *, output_tokens: int = 0, input_tokens: int = 0
) -> dict[str, Any]:
    event: dict[str, Any] = {"type": "message_delta", "delta": {}}
    if stop_reason is not None:
        event["delta"]["stop_reason"] = stop_reason
    event["usage"] = {"output_tokens": output_tokens, "input_tokens": input_tokens}
    return event


def message_stop() -> dict[str, Any]:
    return {"type": "message_stop"}


def text_stream(
    *parts: str,
    stop: str = "end_turn",
    usage: bool = True,
    output_tokens: int = 7,
) -> str:
    events: list[dict[str, Any]] = [message_start(input_tokens=12)]
    events.append(content_block_start(0, text_block()))
    events.extend(text_delta(0, part) for part in parts)
    events.append(content_block_stop(0))
    events.append(message_delta(stop, output_tokens=output_tokens if usage else 0))
    events.append(message_stop())
    return _sse(events)


def sse_response(body: str, *, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    merged = {"content-type": "text/event-stream"}
    if headers:
        merged.update(headers)
    return httpx.Response(status, text=body, headers=merged)


def make_provider(
    handler: Any,
    *,
    api_key: str | None = "test-key",
    reasoning_effort: str = "off",
    **retry_kwargs: Any,
) -> tuple[NativeAnthropicProvider, list[float]]:
    """Provider over httpx.MockTransport whose retry policy records sleeps."""
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    retry_kwargs.setdefault("max_attempts", 3)
    retry_kwargs.setdefault("base_delay", 0.0)
    retry_kwargs.setdefault("max_delay", 60.0)
    retry_kwargs.setdefault("jitter", 0.0)
    policy = RetryPolicy(sleep=sleep, **retry_kwargs)
    provider = NativeAnthropicProvider(
        "claude",
        BASE_URL,
        api_key,
        retry_policy=policy,
        reasoning_effort=reasoning_effort,
        transport=httpx.MockTransport(handler),
    )
    return provider, sleeps


async def collect(
    provider: NativeAnthropicProvider, request: ModelRequest | None = None
) -> list[StreamEvent]:
    try:
        return [event async for event in provider.stream(request or _request())]
    finally:
        await provider.aclose()


def texts(events: list[StreamEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


def only(events: list[StreamEvent], event_type: type) -> list[Any]:
    return [event for event in events if isinstance(event, event_type)]


def body_of(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------


async def test_text_stream_maps_to_native_events_and_request_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sse_response(text_stream("Hel", "lo"))

    provider, _ = make_provider(handler)
    events = await collect(provider)

    assert texts(events) == "Hello"
    usages = only(events, UsageEvent)
    assert len(usages) == 1
    assert usages[0].usage == Usage(
        input_tokens=12,
        output_tokens=7,
        total_tokens=19,
        cached_input_tokens=0,
        cache_write_tokens=0,
    )
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.stop_reason is StopReason.STOP
    usage_index = next(index for index, event in enumerate(events) if isinstance(event, UsageEvent))
    assert usage_index < len(events) - 1

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == URL
    assert request.headers["x-api-key"] == "test-key"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["accept"] == "text/event-stream"
    assert body_of(request) == {
        "model": MODEL,
        "max_tokens": 8192,
        "stream": True,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        "thinking": {"type": "disabled"},
    }


async def test_request_mapping_carries_system_tools_temperature_and_max_tokens() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sse_response(text_stream("ok"))

    tool = ToolSpec(
        name="read",
        description="Read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    request = _request(
        system=_system("be terse"),
        tools=(tool,),
        max_tokens=256,
        temperature=0.25,
    )
    provider, _ = make_provider(handler)
    events = await collect(provider, request)

    assert isinstance(events[-1], Finish)
    body = body_of(seen[0])
    assert body["system"] == "be terse"
    assert body["max_tokens"] == 256
    assert body["temperature"] == 0.25
    assert body["tools"] == [
        {
            "name": "read",
            "description": "Read a file",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]


async def test_tool_call_arguments_split_across_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            _sse(
                [
                    message_start(),
                    content_block_start(0, tool_use_block("toolu_01", "read")),
                    input_json_delta(0, '{"path":'),
                    input_json_delta(0, ' "a.txt"}'),
                    content_block_stop(0),
                    message_delta("tool_use", output_tokens=5),
                    message_stop(),
                ]
            )
        )

    provider, _ = make_provider(handler)
    events = await collect(provider)

    starts = only(events, ToolCallStart)
    assert len(starts) == 1
    assert starts[0].call_id == "toolu_01"
    assert starts[0].name == "read"
    args = only(events, ToolCallArgsDelta)
    assert [delta.call_id for delta in args] == ["toolu_01", "toolu_01"]
    assert "".join(delta.delta for delta in args) == '{"path": "a.txt"}'
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.stop_reason is StopReason.TOOL_CALLS


async def test_complete_model_call_assembles_the_streamed_tool_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            _sse(
                [
                    message_start(),
                    content_block_start(0, text_block()),
                    text_delta(0, "reading"),
                    content_block_stop(0),
                    content_block_start(1, tool_use_block("toolu_01", "read")),
                    input_json_delta(1, '{"path": "a.txt"}'),
                    content_block_stop(1),
                    message_delta("tool_use", output_tokens=9),
                    message_stop(),
                ]
            )
        )

    provider, _ = make_provider(handler)
    response = await complete_model_call(provider, _request())
    assert response.text == "reading"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read"
    assert response.tool_calls[0].arguments == '{"path": "a.txt"}'
    assert response.finish_reason is StopReason.TOOL_CALLS
    await provider.aclose()


async def test_thinking_deltas_map_to_reasoning_when_enabled() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sse_response(
            _sse(
                [
                    message_start(),
                    content_block_start(0, thinking_block()),
                    thinking_delta(0, "plan "),
                    thinking_delta(0, "the answer"),
                    signature_delta(0),
                    content_block_stop(0),
                    content_block_start(1, text_block()),
                    text_delta(1, "answer"),
                    content_block_stop(1),
                    message_delta("end_turn", output_tokens=4),
                    message_stop(),
                ]
            )
        )

    provider, _ = make_provider(handler, reasoning_effort="medium")
    events = await collect(provider)

    assert [event.text for event in only(events, ReasoningDelta)] == ["plan ", "the answer"]
    assert texts(events) == "answer"
    body = body_of(seen[0])
    assert body["thinking"] == {"type": "enabled"}
    assert body["output_config"] == {"effort": "medium"}


async def test_usage_folds_cache_read_and_creation_tokens() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            _sse(
                [
                    message_start(input_tokens=10, cache_read=100, cache_creation=5),
                    content_block_start(0, text_block()),
                    text_delta(0, "ok"),
                    content_block_stop(0),
                    message_delta("end_turn", output_tokens=20),
                    message_stop(),
                ]
            )
        )

    provider, _ = make_provider(handler)
    events = await collect(provider)

    usages = only(events, UsageEvent)
    assert len(usages) == 1
    assert usages[0].usage == Usage(
        input_tokens=115,
        output_tokens=20,
        total_tokens=135,
        cached_input_tokens=100,
        cache_write_tokens=5,
    )


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", StopReason.STOP),
        ("stop_sequence", StopReason.STOP),
        ("pause_turn", StopReason.STOP),
        ("max_tokens", StopReason.LENGTH),
        ("refusal", StopReason.CONTENT_FILTER),
    ],
)
async def test_stop_reasons_map_to_core_stop_reasons(stop_reason: str, expected: StopReason) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(text_stream("hi", stop=stop_reason))

    provider, _ = make_provider(handler)
    events = await collect(provider)
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.stop_reason is expected


async def test_image_input_becomes_an_inline_base64_block() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sse_response(text_stream("ok"))

    message = Message(
        id=new_message_id(),
        role="user",
        content=(
            TextPart(text="look"),
            ImagePart(attachment="data:image/png;base64,QUJD", media_type="image/png"),
        ),
        created_at=1,
        source=UserSource(),
    )
    provider, _ = make_provider(handler)
    events = await collect(provider, _request(messages=(message,)))

    assert isinstance(events[-1], Finish)
    assert body_of(seen[0])["messages"][0]["content"] == [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
    ]


async def test_tool_history_round_trips_into_tool_use_and_tool_result() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sse_response(text_stream("done"))

    assistant = Message(
        id=new_message_id(),
        role="assistant",
        content=(
            TextPart(text="calling"),
            ToolCallPart(id=CALL_ID, name="read", arguments='{"path": "a.txt"}'),
        ),
        created_at=1,
        source=ModelSource(provider="claude", model=MODEL),
    )
    tool = Message(
        id=new_message_id(),
        role="tool",
        content=(TextPart(text="contents"),),
        created_at=1,
        source=ToolSource(call_id=CALL_ID),
        tool_call_id=CALL_ID,
    )
    provider, _ = make_provider(handler)
    await collect(provider, _request(messages=(_user("read it"), assistant, tool)))

    messages = body_of(seen[0])["messages"]
    assert messages[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "calling"},
            {"type": "tool_use", "id": CALL_ID, "name": "read", "input": {"path": "a.txt"}},
        ],
    }
    assert messages[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": CALL_ID,
                "content": [{"type": "text", "text": "contents"}],
            }
        ],
    }


# --------------------------------------------------------------------------
# failures, retries and cancellation
# --------------------------------------------------------------------------


async def test_429_is_retried_and_emits_retry_scheduled() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
                headers={"retry-after": "7"},
            )
        return sse_response(text_stream("ok"))

    provider, sleeps = make_provider(handler)
    events = await collect(provider)

    retries = only(events, RetryScheduled)
    assert retries == [RetryScheduled(attempt=1, code=CODE_RATE_LIMIT, delay_s=7.0)]
    assert events[0] == retries[0]
    assert sleeps == [7.0]
    assert texts(events) == "ok"
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.stop_reason is StopReason.STOP
    assert len(calls) == 2


async def test_400_is_not_retried_and_maps_to_bad_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad input"}},
        )

    provider, sleeps = make_provider(handler)
    with pytest.raises(LlmRequestError) as info:
        await collect(provider)

    assert info.value.code == CODE_BAD_REQUEST
    assert info.value.retryable is False
    assert len(calls) == 1
    assert sleeps == []


async def test_malformed_sse_before_content_is_a_protocol_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return sse_response("data: {not json\n\n")

    provider, sleeps = make_provider(handler)
    with pytest.raises(LlmRequestError) as info:
        await collect(provider)

    assert info.value.code == CODE_PROTOCOL
    assert info.value.retryable is False
    assert len(calls) == 1
    assert sleeps == []


async def test_unknown_stop_reason_ends_with_an_error_finish() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(text_stream("hi", stop="teleported"))

    provider, _ = make_provider(handler)
    events = await collect(provider)

    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.stop_reason is StopReason.ERROR
    assert finish.code == CODE_PROTOCOL


async def test_truncated_stream_after_content_ends_with_an_error_finish() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            _sse([message_start(), content_block_start(0, text_block()), text_delta(0, "partial")])
        )

    provider, _ = make_provider(handler)
    events = await collect(provider)

    assert texts(events) == "partial"
    finish = events[-1]
    assert isinstance(finish, Finish)
    assert finish.stop_reason is StopReason.ERROR
    assert finish.code == CODE_STREAM_CLOSED


async def test_aclose_is_idempotent_and_rejects_streams() -> None:
    provider, _ = make_provider(lambda request: sse_response(text_stream("ok")))
    await provider.aclose()
    await provider.aclose()
    with pytest.raises(LlmError, match="closed"):
        await collect(provider)


async def test_cancellation_propagates_mid_stream() -> None:
    first_chunk_sent = asyncio.Event()

    async def hanging_body():
        first_chunk_sent.set()
        yield _event(message_start()).encode()
        yield _event(content_block_start(0, text_block())).encode()
        yield _event(text_delta(0, "first")).encode()
        await asyncio.Event().wait()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=hanging_body(),
            headers={"content-type": "text/event-stream"},
        )

    provider, _ = make_provider(handler)
    stream = provider.stream(_request())
    try:
        first = await anext(stream)
        assert isinstance(first, TextDelta)
        await first_chunk_sent.wait()
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        await stream.aclose()
        await provider.aclose()
