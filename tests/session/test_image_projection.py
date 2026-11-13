"""Tests for the image/offload pure projection (session image offload)."""

from __future__ import annotations

import json

import pytest

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import ImagePart, LogRecord, Message, TextPart
from deeppyro.session import SessionStore, derive
from deeppyro.session.image_projection import (
    IMAGE_OFFLOAD_EVENT,
    OFFLOADED_IMAGE_PREFIX,
    OFFLOADED_IMAGE_SUFFIX,
    apply_image_offloads,
    image_identity,
    offloaded_image_text,
    project_image_offloads,
)
from tests.session import support

EXPECTED_NO_NAME = (
    "[image omitted to fit request image limits; att-1. "
    "No local normalized image path is available; ask the user to attach it again if needed.]"
)


def ref_(attachment_id: str = "att-1", *, name: str | None = None) -> dict[str, object]:
    """A durable image attachment reference as it appears in message content."""
    payload: dict[str, object] = {
        "attachmentId": attachment_id,
        "mediaType": "image/png",
        "bytes": 128,
        "width": 16,
        "height": 16,
    }
    if name is not None:
        payload["name"] = name
    return payload


def image_block(attachment: object) -> dict[str, object]:
    return {"type": "image", "attachment": attachment}


def user_message(message_id: str, *parts: dict[str, object]) -> dict[str, object]:
    return {"id": message_id, "role": "user", "content": list(parts), "source": {"kind": "user"}}


def image_message(message_id: str = "msg_img", *, name: str | None = None) -> dict[str, object]:
    return user_message(message_id, support.text_part("look"), image_block(ref_(name=name)))


def offload_record(seq: int, targets: list[object]) -> LogRecord:
    return support.record(support.event(IMAGE_OFFLOAD_EVENT, seq, {"targets": targets}))


def user_record(seq: int, message_id: str = "msg_img", *, name: str | None = None) -> LogRecord:
    return support.record(support.surface("user/message", seq, image_message(message_id, name=name)))


def wire_message(*parts: dict[str, object]) -> Message:
    return Message.model_validate(user_message("msg_wire", *parts))


def content_types(message: Message) -> list[str]:
    return [part.type for part in message.content]


# ---------------------------------------------------------------------------
# Text parity with DSH
# ---------------------------------------------------------------------------


def test_placeholder_constants_match_dsh_no_path_variant() -> None:
    assert OFFLOADED_IMAGE_PREFIX == "[image omitted to fit request image limits; "
    assert OFFLOADED_IMAGE_SUFFIX == (
        " No local normalized image path is available; ask the user to attach it again if needed.]"
    )
    assert offloaded_image_text({"attachmentId": "att-1"}) == EXPECTED_NO_NAME


def test_image_identity_without_name_uses_attachment_id() -> None:
    assert image_identity({"attachmentId": "att-1"}) == "att-1"
    assert image_identity({"attachmentId": "att-1", "name": None}) == "att-1"
    assert image_identity({"attachmentId": "att-1", "name": ""}) == "att-1"


def test_image_identity_with_name_uses_json_quoting() -> None:
    assert image_identity({"attachmentId": "att-1", "name": "photo.png"}) == '"photo.png" (att-1)'
    assert image_identity({"attachmentId": "att-1", "name": "截图.png"}) == '"截图.png" (att-1)'
    tricky = 'chart "Q1".png'
    expected = json.dumps(tricky, ensure_ascii=False) + " (att-1)"
    assert image_identity({"attachmentId": "att-1", "name": tricky}) == expected


def test_image_identity_requires_an_attachment_id() -> None:
    with pytest.raises(SessionError, match="lacks attachmentId"):
        image_identity({"name": "photo.png"})


def test_offloaded_text_embeds_the_named_identity() -> None:
    text = offloaded_image_text({"attachmentId": "att-9", "name": "diagram.png"})
    assert text == (
        "[image omitted to fit request image limits; "
        '"diagram.png" (att-9). '
        "No local normalized image path is available; ask the user to attach it again if needed.]"
    )


# ---------------------------------------------------------------------------
# apply_image_offloads
# ---------------------------------------------------------------------------


def test_apply_replaces_only_selected_images() -> None:
    message = wire_message(
        support.text_part("before"),
        image_block(ref_("att-1")),
        support.text_part("between"),
        image_block(ref_("att-2")),
    )
    updated = apply_image_offloads(message, {1})
    assert content_types(updated) == ["text", "text", "text", "image"]
    replaced = updated.content[1]
    assert isinstance(replaced, TextPart)
    assert replaced.text == offloaded_image_text(ref_("att-1"))
    assert isinstance(updated.content[3], ImagePart)


def test_apply_does_not_mutate_the_input_message() -> None:
    message = wire_message(support.text_part("x"), image_block(ref_("att-1")))
    updated = apply_image_offloads(message, {1})
    assert updated is not message
    assert isinstance(message.content[1], ImagePart)
    assert isinstance(updated.content[1], TextPart)
    assert updated.id == message.id
    assert updated.role == message.role
    assert updated.source == message.source


def test_apply_bare_string_attachment_falls_back_to_its_id() -> None:
    message = wire_message(image_block("att-bare"))
    updated = apply_image_offloads(message, {0})
    part = updated.content[0]
    assert isinstance(part, TextPart)
    assert part.text == offloaded_image_text({"attachmentId": "att-bare"})
    assert "att-bare." in part.text


def test_apply_ignores_out_of_range_and_non_image_indexes() -> None:
    message = wire_message(support.text_part("x"), image_block(ref_("att-1")))
    updated = apply_image_offloads(message, {0, 99})
    assert content_types(updated) == ["text", "image"]


def test_apply_preserves_every_other_part() -> None:
    message = wire_message(
        support.text_part("text"),
        image_block(ref_("att-1")),
        {"type": "reasoning", "text": "because"},
    )
    updated = apply_image_offloads(message, {1})
    assert updated.content[0] == message.content[0]
    assert updated.content[2] == message.content[2]


def test_apply_with_empty_indexes_keeps_every_part() -> None:
    message = wire_message(support.text_part("x"), image_block(ref_("att-1")))
    updated = apply_image_offloads(message, set())
    assert content_types(updated) == ["text", "image"]


# ---------------------------------------------------------------------------
# project_image_offloads
# ---------------------------------------------------------------------------


def test_project_collects_a_single_record() -> None:
    records = [offload_record(1, [{"seq": 0, "imageIndexes": [1]}])]
    assert project_image_offloads(records) == {0: {1}}


def test_project_unions_records_for_the_same_seq() -> None:
    records = [
        offload_record(1, [{"seq": 0, "imageIndexes": [0]}]),
        offload_record(2, [{"seq": 0, "imageIndexes": [0, 2]}]),
    ]
    assert project_image_offloads(records) == {0: {0, 2}}


def test_project_collects_multiple_targets_and_seqs() -> None:
    records = [
        offload_record(
            2,
            [
                {"seq": 0, "imageIndexes": [1]},
                {"seq": 1, "imageIndexes": [2, 3]},
            ],
        )
    ]
    assert project_image_offloads(records) == {0: {1}, 1: {2, 3}}


def test_project_accepts_empty_targets() -> None:
    assert project_image_offloads([offload_record(1, [])]) == {}


def test_project_ignores_other_record_types() -> None:
    records = [
        support.record(support.event("turn/start", 0, {"turn": 1})),
        offload_record(1, [{"seq": 0, "imageIndexes": [1]}]),
    ]
    assert project_image_offloads(records) == {0: {1}}


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"targets": "nope"},
        {"targets": ["nope"]},
        {"targets": [{"imageIndexes": [0]}]},
        {"targets": [{"seq": True, "imageIndexes": [0]}]},
        {"targets": [{"seq": -1, "imageIndexes": [0]}]},
        {"targets": [{"seq": 0}]},
        {"targets": [{"seq": 0, "imageIndexes": "nope"}]},
        {"targets": [{"seq": 0, "imageIndexes": [True]}]},
        {"targets": [{"seq": 0, "imageIndexes": [-2]}]},
    ],
)
def test_project_strict_mode_rejects_invalid_records(data: dict[str, object]) -> None:
    record = support.record(support.event(IMAGE_OFFLOAD_EVENT, 5, data))
    with pytest.raises(SessionError, match="invalid image/offload record at seq 5"):
        project_image_offloads([record])


def test_project_tolerant_mode_skips_invalid_records() -> None:
    valid = offload_record(2, [{"seq": 0, "imageIndexes": [1]}])
    invalid = support.record(support.event(IMAGE_OFFLOAD_EVENT, 3, {"targets": "nope"}))
    assert project_image_offloads([valid, invalid], strict=False) == {0: {1}}


# ---------------------------------------------------------------------------
# derive_messages integration
# ---------------------------------------------------------------------------


def test_derive_messages_applies_the_offload_to_its_target_node() -> None:
    records = [user_record(0), offload_record(1, [{"seq": 0, "imageIndexes": [1]}])]
    messages = derive.derive_messages(records)
    assert len(messages) == 1
    assert content_types(messages[0]) == ["text", "text"]
    part = messages[0].content[1]
    assert isinstance(part, TextPart)
    assert part.text == EXPECTED_NO_NAME
    assert messages[0].text() == "look" + EXPECTED_NO_NAME


def test_derive_messages_offloads_several_images_across_messages() -> None:
    records = [
        user_record(0, "msg_a", name="a.png"),
        user_record(1, "msg_b"),
        offload_record(
            2,
            [
                {"seq": 0, "imageIndexes": [1]},
                {"seq": 1, "imageIndexes": [1]},
            ],
        ),
    ]
    messages = derive.derive_messages(records)
    assert content_types(messages[0]) == ["text", "text"]
    assert content_types(messages[1]) == ["text", "text"]
    first = messages[0].content[1]
    assert isinstance(first, TextPart)
    assert first.text == offloaded_image_text({"attachmentId": "att-1", "name": "a.png"})


def test_derive_messages_ignores_targets_absent_from_the_surface() -> None:
    records = [user_record(0), offload_record(1, [{"seq": 99, "imageIndexes": [1]}])]
    messages = derive.derive_messages(records)
    assert content_types(messages[0]) == ["text", "image"]


def test_derive_messages_ignores_indexes_that_are_not_images() -> None:
    records = [
        support.record(support.surface("user/message", 0, support.user_message("msg_text", "hi"))),
        offload_record(1, [{"seq": 0, "imageIndexes": [1]}]),
    ]
    messages = derive.derive_messages(records)
    assert messages[0].text() == "hi"


def test_derive_messages_is_idempotent_for_duplicate_offloads() -> None:
    records = [
        user_record(0),
        offload_record(1, [{"seq": 0, "imageIndexes": [1, 1]}]),
        offload_record(2, [{"seq": 0, "imageIndexes": [1]}]),
    ]
    messages = derive.derive_messages(records)
    assert content_types(messages[0]) == ["text", "text"]


def test_image_offload_never_becomes_a_surface_node() -> None:
    record = offload_record(0, [])
    assert derive.fold_surface([record]) == []
    assert derive.plan_surface_transition([], record, strict=True, records_by_seq={}) == (
        derive.SKIP,
        None,
        None,
    )


# ---------------------------------------------------------------------------
# Real Session integration (durable append + reload)
# ---------------------------------------------------------------------------


def test_session_round_trip_applies_the_projection(tmp_path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(cwd=str(tmp_path))
    user_seq = session.append("user/message", image_message())
    session.append(
        IMAGE_OFFLOAD_EVENT,
        {"targets": [{"seq": user_seq, "imageIndexes": [1]}]},
    )
    messages = session.derive_messages()
    assert content_types(messages[0]) == ["text", "text"]
    reopened = store.open(session.session_id)
    assert reopened.derive_messages() == messages
    assert IMAGE_OFFLOAD_EVENT in [record.type for record in reopened.records]


def test_session_strict_derive_rejects_malformed_offload_records(tmp_path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(cwd=str(tmp_path))
    session.append("user/message", image_message())
    session.append(IMAGE_OFFLOAD_EVENT, {"targets": "nope"})
    with pytest.raises(SessionError, match="invalid image/offload record"):
        session.derive_messages()
    tolerant = session.derive_messages(strict=False)
    assert content_types(tolerant[0]) == ["text", "image"]
