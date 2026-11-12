from __future__ import annotations

from deeppyro.core.protocols import Finish, LlmProvider, LlmRequest, TextDelta

from .fake_provider import Response, ScriptedProvider, tool_response


async def test_scripted_provider_is_a_provider() -> None:
    provider = ScriptedProvider()
    assert isinstance(provider, LlmProvider)


async def test_streams_scripted_text_in_order() -> None:
    provider = ScriptedProvider([Response(text="hello", reasoning="think", usage=None)])
    request = LlmRequest(messages=[], system="s", tools=[], provider="fake", model="m")
    events = [event async for event in provider.stream(request)]
    assert [type(e).__name__ for e in events] == ["ReasoningDelta", "TextDelta", "Finish"]
    assert isinstance(events[1], TextDelta) and events[1].text == "hello"
    assert isinstance(events[2], Finish) and events[2].kind == "stop"
    assert provider.requests == [request]


async def test_empty_script_finishes_stop_without_hanging() -> None:
    provider = ScriptedProvider()
    request = LlmRequest(messages=[], system="", tools=[], provider="fake", model="m")
    events = [event async for event in provider.stream(request)]
    assert len(events) == 1 and isinstance(events[0], Finish)


async def test_tool_response_shape() -> None:
    provider = ScriptedProvider([tool_response("c1", "read_file", '{"path": "a"}')])
    request = LlmRequest(messages=[], system="", tools=[], provider="fake", model="m")
    events = [event async for event in provider.stream(request)]
    names = [type(e).__name__ for e in events]
    assert names == ["ToolCallStart", "ToolCallArgsDelta", "Finish"]


async def test_aclose_marks_closed() -> None:
    provider = ScriptedProvider()
    await provider.aclose()
    assert provider.closed is True
