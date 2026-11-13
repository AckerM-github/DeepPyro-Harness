"""Message conversion tests: wire Message -> OpenAI chat-completions payload."""

from __future__ import annotations

import pytest

from deeppyro.core.wire import (
    ImagePart,
    Message,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)
from deeppyro.llm.errors import CODE_BAD_REQUEST, LlmRequestError
from deeppyro.llm.messages import (
    build_chat_payload,
    convert_messages,
    image_part_url,
    message_to_chat,
    normalize_arguments,
    tool_to_json,
)
from tests.llm.helpers import make_request, text_tool, user_message


def _assistant_with_tool_call() -> Message:
    return Message(
        role="assistant",
        content=[
            ReasoningPart(text="thinking"),
            TextPart(text="let me look"),
            ToolCallPart(id="call_abc", name="read", arguments='{"path": "a.txt"}'),
        ],
    )


def test_system_instruction_is_prepended() -> None:
    payload = build_chat_payload(make_request(system="you are helpful"))
    assert payload["messages"][0] == {"role": "system", "content": "you are helpful"}
    assert payload["messages"][1] == {"role": "user", "content": "hi"}


def test_existing_system_message_wins_over_request_system() -> None:
    history = [Message(role="system", content=[TextPart(text="from history")]), user_message("hi")]
    payload = build_chat_payload(make_request(messages=history, system="from request"))
    system_messages = [m for m in payload["messages"] if m["role"] == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"] == "from history"


def test_developer_role_becomes_system() -> None:
    message = Message(role="developer", content=[TextPart(text="memory note")])
    assert message_to_chat(message) == {"role": "system", "content": "memory note"}


def test_empty_developer_message_is_dropped() -> None:
    message = Message(role="developer", content=[])
    assert message_to_chat(message) is None


def test_assistant_text_and_reasoning_and_tool_calls() -> None:
    converted = message_to_chat(_assistant_with_tool_call())
    assert converted is not None
    assert converted["role"] == "assistant"
    assert converted["content"] == "let me look"
    assert converted["reasoning_content"] == "thinking"
    assert converted["tool_calls"] == [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "read", "arguments": '{"path": "a.txt"}'},
        }
    ]


def test_assistant_with_only_tool_calls_has_null_content() -> None:
    message = Message(
        role="assistant",
        content=[ToolCallPart(id="call_1", name="read", arguments="{}")],
    )
    converted = message_to_chat(message)
    assert converted is not None
    assert converted["content"] is None
    assert "reasoning_content" not in converted


def test_tool_message_uses_tool_call_id() -> None:
    message = Message(
        role="tool",
        content=[TextPart(text="file body")],
        toolCallId="call_abc",
        source={"kind": "tool", "callId": "call_abc"},
    )
    assert message_to_chat(message) == {
        "role": "tool",
        "tool_call_id": "call_abc",
        "content": "file body",
    }


def test_tool_message_falls_back_to_source_call_id() -> None:
    message = Message(
        role="tool",
        content=[TextPart(text="out")],
        source={"kind": "tool", "callId": "call_from_source"},
    )
    converted = message_to_chat(message)
    assert converted is not None
    assert converted["tool_call_id"] == "call_from_source"


def test_user_image_data_url() -> None:
    message = Message(
        role="user",
        content=[TextPart(text="look"), ImagePart(attachment="data:image/png;base64,AAAA")],
    )
    assert message_to_chat(message) == {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


def test_image_dict_with_base64_and_mime() -> None:
    part = ImagePart(attachment={"data": "BBBB", "mimeType": "image/jpeg"})
    assert image_part_url(part) == "data:image/jpeg;base64,BBBB"


def test_unresolvable_attachment_is_skipped() -> None:
    assert image_part_url(ImagePart(attachment="attachment-123")) is None
    message = Message(role="user", content=[ImagePart(attachment="attachment-123")])
    assert message_to_chat(message) is None


def test_normalize_arguments_is_byte_for_byte_identical() -> None:
    raw = '{ "path" : "a.txt", "n": 1 }'
    assert normalize_arguments(raw) == raw
    assert normalize_arguments('{"b":2,"a":1}') == '{"b":2,"a":1}'
    assert normalize_arguments(None) == "{}"
    assert normalize_arguments("{}") == "{}"


def test_normalize_arguments_rejects_invalid_json() -> None:
    with pytest.raises(LlmRequestError) as info:
        normalize_arguments("not json")
    assert info.value.code == CODE_BAD_REQUEST
    assert info.value.retryable is False


def test_tool_call_with_invalid_arguments_raises() -> None:
    message = Message(
        role="assistant",
        content=[ToolCallPart(id="call_bad", name="read", arguments="{oops")],
    )
    with pytest.raises(LlmRequestError, match="not valid JSON"):
        message_to_chat(message)


def test_tool_to_json() -> None:
    assert tool_to_json(text_tool()) == {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }


def test_payload_stream_fields_and_optional_fields() -> None:
    payload = build_chat_payload(
        make_request(
            tools=[text_tool()],
            max_tokens=200000,
            temperature=0.0,
            context_window=65536,
            session_id="session-1",
        )
    )
    assert payload["model"] == "deepseek-chat"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["max_tokens"] == 65536  # clamped to context_window
    assert payload["temperature"] == 0.0
    assert payload["tools"][0]["function"]["name"] == "read"


def test_payload_omits_tools_and_temperature_when_absent() -> None:
    payload = build_chat_payload(make_request())
    assert "tools" not in payload
    assert "max_tokens" not in payload
    assert "temperature" not in payload


def test_payload_uses_provider_model_ctx_for_clamp() -> None:
    payload = build_chat_payload(make_request(max_tokens=99999), model_ctx=32768)
    assert payload["max_tokens"] == 32768


def test_convert_messages_drops_empty_messages() -> None:
    history = [Message(role="user", content=[]), user_message("kept")]
    converted = convert_messages(history)
    assert converted == [{"role": "user", "content": "kept"}]


# ---------------------------------------------------------------------------
# attachment resolver (image_part_url, optional attachments= service)
# ---------------------------------------------------------------------------


def _ref() -> dict[str, object]:
    return {"attachmentId": "att-1", "mediaType": "image/png", "bytes": 3}


class _Store:
    """Resolver double exposing lookup + read_image_bytes (duck-typed service)."""

    def __init__(self, refs: dict[str, dict[str, object]] | None = None, data: bytes = b"XYZ") -> None:
        self.refs = refs or {}
        self.data = data
        self.read_calls: list[object] = []

    def lookup(self, attachment_id: str) -> dict[str, object]:
        return self.refs[attachment_id]

    def read_image_bytes(self, ref: object) -> bytes:
        self.read_calls.append(ref)
        return self.data


class _PathStore:
    """Resolver double that only knows host paths."""

    def __init__(self, path: object) -> None:
        self.path = path
        self.calls: list[object] = []

    def image_host_path(self, ref: object) -> object:
        self.calls.append(ref)
        return self.path


class _BrokenStore:
    def __init__(self, *, lookup_error: bool = False) -> None:
        self.lookup_error = lookup_error

    def lookup(self, attachment_id: str) -> dict[str, object]:
        if self.lookup_error:
            raise KeyError(attachment_id)
        return {"attachmentId": attachment_id, "mediaType": "image/png"}

    def read_image_bytes(self, ref: object) -> bytes:
        raise OSError("cannot read attachment")


def test_resolver_resolves_reference_dict_to_data_url() -> None:
    store = _Store(data=b"XYZ")
    part = ImagePart(attachment=_ref())
    assert image_part_url(part, attachments=store) == "data:image/png;base64,WFla"
    assert store.read_calls == [_ref()]


def test_resolver_resolves_bare_id_through_lookup() -> None:
    store = _Store(refs={"att-1": _ref()}, data=b"XYZ")
    assert image_part_url(ImagePart(attachment="att-1"), attachments=store) == ("data:image/png;base64,WFla")
    assert store.read_calls == [_ref()]


def test_resolver_uses_bare_id_directly_without_lookup() -> None:
    class _Direct:
        def __init__(self) -> None:
            self.refs: list[object] = []

        def read_image_bytes(self, ref: object) -> bytes:
            self.refs.append(ref)
            return b"XYZ"

    store = _Direct()
    assert image_part_url(ImagePart(attachment="att-1"), attachments=store) == ("data:image/png;base64,WFla")
    assert store.refs == ["att-1"]


def test_resolver_reads_host_path_and_keeps_declared_mime(tmp_path: object) -> None:
    path = tmp_path / "image.png"  # type: ignore[operator]
    path.write_bytes(b"HELLO")
    store = _PathStore(path)
    ref = {"attachmentId": "att-1", "mediaType": "image/jpeg", "bytes": 5}
    assert image_part_url(ImagePart(attachment=ref), attachments=store) == ("data:image/jpeg;base64,SEVMTE8=")
    # image_host_path is only consulted when read_image_bytes is absent.
    assert store.calls == [ref]


def test_resolver_bare_id_host_path_uses_default_mime(tmp_path: object) -> None:
    path = tmp_path / "image.png"  # type: ignore[operator]
    path.write_bytes(b"XYZ")
    store = _PathStore(path)
    assert image_part_url(ImagePart(attachment="att-1"), attachments=store) == ("data:image/png;base64,WFla")
    assert store.calls == ["att-1"]


def test_resolver_accepts_plain_dict_part() -> None:
    store = _Store(data=b"XYZ")
    part = {"type": "image", "attachment": _ref()}
    assert image_part_url(part, attachments=store) == "data:image/png;base64,WFla"


def test_resolver_lookup_failure_degrades_to_none() -> None:
    store = _BrokenStore(lookup_error=True)
    assert image_part_url(ImagePart(attachment="att-1"), attachments=store) is None


def test_resolver_read_failure_degrades_to_none() -> None:
    assert image_part_url(ImagePart(attachment=_ref()), attachments=_BrokenStore()) is None


def test_resolver_missing_host_file_degrades_to_none(tmp_path: object) -> None:
    store = _PathStore(tmp_path / "missing.png")  # type: ignore[operator]
    assert image_part_url(ImagePart(attachment=_ref()), attachments=store) is None


def test_resolver_without_supported_methods_degrades_to_none() -> None:
    assert image_part_url(ImagePart(attachment=_ref()), attachments=object()) is None


def test_resolver_empty_bytes_degrade_to_none() -> None:
    assert image_part_url(ImagePart(attachment=_ref()), attachments=_Store(data=b"")) is None


def test_inline_data_wins_over_resolver() -> None:
    part = ImagePart(attachment={"data": "BBBB", "mimeType": "image/jpeg"})
    assert image_part_url(part, attachments=_BrokenStore()) == "data:image/jpeg;base64,BBBB"


def test_ready_urls_are_forwarded_without_resolver_call() -> None:
    for url in ("data:image/png;base64,AAAA", "https://example.test/a.png"):
        assert image_part_url(ImagePart(attachment=url), attachments=_BrokenStore()) == url
