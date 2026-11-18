"""Tests for agent.model_call.complete_model_call (KI-010 native model call).

The function is the ported heart of the deleted NativeProviderAdapter: these tests
pin every invariant the adapter enforced, now directly against the native
core.llm stream protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

import pytest

from deeppyro.agent.errors import ProviderStreamError, RunCancelledError
from deeppyro.agent.model_call import complete_model_call
from deeppyro.core.ids import new_message_id
from deeppyro.core.llm import (
    Finish,
    ModelRequest,
    NativeToolCall,
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
from deeppyro.core.messages import TOOL_CALL_ID_RE, Message, TextPart, UserSource

_CANCEL_MARKER = object()


def _request() -> ModelRequest:
    message = Message(
        id=new_message_id(),
        role="user",
        content=(TextPart(text="hi"),),
        created_at=1,
        source=UserSource(),
    )
    return ModelRequest(provider="scripted", model="fake-model", messages=(message,))


class _ScriptedProvider:
    """Yields a fixed event list; a callable entry runs in place, an exception raises."""

    name = "scripted"

    def __init__(self, events: Sequence[StreamEvent | BaseException | Callable[[], None]]) -> None:
        self._script = list(events)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for item in self._script:
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                item()
                continue
            yield item

    async def aclose(self) -> None:
        return None


async def _call(
    *events: StreamEvent | BaseException | Callable[[], None],
    cancel: asyncio.Event | None = None,
    on_text: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
) -> tuple[object, _ScriptedProvider]:
    provider = _ScriptedProvider(events)
    response = await complete_model_call(
        provider,
        _request(),
        cancel=cancel,
        on_text=on_text,
        on_reasoning=on_reasoning,
    )
    return response, provider


async def test_text_is_accumulated_in_stream_order() -> None:
    seen: list[str] = []
    response, provider = await _call(
        TextDelta(text="hel"),
        TextDelta(text="lo"),
        UsageEvent(usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5)),
        Finish(stop_reason=StopReason.STOP),
        on_text=seen.append,
    )
    assert response.text == "hello"
    assert response.finish_reason is StopReason.STOP
    assert response.usage == Usage(input_tokens=3, output_tokens=2, total_tokens=5)
    assert response.tool_calls == ()
    assert response.retries == 0
    assert seen == ["hel", "lo"]
    assert len(provider.requests) == 1
    assert provider.requests[0].provider == "scripted"
    assert isinstance(response.latency_ms, int)
    assert response.latency_ms >= 0


async def test_reasoning_is_accumulated_and_callback_gets_fragments() -> None:
    seen: list[str] = []
    response, _ = await _call(
        ReasoningDelta(text="think "),
        ReasoningDelta(text="hard"),
        TextDelta(text="done"),
        Finish(stop_reason=StopReason.STOP),
        on_reasoning=seen.append,
    )
    assert response.reasoning == "think hard"
    assert response.text == "done"
    assert seen == ["think ", "hard"]


async def test_empty_deltas_are_ignored() -> None:
    seen: list[str] = []
    response, _ = await _call(
        TextDelta(text=""),
        TextDelta(text="ok"),
        Finish(stop_reason=StopReason.STOP),
        on_text=seen.append,
    )
    assert response.text == "ok"
    assert seen == ["ok"]


async def test_tool_call_fragments_join_and_ids_are_native() -> None:
    provider = _ScriptedProvider(
        [
            ToolCallStart(call_id="provider-call-1", name="read", index=0),
            ToolCallArgsDelta(call_id="provider-call-1", delta='{"path"', index=0),
            ToolCallArgsDelta(call_id="provider-call-1", delta=': "a.txt"}', index=0),
            Finish(stop_reason=StopReason.TOOL_CALLS),
        ]
    )
    response = await complete_model_call(provider, _request())
    assert response.finish_reason is StopReason.TOOL_CALLS
    assert len(response.tool_calls) == 1
    call: NativeToolCall = response.tool_calls[0]
    assert TOOL_CALL_ID_RE.fullmatch(call.call_id)
    assert call.name == "read"
    assert call.arguments == '{"path": "a.txt"}'
    assert call.call_id != "provider-call-1"


async def test_parallel_tool_calls_are_tracked_by_index() -> None:
    response, _ = await _call(
        ToolCallStart(call_id="p1", name="read", index=0),
        ToolCallStart(call_id="p2", name="list", index=1),
        ToolCallArgsDelta(call_id="p2", delta='{"dir": "x"}', index=1),
        ToolCallArgsDelta(call_id="p1", delta='{"path": "a"}', index=0),
        Finish(stop_reason=StopReason.TOOL_CALLS),
    )
    assert [(call.name, call.arguments) for call in response.tool_calls] == [
        ("read", '{"path": "a"}'),
        ("list", '{"dir": "x"}'),
    ]
    assert len({call.call_id for call in response.tool_calls}) == 2


async def test_provider_call_id_resolves_across_indexes() -> None:
    response, _ = await _call(
        ToolCallStart(call_id="p1", name="read", index=0),
        ToolCallArgsDelta(call_id="p1", delta='{"a": 1}', index=5),
        Finish(stop_reason=StopReason.TOOL_CALLS),
    )
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].arguments == '{"a": 1}'


async def test_missing_provider_call_id_falls_back_to_the_stream_index() -> None:
    response, _ = await _call(
        ToolCallStart(call_id="", name="read", index=3),
        ToolCallArgsDelta(call_id="", delta='{"a": 2}', index=3),
        Finish(stop_reason=StopReason.TOOL_CALLS),
    )
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read"
    assert response.tool_calls[0].arguments == '{"a": 2}'


async def test_tool_call_name_is_set_once() -> None:
    response, _ = await _call(
        ToolCallStart(call_id="p1", name="read"),
        ToolCallArgsDelta(call_id="p1", delta="{}", name="overwritten"),
        Finish(stop_reason=StopReason.TOOL_CALLS),
    )
    assert response.tool_calls[0].name == "read"


async def test_arguments_default_to_an_empty_object() -> None:
    response, _ = await _call(
        ToolCallStart(call_id="p1", name="read"),
        Finish(stop_reason=StopReason.TOOL_CALLS),
    )
    assert response.tool_calls[0].arguments == "{}"


async def test_finish_is_required() -> None:
    provider = _ScriptedProvider([TextDelta(text="hi")])
    with pytest.raises(ProviderStreamError, match="ended without a finish event"):
        await complete_model_call(provider, _request())


async def test_unknown_finish_reason_is_a_stream_error() -> None:
    provider = _ScriptedProvider([Finish(stop_reason="bogus")])  # type: ignore[arg-type]
    with pytest.raises(ProviderStreamError, match="unknown finish reason"):
        await complete_model_call(provider, _request())


async def test_error_finish_with_message() -> None:
    provider = _ScriptedProvider([Finish(stop_reason=StopReason.ERROR, message="boom")])
    with pytest.raises(ProviderStreamError, match="provider reported an error: 'boom'"):
        await complete_model_call(provider, _request())


async def test_error_finish_without_message() -> None:
    provider = _ScriptedProvider([Finish(stop_reason=StopReason.ERROR)])
    with pytest.raises(ProviderStreamError, match=r"provider reported an error$"):
        await complete_model_call(provider, _request())


async def test_tool_call_without_name_is_rejected() -> None:
    provider = _ScriptedProvider(
        [
            ToolCallArgsDelta(call_id="p1", delta="{}"),
            Finish(stop_reason=StopReason.TOOL_CALLS),
        ]
    )
    with pytest.raises(ProviderStreamError, match="without a name"):
        await complete_model_call(provider, _request())


async def test_stop_finish_with_tool_calls_is_forced_to_tool_calls() -> None:
    response, _ = await _call(
        ToolCallStart(call_id="p1", name="read"),
        Finish(stop_reason=StopReason.STOP),
    )
    assert response.finish_reason is StopReason.TOOL_CALLS


async def test_tool_calls_finish_without_calls_is_rejected() -> None:
    provider = _ScriptedProvider([Finish(stop_reason=StopReason.TOOL_CALLS)])
    with pytest.raises(ProviderStreamError, match="sent no tool call"):
        await complete_model_call(provider, _request())


async def test_empty_assistant_message_is_rejected() -> None:
    provider = _ScriptedProvider(
        [
            UsageEvent(usage=Usage(input_tokens=1, output_tokens=0, total_tokens=1)),
            Finish(stop_reason=StopReason.STOP),
        ]
    )
    with pytest.raises(ProviderStreamError, match="empty assistant message"):
        await complete_model_call(provider, _request())


async def test_usage_is_last_wins() -> None:
    response, _ = await _call(
        TextDelta(text="x"),
        UsageEvent(usage=Usage(input_tokens=1, output_tokens=1, total_tokens=2)),
        UsageEvent(usage=Usage(input_tokens=7, output_tokens=8, total_tokens=15)),
        Finish(stop_reason=StopReason.STOP),
    )
    assert response.usage == Usage(input_tokens=7, output_tokens=8, total_tokens=15)


async def test_retry_events_are_counted() -> None:
    response, _ = await _call(
        RetryScheduled(attempt=1, code="RATE_LIMIT", delay_s=1.0),
        RetryScheduled(attempt=2, code="SERVER", delay_s=2.0),
        TextDelta(text="ok"),
        Finish(stop_reason=StopReason.STOP),
    )
    assert response.retries == 2


async def test_replay_state_is_passed_through() -> None:
    response, _ = await _call(
        TextDelta(text="ok"),
        Finish(stop_reason=StopReason.STOP, replay_state={"cursor": 4}),
    )
    assert response.replay_state == {"cursor": 4}


async def test_finish_is_last_wins() -> None:
    response, _ = await _call(
        TextDelta(text="ok"),
        Finish(stop_reason=StopReason.STOP),
        Finish(stop_reason=StopReason.LENGTH),
    )
    assert response.finish_reason is StopReason.LENGTH


async def test_cancel_before_the_call() -> None:
    cancel = asyncio.Event()
    cancel.set()
    provider = _ScriptedProvider([Finish(stop_reason=StopReason.STOP)])
    with pytest.raises(RunCancelledError, match="before the model call"):
        await complete_model_call(provider, _request(), cancel=cancel)


async def test_cancel_during_the_stream() -> None:
    cancel = asyncio.Event()

    def _trip() -> None:
        cancel.set()

    provider = _ScriptedProvider([TextDelta(text="a"), _trip, TextDelta(text="b")])
    with pytest.raises(RunCancelledError, match="during the model call"):
        await complete_model_call(provider, _request(), cancel=cancel)


async def test_cancel_after_the_stream() -> None:
    cancel = asyncio.Event()

    def _trip() -> None:
        cancel.set()

    provider = _ScriptedProvider([Finish(stop_reason=StopReason.STOP), _trip])
    with pytest.raises(RunCancelledError, match="before the model call finished"):
        await complete_model_call(provider, _request(), cancel=cancel)


async def test_provider_exceptions_propagate_unchanged() -> None:
    provider = _ScriptedProvider([RuntimeError("network down")])
    with pytest.raises(RuntimeError, match="network down"):
        await complete_model_call(provider, _request())


async def test_provider_cancellation_is_reraised() -> None:
    provider = _ScriptedProvider([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await complete_model_call(provider, _request())


async def test_aborted_finish_is_returned_not_raised() -> None:
    response, _ = await _call(
        TextDelta(text="partial"),
        Finish(stop_reason=StopReason.ABORTED),
    )
    assert response.finish_reason is StopReason.ABORTED
    assert response.text == "partial"
