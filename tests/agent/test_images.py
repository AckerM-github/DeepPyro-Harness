"""Pure tests for the request-side image planning in deeppyro.agent.images."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from deeppyro.agent.images import (
    ImageRequestPlan,
    expand_image_attachments,
    plan_request_images,
    surface_messages_with_seq,
)
from deeppyro.core.errors import SessionError
from deeppyro.core.ids import new_message_id
from deeppyro.core.wire import ImagePart, Message, TextPart
from deeppyro.llm.image_budget import ImageBudget
from deeppyro.session.image_projection import offloaded_image_text

from .fakes import FakeSessionLog

PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"
PNG2 = b"\x89PNG\r\n\x1a\nsecond-image"
REF = {"attachmentId": "att-1", "mediaType": "image/png", "bytes": len(PNG)}
REF2 = {"attachmentId": "att-2", "mediaType": "image/png", "bytes": len(PNG2)}


def data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def user_message(*, text: str | None = None, images: tuple[Any, ...] = ()) -> Message:
    content: list[Any] = []
    if text is not None:
        content.append(TextPart(text=text))
    for image in images:
        content.append(ImagePart(attachment=image))
    return Message(role="user", content=content, id=new_message_id(), source={"kind": "user"})


@dataclass
class FakeAttachments:
    """Minimal resolver: wire-shaped refs (bare id or ref dict) are accepted."""

    blobs: dict[str, bytes]
    reads: list[str] = field(default_factory=list)

    def read_image_bytes(self, ref: Any) -> bytes:
        if isinstance(ref, str):
            attachment_id = ref
        elif isinstance(ref, dict):
            attachment_id = str(ref.get("attachmentId"))
        else:
            attachment_id = str(getattr(ref, "attachmentId", ref))
        self.reads.append(attachment_id)
        return self.blobs[attachment_id]

    def lookup(self, attachment_id: str) -> Any:
        return None


@dataclass
class TypedRef:
    """Stand-in for attachments.ImageAttachmentRef (typed value, not a mapping)."""

    attachmentId: str
    mediaType: str = "image/png"
    bytes: int = 0
    width: int = 1
    height: int = 1


class TypedAttachments:
    """Resolver that only accepts typed references, like the real store."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.reads: list[str] = []
        self.refs = {key: TypedRef(key, bytes=len(value)) for key, value in blobs.items()}

    def read_image_bytes(self, ref: Any) -> bytes:
        attachment_id = ref.attachmentId  # AttributeError for a wire mapping
        self.reads.append(attachment_id)
        return self.blobs[attachment_id]

    def lookup(self, attachment_id: str) -> Any:
        return self.refs.get(attachment_id)


def make_log(*messages: Message) -> FakeSessionLog:
    log = FakeSessionLog(cwd=".")
    for message in messages:
        log.append("user/message", message.model_dump())
    return log


def test_surface_messages_pair_each_message_with_its_record_seq() -> None:
    log = make_log(user_message(text="one"), user_message(text="two"))

    entries = surface_messages_with_seq(log.records)

    assert [seq for seq, _ in entries] == [1, 2]
    assert [message.text() for _, message in entries] == ["one", "two"]


def test_surface_messages_apply_existing_offload_projection() -> None:
    log = make_log(user_message(text="see", images=(REF,)))
    log.append("image/offload", {"targets": [{"seq": 1, "imageIndexes": [1]}]})

    entries = surface_messages_with_seq(log.records)

    content = entries[0][1].content
    assert isinstance(content[1], TextPart)
    assert content[1].text == offloaded_image_text(REF)


def test_surface_messages_use_explicit_offloads_without_records() -> None:
    log = make_log(user_message(images=(REF,)))

    entries = surface_messages_with_seq(log.records, offloads={1: {0}})

    assert isinstance(entries[0][1].content[0], TextPart)
    assert entries[0][1].content[0].text == offloaded_image_text(REF)


def test_surface_messages_ignore_offload_targets_off_the_surface() -> None:
    log = make_log(user_message(images=(REF,)))
    log.append("image/offload", {"targets": [{"seq": 99, "imageIndexes": [0]}]})

    entries = surface_messages_with_seq(log.records)

    assert isinstance(entries[0][1].content[0], ImagePart)


def test_expand_inlines_references_and_copies_messages() -> None:
    message = user_message(text="look", images=(REF,))
    attachments = FakeAttachments({"att-1": PNG})

    expanded = expand_image_attachments([message], attachments)

    assert expanded[0] is not message
    assert expanded[0].content is not message.content
    assert expanded[0].content[0].text == "look"
    assert expanded[0].content[1].attachment == {"url": data_url(PNG)}
    # The durable message is untouched.
    assert message.content[1].attachment == REF


def test_expand_reads_a_duplicate_reference_once() -> None:
    attachments = FakeAttachments({"att-1": PNG})
    messages = [user_message(images=(REF,)), user_message(images=(REF,))]

    expanded = expand_image_attachments(messages, attachments)

    assert attachments.reads == ["att-1"]
    assert expanded[0].content[0].attachment == {"url": data_url(PNG)}
    assert expanded[1].content[0].attachment == {"url": data_url(PNG)}


def test_expand_leaves_ready_urls_and_unresolvable_refs_untouched() -> None:
    ready = {"url": data_url(PNG2)}
    missing = {"attachmentId": "att-missing"}
    messages = [user_message(images=(ready, missing))]
    attachments = FakeAttachments({"att-1": PNG})

    expanded = expand_image_attachments(messages, attachments)

    assert expanded[0].content[0].attachment == ready
    assert expanded[0].content[1].attachment == missing
    assert attachments.reads == ["att-missing"]


def test_expand_resolves_bare_ids_through_the_typed_store_lookup() -> None:
    attachments = TypedAttachments({"att-1": PNG})
    message = user_message(images=("att-1",))

    expanded = expand_image_attachments([message], attachments)

    assert expanded[0].content[0].attachment == {"url": data_url(PNG)}
    assert attachments.reads == ["att-1"]


def test_expand_without_attachments_is_a_copy_only() -> None:
    message = user_message(images=(REF,))

    expanded = expand_image_attachments([message], None)

    assert expanded[0] is not message
    assert expanded[0].content[0].attachment == REF


def test_plan_without_attachments_keeps_references_and_writes_nothing() -> None:
    log = make_log(user_message(images=(REF,)))

    plan = plan_request_images(log.records, attachments=None)

    assert plan.offload_targets == []
    assert plan.offloaded == 0
    assert plan.record_data() == {}
    assert isinstance(plan.messages[0].content[0], ImagePart)


def test_plan_without_records_is_empty() -> None:
    plan = plan_request_images([], attachments=FakeAttachments({}))

    assert plan == ImageRequestPlan(messages=[])


def test_plan_fits_budget_and_only_expands() -> None:
    log = make_log(user_message(images=(REF,)), user_message(images=(REF2,)))

    plan = plan_request_images(log.records, attachments=FakeAttachments({"att-1": PNG, "att-2": PNG2}))

    assert plan.offload_targets == []
    assert plan.offloaded == 0
    assert plan.messages[0].content[0].attachment == {"url": data_url(PNG)}
    assert plan.messages[1].content[0].attachment == {"url": data_url(PNG2)}


def test_plan_offloads_the_oldest_occurrence_when_the_count_budget_is_exceeded() -> None:
    log = make_log(user_message(images=(REF,)), user_message(images=(REF2,)))
    attachments = FakeAttachments({"att-1": PNG, "att-2": PNG2})
    budget = ImageBudget(max_images=1, offload_count_quantum=1)

    plan = plan_request_images(log.records, attachments=attachments, budget=budget)

    assert plan.offload_targets == [{"seq": 1, "imageIndexes": [0]}]
    assert plan.offloaded == 1
    assert plan.record_data() == {"targets": [{"seq": 1, "imageIndexes": [0]}]}
    assert isinstance(plan.messages[0].content[0], TextPart)
    assert plan.messages[0].content[0].text == offloaded_image_text(REF)
    assert plan.messages[1].content[0].attachment == {"url": data_url(PNG2)}


def test_plan_groups_multiple_targets_on_one_record_seq() -> None:
    first = user_message(images=(REF, REF))
    second = user_message(images=(REF2,))
    log = make_log(first, second)
    attachments = FakeAttachments({"att-1": PNG, "att-2": PNG2})
    budget = ImageBudget(max_images=2, offload_count_quantum=2)

    plan = plan_request_images(log.records, attachments=attachments, budget=budget)

    assert plan.offload_targets == [{"seq": 1, "imageIndexes": [0, 1]}]
    assert plan.offloaded == 2
    assert all(isinstance(part, TextPart) for part in plan.messages[0].content)
    assert plan.messages[1].content[0].attachment == {"url": data_url(PNG2)}


def test_plan_counts_declared_bytes_for_an_unreadable_reference() -> None:
    ref = {"attachmentId": "att-missing", "mediaType": "image/png", "bytes": 9}
    log = make_log(user_message(images=(ref,)))
    budget = ImageBudget(max_inline_bytes=8, inline_offload_byte_quantum=8)

    plan = plan_request_images(log.records, attachments=FakeAttachments({}), budget=budget)

    assert plan.offload_targets == [{"seq": 1, "imageIndexes": [0]}]
    assert isinstance(plan.messages[0].content[0], TextPart)


def test_plan_is_idempotent_once_the_offload_record_is_persisted() -> None:
    log = make_log(user_message(images=(REF,)), user_message(images=(REF2,)))
    attachments = FakeAttachments({"att-1": PNG, "att-2": PNG2})
    budget = ImageBudget(max_images=1, offload_count_quantum=1)
    first = plan_request_images(log.records, attachments=attachments, budget=budget)
    assert first.offload_targets
    log.append("image/offload", first.record_data())

    second = plan_request_images(log.records, attachments=attachments, budget=budget)

    assert second.offload_targets == []
    assert second.offloaded == 0
    assert isinstance(second.messages[0].content[0], TextPart)
    assert second.messages[0].content[0].text == offloaded_image_text(REF)
    assert second.messages[1].content[0].attachment == {"url": data_url(PNG2)}


def test_plan_strict_malformed_offload_record_propagates() -> None:
    log = make_log(user_message(images=(REF,)))
    log.append("image/offload", {"targets": [{"seq": "1", "imageIndexes": [0]}]})

    with pytest.raises(SessionError):
        plan_request_images(log.records, attachments=FakeAttachments({"att-1": PNG}))


def test_record_data_returns_json_native_targets() -> None:
    plan = ImageRequestPlan(
        messages=[],
        offload_targets=[{"seq": 3, "imageIndexes": [1, 2]}],
        offloaded=2,
    )

    assert plan.record_data() == {"targets": [{"seq": 3, "imageIndexes": [1, 2]}]}
    assert json.loads(json.dumps(plan.record_data())) == plan.record_data()


def test_plan_never_writes_base64_into_the_record_payload() -> None:
    log = make_log(user_message(images=(REF,)), user_message(images=(REF2,)))
    budget = ImageBudget(max_images=1, offload_count_quantum=1)

    plan = plan_request_images(
        log.records, attachments=FakeAttachments({"att-1": PNG, "att-2": PNG2}), budget=budget
    )

    payload = json.dumps(plan.record_data())
    assert "base64" not in payload
    assert "data:image" not in payload
    assert "attachmentId" not in payload
