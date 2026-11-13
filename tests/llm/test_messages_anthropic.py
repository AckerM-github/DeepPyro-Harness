"""Anthropic message conversion tests (docs/spec/llm.md section 14)."""

from __future__ import annotations

import json

import pytest

from deeppyro.core.protocols import ToolSchema
from deeppyro.core.wire import ImagePart, Message, ReasoningPart, TextPart, ToolCallPart
from deeppyro.llm.errors import CODE_BAD_REQUEST, LlmRequestError
from deeppyro.llm.messages_anthropic import (
    build_anthropic_payload,
    convert_anthropic_messages,
    tool_to_anthropic,
)
from tests.llm.helpers import make_request

SYSTEM = "You are Claude."


def user(*parts: object) -> Message:
    return Message(role="user", content=list(parts))


def assistant(*parts: object) -> Message:
    return Message(role="assistant", content=list(parts))


def tool_result(text: str, call_id: str, *, is_error: bool = False) -> Message:
    return Message(
        role="tool",
        content=[TextPart(text=text)],
        toolCallId=call_id,
        isError=is_error,
    )


def call(call_id: str = "call_abc", name: str = "read", arguments: str = '{"path": "a.txt"}') -> ToolCallPart:
    return ToolCallPart(id=call_id, name=name, arguments=arguments)


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


def test_converts_user_message_and_top_level_system() -> None:
    messages, system = convert_anthropic_messages([user(TextPart(text="hi"))], SYSTEM)
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert system == SYSTEM


def test_system_messages_are_folded_into_the_top_level_string() -> None:
    history = [
        Message(role="system", content=[TextPart(text="first")]),
        user(TextPart(text="hi")),
        Message(role="developer", content=[TextPart(text="second")]),
    ]
    messages, system = convert_anthropic_messages(history, SYSTEM)
    assert system == "You are Claude.\n\nfirst\n\nsecond"
    assert messages == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_empty_system_messages_are_dropped() -> None:
    history = [Message(role="developer", content=[]), user(TextPart(text="hi"))]
    _, system = convert_anthropic_messages(history, None)
    assert system is None


def test_assistant_text_and_reasoning_order_is_preserved() -> None:
    history = [
        user(TextPart(text="hi")),
        assistant(ReasoningPart(text="thinking"), TextPart(text="hello")),
    ]
    messages, _ = convert_anthropic_messages(history, None, reasoning_effort="low")
    assert messages[1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "thinking"},
            {"type": "text", "text": "hello"},
        ],
    }


def test_history_reasoning_is_dropped_when_thinking_is_off() -> None:
    history = [user(TextPart(text="hi")), assistant(ReasoningPart(text="secret"), TextPart(text="hi"))]
    messages, _ = convert_anthropic_messages(history, None, reasoning_effort="off")
    assert messages[1] == {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}


def test_tool_use_input_is_a_json_object() -> None:
    history = [
        user(TextPart(text="read it")),
        assistant(call(arguments='{"path": "src/a.py"}')),
        tool_result("ok", "call_abc"),
    ]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages[1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "call_abc", "name": "read", "input": {"path": "src/a.py"}}],
    }


@pytest.mark.parametrize("arguments", ["not json", "[1, 2]", '"text"', ""])
def test_unusable_tool_arguments_become_empty_object(arguments: str) -> None:
    history = [
        user(TextPart(text="x")),
        assistant(call(arguments=arguments)),
        tool_result("ok", "call_abc"),
    ]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages[1]["content"][0]["input"] == {}


def test_tool_result_becomes_a_user_block() -> None:
    history = [
        user(TextPart(text="read it")),
        assistant(call()),
        tool_result("file body", "call_abc"),
    ]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages[-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_abc",
                "content": [{"type": "text", "text": "file body"}],
            }
        ],
    }


def test_error_tool_result_sets_is_error() -> None:
    history = [
        user(TextPart(text="read it")),
        assistant(call()),
        tool_result("boom", "call_abc", is_error=True),
    ]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages[-1]["content"][0]["is_error"] is True


def test_tool_result_falls_back_to_source_call_id() -> None:
    history = [
        user(TextPart(text="x")),
        assistant(call()),
        Message(
            role="tool",
            content=[TextPart(text="ok")],
            source={"kind": "tool", "callId": "call_abc"},
        ),
    ]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages[-1]["content"][0]["tool_use_id"] == "call_abc"


def test_adjacent_same_role_messages_are_merged() -> None:
    history = [user(TextPart(text="a")), user(TextPart(text="b"))]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ],
        }
    ]


def test_empty_assistant_message_is_skipped() -> None:
    history = [user(TextPart(text="a")), Message(role="assistant", content=[]), user(TextPart(text="b"))]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ],
        }
    ]


def test_image_data_url_becomes_base64_block() -> None:
    history = [
        user(
            TextPart(text="look"),
            ImagePart(attachment="data:image/png;base64,QUJD"),
        )
    ]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }


def test_remote_and_bare_image_references_are_skipped() -> None:
    history = [
        user(
            TextPart(text="look"),
            ImagePart(attachment="https://example.com/cat.png"),
            ImagePart(attachment="att_123"),
        )
    ]
    messages, _ = convert_anthropic_messages(history, None)
    assert messages[0]["content"] == [{"type": "text", "text": "look"}]


def test_calls_must_be_resolved_immediately() -> None:
    history = [user(TextPart(text="x")), assistant(call()), user(TextPart(text="never mind"))]
    with pytest.raises(LlmRequestError, match="tool calls without") as info:
        convert_anthropic_messages(history, None)
    assert info.value.code == CODE_BAD_REQUEST


def test_history_ending_in_an_unresolved_call_is_rejected() -> None:
    history = [user(TextPart(text="x")), assistant(call())]
    with pytest.raises(LlmRequestError, match="tool calls without"):
        convert_anthropic_messages(history, None)


def test_tool_result_without_call_is_rejected() -> None:
    history = [user(TextPart(text="x")), tool_result("ok", "call_missing")]
    with pytest.raises(LlmRequestError, match="without a matching tool call"):
        convert_anthropic_messages(history, None)


def test_duplicate_tool_use_id_is_rejected() -> None:
    history = [
        user(TextPart(text="x")),
        assistant(call("call_dup"), call("call_dup")),
    ]
    with pytest.raises(LlmRequestError, match="duplicate tool_use id"):
        convert_anthropic_messages(history, None)


def test_empty_conversation_is_rejected() -> None:
    with pytest.raises(LlmRequestError, match="no messages"):
        convert_anthropic_messages([], None)


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------


def test_payload_defaults() -> None:
    payload = build_anthropic_payload(make_request(messages=[user(TextPart(text="hi"))]))
    assert payload["model"] == "deepseek-chat"
    assert payload["max_tokens"] == 8192
    assert payload["stream"] is True
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "system" not in payload
    assert "temperature" not in payload
    assert "tools" not in payload
    assert "output_config" not in payload


def test_payload_includes_system_temperature_and_tools() -> None:
    request = make_request(
        messages=[user(TextPart(text="hi"))],
        system=SYSTEM,
        temperature=0.0,
        tools=[ToolSchema(name="read", description="Read a file", parameters={"type": "object"})],
    )
    payload = build_anthropic_payload(request)
    assert payload["system"] == SYSTEM
    assert payload["temperature"] == 0.0
    assert payload["tools"] == [
        {
            "name": "read",
            "description": "Read a file",
            "input_schema": {"type": "object"},
        }
    ]


@pytest.mark.parametrize(
    ("max_tokens", "model_max_tokens", "expected"),
    [
        (100, 2000, 100),
        (None, 2000, 2000),
        (None, None, 8192),
        (0, 2000, 2000),
    ],
)
def test_payload_max_tokens_precedence(
    max_tokens: int | None,
    model_max_tokens: int | None,
    expected: int,
) -> None:
    request = make_request(messages=[user(TextPart(text="hi"))], max_tokens=max_tokens)
    payload = build_anthropic_payload(
        request,
        model_max_tokens=model_max_tokens,
        default_max_tokens=8192,
    )
    assert payload["max_tokens"] == expected


def test_payload_enables_thinking_for_non_off_effort() -> None:
    request = make_request(messages=[user(TextPart(text="hi"))])
    payload = build_anthropic_payload(request, reasoning_effort="medium")
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["output_config"] == {"effort": "medium"}


def test_payload_rejects_unknown_effort() -> None:
    request = make_request(messages=[user(TextPart(text="hi"))])
    with pytest.raises(LlmRequestError, match="reasoning effort") as info:
        build_anthropic_payload(request, reasoning_effort="turbo")
    assert info.value.code == CODE_BAD_REQUEST


def test_tool_to_anthropic_defaults_empty_schema() -> None:
    converted = tool_to_anthropic(ToolSchema(name="noop", description="", parameters={}))
    assert converted == {
        "name": "noop",
        "description": "",
        "input_schema": {"type": "object", "properties": {}},
    }


def test_payload_tool_arguments_are_parsed_objects() -> None:
    request = make_request(
        messages=[
            user(TextPart(text="read")),
            assistant(call(arguments='{"path": "x"}')),
            tool_result("ok", "call_abc"),
        ],
    )
    payload = build_anthropic_payload(request)
    assert payload["messages"][1]["content"][0]["input"] == {"path": "x"}
    assert json.dumps(payload)  # payload is JSON-serializable end to end
