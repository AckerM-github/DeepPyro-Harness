"""Native message model tests: identity, role/part matrix, and JSON round trip."""

from __future__ import annotations

import pytest

from deeppyro.core.messages import (
    MISSING_TOOL_RESULT_TEXT,
    CompactionSource,
    GoalSource,
    ImagePart,
    Message,
    MessageValidationError,
    ModelSource,
    PluginSource,
    ReasoningPart,
    RepairSource,
    SystemSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
    message_text,
    message_tool_calls,
    missing_tool_result_message,
    part_from_dict,
    part_to_dict,
    replace_content,
    source_from_dict,
    source_to_dict,
)

CALL_ID = "call_0123456789abcdef"
MESSAGE_ID = "msg_0123456789abcdef"


def text_message(role: str = "user", **overrides: object) -> Message:
    fields = {
        "id": MESSAGE_ID,
        "role": role,
        "content": (TextPart("hello"),),
        "created_at": 1_700_000_000_000,
        "source": UserSource(),
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


def test_text_message_serializes_without_null_fields() -> None:
    message = text_message()
    data = message.to_dict()
    assert data == {
        "id": MESSAGE_ID,
        "role": "user",
        "content": [{"type": "text", "text": "hello"}],
        "created_at": 1_700_000_000_000,
        "source": {"kind": "user"},
    }
    assert Message.from_dict(data) == message


def test_tool_message_round_trips_identity_fields() -> None:
    message = Message(
        id=MESSAGE_ID,
        role="tool",
        content=(TextPart("boom"),),
        created_at=7,
        source=ToolSource(CALL_ID),
        tool_call_id=CALL_ID,
        is_error=True,
    )
    data = message.to_dict()
    assert data["tool_call_id"] == CALL_ID
    assert data["is_error"] is True
    assert Message.from_dict(data) == message


def test_all_parts_round_trip() -> None:
    parts = (
        TextPart("plain"),
        ReasoningPart("thinking"),
        ToolCallPart(CALL_ID, "read_file", '{"path":"a.txt"}'),
        ImagePart("attachment-1", "image/png"),
    )
    for part in parts:
        assert part_from_dict(part_to_dict(part)) == part


def test_all_sources_round_trip() -> None:
    sources = (
        UserSource(),
        ModelSource("deepseek", "deepseek-flash"),
        ToolSource(CALL_ID),
        SystemSource(),
        GoalSource("goal-1", 2, 3),
        CompactionSource("compaction-1"),
        RepairSource("missing-tool-result"),
        PluginSource("demo"),
    )
    for source in sources:
        assert source_from_dict(source_to_dict(source)) == source


def test_message_helpers_expose_text_and_calls() -> None:
    message = Message(
        id=MESSAGE_ID,
        role="assistant",
        content=(
            ReasoningPart("think"),
            TextPart("before"),
            ToolCallPart(CALL_ID, "read_file", "{}"),
            TextPart("after"),
        ),
        created_at=1,
        source=ModelSource("deepseek", "deepseek-flash"),
    )
    assert message_text(message) == "beforeafter"
    assert message_tool_calls(message) == (ToolCallPart(CALL_ID, "read_file", "{}"),)


def test_missing_tool_result_message_is_deterministic() -> None:
    message = missing_tool_result_message(CALL_ID, 99)
    assert message.id == "msg_missing_0123456789abcdef"
    assert message.role == "tool"
    assert message.tool_call_id == CALL_ID
    assert message.is_error is True
    assert message.source == RepairSource("missing-tool-result")
    assert message.text() == MISSING_TOOL_RESULT_TEXT


@pytest.mark.parametrize(
    ("role", "part"),
    [
        ("system", ImagePart("a", "image/png")),
        ("developer", ReasoningPart("r")),
        ("user", ToolCallPart(CALL_ID, "x", "{}")),
        ("assistant", ImagePart("a", "image/png")),
        ("tool", ToolCallPart(CALL_ID, "x", "{}")),
    ],
)
def test_role_part_matrix_rejects_disallowed_parts(role: str, part: object) -> None:
    with pytest.raises(MessageValidationError):
        Message(
            id=MESSAGE_ID,
            role=role,  # type: ignore[arg-type]
            content=(part,),  # type: ignore[arg-type]
            created_at=1,
            source=UserSource(),
        )


def test_tool_role_requires_matching_tool_source_and_call_id() -> None:
    with pytest.raises(MessageValidationError, match="requires tool_call_id"):
        Message(
            id=MESSAGE_ID, role="tool", content=(TextPart("x"),), created_at=1, source=ToolSource(CALL_ID)
        )
    with pytest.raises(MessageValidationError, match="requires a tool or repair message source"):
        Message(
            id=MESSAGE_ID,
            role="tool",
            content=(TextPart("x"),),
            created_at=1,
            source=UserSource(),
            tool_call_id=CALL_ID,
        )
    with pytest.raises(MessageValidationError, match="must equal tool_call_id"):
        Message(
            id=MESSAGE_ID,
            role="tool",
            content=(TextPart("x"),),
            created_at=1,
            source=ToolSource("call_9999999999999999"),
            tool_call_id=CALL_ID,
        )


def test_non_tool_role_rejects_tool_call_id_and_is_error() -> None:
    with pytest.raises(MessageValidationError, match="must not carry tool_call_id"):
        text_message(tool_call_id=CALL_ID)
    with pytest.raises(MessageValidationError, match="only valid on tool messages"):
        text_message(is_error=True)


def test_message_rejects_bad_identity_and_content() -> None:
    with pytest.raises(MessageValidationError, match="not a message id"):
        text_message(id="msg_short")
    with pytest.raises(MessageValidationError, match="non-empty tuple"):
        text_message(content=())
    with pytest.raises(MessageValidationError, match="does not allow"):
        text_message(role="system", content=(TextPart("x"), ImagePart("a", "image/gif")))
    with pytest.raises(MessageValidationError, match="created_at"):
        text_message(created_at=-1)
    with pytest.raises(MessageValidationError, match="source"):
        text_message(source="user")


def test_message_rejects_duplicate_tool_calls_in_one_message() -> None:
    with pytest.raises(MessageValidationError, match="duplicate tool call id"):
        Message(
            id=MESSAGE_ID,
            role="assistant",
            content=(
                ToolCallPart(CALL_ID, "a", "{}"),
                ToolCallPart(CALL_ID, "b", "{}"),
            ),
            created_at=1,
            source=ModelSource("deepseek", "deepseek-flash"),
        )


def test_from_dict_rejects_unknown_and_missing_fields() -> None:
    data = text_message().to_dict()
    data["extra"] = 1
    with pytest.raises(MessageValidationError, match="unknown field 'extra'"):
        Message.from_dict(data)
    missing = text_message().to_dict()
    del missing["source"]
    with pytest.raises(MessageValidationError, match="lacks required field 'source'"):
        Message.from_dict(missing)
    with pytest.raises(MessageValidationError, match="must be an object"):
        Message.from_dict([])  # type: ignore[arg-type]


def test_part_and_source_parsers_reject_unknown_shapes() -> None:
    with pytest.raises(MessageValidationError, match="unknown content part type"):
        part_from_dict({"type": "audio"})
    with pytest.raises(MessageValidationError, match="unknown field"):
        part_from_dict({"type": "text", "text": "x", "extra": 1})
    with pytest.raises(MessageValidationError, match="unknown message source kind"):
        source_from_dict({"kind": "magic"})


def test_replace_content_keeps_identity_fields() -> None:
    original = text_message()
    updated = replace_content(original, (TextPart("next"),))
    assert updated.id == original.id
    assert updated.role == original.role
    assert updated.created_at == original.created_at
    assert updated.source == original.source
    assert updated.text() == "next"


def test_image_media_type_is_checked() -> None:
    with pytest.raises(MessageValidationError, match="media type"):
        ImagePart("a", "image/tiff")  # type: ignore[arg-type]
