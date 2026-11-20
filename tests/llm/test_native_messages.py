"""Tests for the native OpenAI-compatible message converter (KI-010).

The converter builds the native request body; these tests pin that body
byte-for-byte so the provider wire shape stays stable.
"""

from __future__ import annotations

import json

import pytest

from deeppyro.core.llm import ModelRequest, ToolSpec
from deeppyro.core.messages import (
    ImagePart,
    Message,
    ModelSource,
    ReasoningPart,
    SystemSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.llm.errors import CODE_BAD_REQUEST, LlmRequestError
from deeppyro.llm.native_messages import (
    build_chat_payload,
    convert_messages,
    message_to_chat,
    message_tool_calls,
    normalize_arguments,
    tool_to_json,
)


def _msg(n: int) -> str:
    return f"msg_{n:016x}"


def _user(text: str) -> Message:
    return Message(
        id=_msg(1),
        role="user",
        content=(TextPart(text=text),),
        created_at=1,
        source=UserSource(),
    )


def _assistant(*parts: object, n: int = 2) -> Message:
    return Message(
        id=_msg(n),
        role="assistant",
        content=tuple(parts),  # type: ignore[arg-type]
        created_at=1,
        source=ModelSource(provider="deepseek", model="deepseek-flash"),
    )


def _tool(text: str, call_id: str = "call_0123456789abcdef") -> Message:
    return Message(
        id=_msg(3),
        role="tool",
        content=(TextPart(text=text),),
        created_at=1,
        source=ToolSource(call_id=call_id),
        tool_call_id=call_id,
    )


def test_plain_text_request_body_is_a_golden() -> None:
    request = ModelRequest(
        provider="deepseek",
        model="deepseek-flash",
        messages=(_user("hello"),),
        max_tokens=128,
        temperature=0,
    )
    assert build_chat_payload(request) == {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 128,
        "temperature": 0,
    }


def test_system_and_tools_body_is_a_golden() -> None:
    system = Message(
        id=_msg(9),
        role="system",
        content=(TextPart(text="be terse"),),
        created_at=1,
        source=SystemSource(),
    )
    tool = ToolSpec(
        name="read",
        description="Read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    request = ModelRequest(
        provider="deepseek",
        model="deepseek-flash",
        messages=(_user("hi"),),
        system=system,
        tools=(tool,),
    )
    assert build_chat_payload(request) == {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
    }


def test_tools_are_omitted_when_empty() -> None:
    payload = build_chat_payload(ModelRequest(provider="p", model="m", messages=(_user("x"),)))
    assert "tools" not in payload


def test_system_instruction_is_not_duplicated() -> None:
    system = Message(
        id=_msg(9), role="system", content=(TextPart(text="sys"),), created_at=1, source=SystemSource()
    )
    messages = convert_messages((_user("hi"),), system)
    assert [message["role"] for message in messages] == ["system", "user"]


def test_developer_message_is_rendered_as_system() -> None:
    developer = Message(
        id=_msg(4),
        role="developer",
        content=(TextPart(text="dev rules"),),
        created_at=1,
        source=SystemSource(),
    )
    assert message_to_chat(developer) == {"role": "system", "content": "dev rules"}


def test_system_message_with_blank_text_is_dropped() -> None:
    blank = Message(
        id=_msg(4), role="system", content=(TextPart(text="   "),), created_at=1, source=SystemSource()
    )
    assert message_to_chat(blank) is None


def test_assistant_message_carries_reasoning_and_tool_calls() -> None:
    call_id = "call_0123456789abcdef"
    assistant = _assistant(
        ReasoningPart(text="think"),
        TextPart(text="answer"),
        ToolCallPart(id=call_id, name="read", arguments='{"path": "a.txt"}'),
    )
    assert message_to_chat(assistant) == {
        "role": "assistant",
        "content": "answer",
        "reasoning_content": "think",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "read", "arguments": '{"path": "a.txt"}'},
            }
        ],
    }


def test_assistant_tool_calls_only_use_null_content() -> None:
    assistant = _assistant(ToolCallPart(id="call_0123456789abcdef", name="read", arguments="{}"))
    payload = message_to_chat(assistant)
    assert payload is not None
    assert payload["content"] is None
    assert payload["tool_calls"] == [
        {
            "id": "call_0123456789abcdef",
            "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        }
    ]


def test_tool_message_uses_call_id() -> None:
    assert message_to_chat(_tool("42")) == {
        "role": "tool",
        "tool_call_id": "call_0123456789abcdef",
        "content": "42",
    }


def test_tool_message_falls_back_to_source_call_id() -> None:
    call_id = "call_aaaaaaaaaaaaaaaa"
    message = Message(
        id=_msg(5),
        role="tool",
        content=(TextPart(text="ok"),),
        created_at=1,
        source=ToolSource(call_id=call_id),
        tool_call_id=call_id,
    )
    assert message_to_chat(message) == {"role": "tool", "tool_call_id": call_id, "content": "ok"}


def test_user_image_renders_as_image_url_content_parts() -> None:
    user = Message(
        id=_msg(6),
        role="user",
        content=(
            TextPart(text="look"),
            ImagePart(attachment="data:image/png;base64,AAAA", media_type="image/png"),
        ),
        created_at=1,
        source=UserSource(),
    )
    assert message_to_chat(user) == {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


def test_user_image_without_text_renders_only_the_image() -> None:
    user = Message(
        id=_msg(6),
        role="user",
        content=(ImagePart(attachment="https://example.test/a.png", media_type="image/png"),),
        created_at=1,
        source=UserSource(),
    )
    assert message_to_chat(user) == {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}],
    }


def test_unresolvable_image_is_dropped_and_empty_user_message_is_skipped() -> None:
    user = Message(
        id=_msg(7),
        role="user",
        content=(ImagePart(attachment="att_missing", media_type="image/png"),),
        created_at=1,
        source=UserSource(),
    )
    assert message_to_chat(user) is None
    assert convert_messages((user,)) == []


def test_unresolvable_image_keeps_text_parts() -> None:
    user = Message(
        id=_msg(7),
        role="user",
        content=(
            TextPart(text="describe"),
            ImagePart(attachment="att_missing", media_type="image/png"),
        ),
        created_at=1,
        source=UserSource(),
    )
    payload = message_to_chat(user)
    assert payload == {"role": "user", "content": [{"type": "text", "text": "describe"}]}


def test_attachments_resolver_is_used_for_bare_ids() -> None:
    class _Attachments:
        def lookup(self, ref: str) -> object:
            assert ref == "att_1"
            return {"data": b"\x89PNG", "mimeType": "image/png"}

        def read_image_bytes(self, ref: str) -> bytes:
            return b"\x89PNG"

    user = Message(
        id=_msg(8),
        role="user",
        content=(ImagePart(attachment="att_1", media_type="image/webp"),),
        created_at=1,
        source=UserSource(),
    )
    payload = message_to_chat(user, attachments=_Attachments())
    assert payload == {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw=="}},
        ],
    }


def test_tool_spec_parameters_default_to_an_object_schema() -> None:
    assert tool_to_json(ToolSpec(name="noop")) == {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_tool_spec_output_is_never_sent_on_the_wire() -> None:
    tool = ToolSpec(
        name="read",
        parameters={"type": "object", "properties": {}},
        output={"type": "string"},
    )
    payload = tool_to_json(tool)
    assert set(payload["function"]) == {"name", "description", "parameters"}


def test_normalize_arguments_validates_json_and_keeps_the_raw_string() -> None:
    raw = '{"a": 1}'
    assert normalize_arguments(raw) is raw
    assert normalize_arguments(None) == "{}"
    with pytest.raises(LlmRequestError) as excinfo:
        normalize_arguments("{not json")
    assert excinfo.value.code == CODE_BAD_REQUEST
    assert excinfo.value.retryable is False


def test_max_tokens_clips_to_the_request_context_window() -> None:
    request = ModelRequest(
        provider="p",
        model="m",
        messages=(_user("x"),),
        max_tokens=4096,
        context_window=1024,
    )
    assert build_chat_payload(request)["max_tokens"] == 1024


def test_max_tokens_falls_back_to_model_context() -> None:
    request = ModelRequest(provider="p", model="m", messages=(_user("x"),), max_tokens=4096)
    assert build_chat_payload(request, model_ctx=512)["max_tokens"] == 512


def test_no_max_tokens_or_temperature_when_unset() -> None:
    payload = build_chat_payload(ModelRequest(provider="p", model="m", messages=(_user("x"),)))
    assert "max_tokens" not in payload
    assert "temperature" not in payload


def test_convert_messages_skips_system_roles_when_a_system_instruction_is_given() -> None:
    request = ModelRequest(provider="p", model="m", messages=(_user("x"),))
    system = Message(
        id=_msg(9), role="system", content=(TextPart(text="sys"),), created_at=1, source=SystemSource()
    )
    messages = convert_messages(request.messages, system)
    assert messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x"},
    ]


def test_message_tool_calls_helper_returns_native_parts() -> None:
    assistant = _assistant(
        TextPart(text="ok"),
        ToolCallPart(id="call_0123456789abcdef", name="read", arguments=json.dumps({"path": "a"})),
    )
    calls = message_tool_calls(assistant)
    assert [call.name for call in calls] == ["read"]


def test_tool_call_without_reasoning_gets_an_empty_reasoning_content_when_required() -> None:
    assistant = _assistant(ToolCallPart(id="call_0123456789abcdef", name="read", arguments="{}"))
    assert message_to_chat(assistant, requires_reasoning_content=True) == {
        "role": "assistant",
        "content": None,
        "reasoning_content": "",
        "tool_calls": [
            {
                "id": "call_0123456789abcdef",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            }
        ],
    }


def test_tool_call_without_reasoning_omits_the_field_by_default() -> None:
    assistant = _assistant(ToolCallPart(id="call_0123456789abcdef", name="read", arguments="{}"))
    payload = message_to_chat(assistant)
    assert payload is not None
    assert "reasoning_content" not in payload


def test_captured_reasoning_wins_when_replay_is_required() -> None:
    assistant = _assistant(
        ReasoningPart(text="think"),
        ToolCallPart(id="call_0123456789abcdef", name="read", arguments="{}"),
    )
    payload = message_to_chat(assistant, requires_reasoning_content=True)
    assert payload is not None
    assert payload["reasoning_content"] == "think"


def test_plain_assistant_message_does_not_gain_empty_reasoning_content() -> None:
    assistant = _assistant(TextPart(text="answer"))
    assert message_to_chat(assistant, requires_reasoning_content=True) == {
        "role": "assistant",
        "content": "answer",
    }


def test_reasoning_replay_only_applies_when_tools_are_offered() -> None:
    tool = ToolSpec(name="read", parameters={"type": "object", "properties": {}})
    messages = (
        _user("hi"),
        _assistant(ToolCallPart(id="call_0123456789abcdef", name="read", arguments="{}")),
    )
    without_tools = build_chat_payload(
        ModelRequest(provider="deepseek", model="deepseek-flash", messages=messages),
        requires_reasoning_content=True,
    )
    assert "reasoning_content" not in without_tools["messages"][1]

    with_tools = build_chat_payload(
        ModelRequest(provider="deepseek", model="deepseek-flash", messages=messages, tools=(tool,)),
        requires_reasoning_content=True,
    )
    assert with_tools["messages"][1]["reasoning_content"] == ""
