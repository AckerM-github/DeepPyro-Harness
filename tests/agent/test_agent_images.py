"""End-to-end image request planning through the AgentSession step loop."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.ids import new_message_id
from deeppyro.core.protocols import TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.core.wire import ImagePart, Message, TextPart
from deeppyro.llm.image_budget import ImageBudget
from deeppyro.session.image_projection import OFFLOADED_IMAGE_PREFIX, offloaded_image_text

from .fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    FakeTool,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
    usage,
)

PNG = b"\x89PNG\r\n\x1a\nfirst-image-bytes"
PNG2 = b"\x89PNG\r\n\x1a\nsecond-image-bytes"
REF = {"attachmentId": "att-1", "mediaType": "image/png", "bytes": len(PNG)}
REF2 = {"attachmentId": "att-2", "mediaType": "image/png", "bytes": len(PNG2)}


def data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


@dataclass
class FakeAttachments:
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


def user_message(*, text: str | None = None, images: tuple[Any, ...] = ()) -> Message:
    content: list[Any] = []
    if text is not None:
        content.append(TextPart(text=text))
    for image in images:
        content.append(ImagePart(attachment=image))
    return Message(role="user", content=content, id=new_message_id(), source={"kind": "user"})


def make_agent(
    tmp_path: Path,
    script: list[ScriptedTurn],
    *,
    services: dict[str, Any] | None = None,
) -> tuple[FakeSessionLog, ScriptedProvider, RecordingSink, AgentSession]:
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        services=services,
    )
    return session, provider, sink, agent


def seed_user_message(session: FakeSessionLog, message: Message) -> int:
    return session.append("user/message", message.model_dump())


def request_parts(request: Any, part_type: type) -> list[Any]:
    return [part for message in request.messages for part in message.content if isinstance(part, part_type)]


def placeholder_texts(request: Any) -> list[str]:
    return [
        part.text for part in request_parts(request, TextPart) if part.text.startswith(OFFLOADED_IMAGE_PREFIX)
    ]


async def test_request_carries_inline_data_url_and_log_keeps_the_reference(tmp_path: Path) -> None:
    script = [ScriptedTurn(events=[TextDelta("ok"), usage(), finish("stop")])]
    session, provider, _sink, agent = make_agent(
        tmp_path, script, services={"attachments": FakeAttachments({"att-1": PNG})}
    )
    seed_user_message(session, user_message(images=(REF,)))

    text = await agent.send("describe this")

    assert text == "ok"
    request = provider.requests[0]
    images = request_parts(request, ImagePart)
    assert len(images) == 1
    assert images[0].attachment == {"url": data_url(PNG)}
    assert session.records_of("image/offload") == []
    # The durable log still only carries the reference.
    derived = session.derive_messages()
    durable_images = [part for message in derived for part in message.content if isinstance(part, ImagePart)]
    assert durable_images[0].attachment == REF
    assert "data:image" not in json.dumps([record.data for record in session.records])


async def test_budget_exceeded_appends_one_offload_record_before_the_request(tmp_path: Path) -> None:
    script = [ScriptedTurn(events=[TextDelta("compared"), usage(), finish("stop")])]
    attachments = FakeAttachments({"att-1": PNG, "att-2": PNG2})
    budget = ImageBudget(max_images=1, offload_count_quantum=1)
    session, provider, _sink, agent = make_agent(
        tmp_path, script, services={"attachments": attachments, "image_budget": budget}
    )
    seeded_seq = seed_user_message(session, user_message(images=(REF, REF2)))

    text = await agent.send("compare")

    assert text == "compared"
    offloads = session.records_of("image/offload")
    assert len(offloads) == 1
    assert offloads[0].data == {"targets": [{"seq": seeded_seq, "imageIndexes": [0]}]}
    assert offloads[0].surfaceOp is None
    assert offloads[0].sourceEventSeqs is None

    request = provider.requests[0]
    assert placeholder_texts(request) == [offloaded_image_text(REF)]
    inline = request_parts(request, ImagePart)
    assert len(inline) == 1
    assert inline[0].attachment == {"url": data_url(PNG2)}

    types = session.types()
    assert types.index("request/context") < types.index("image/offload")
    assert types.index("image/offload") < types.index("assistant/message")
    # The durable log never receives the base64 payload.
    assert "data:image" not in json.dumps([record.data for record in session.records])


async def test_offload_record_is_written_once_across_tool_steps(tmp_path: Path) -> None:
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call-1", "echo"),
                ToolCallArgsDelta("call-1", '{"text": "hi"}'),
                usage(),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), usage(), finish("stop")]),
    ]
    attachments = FakeAttachments({"att-1": PNG, "att-2": PNG2})
    budget = ImageBudget(max_images=1, offload_count_quantum=1)
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry([FakeTool("echo", text="echo:ok")]),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        services={"attachments": attachments, "image_budget": budget},
    )
    seed_user_message(session, user_message(images=(REF, REF2)))

    text = await agent.send("use the tool")

    assert text == "done"
    assert len(provider.requests) == 2
    assert len(session.records_of("image/offload")) == 1
    for request in provider.requests:
        assert placeholder_texts(request) == [offloaded_image_text(REF)]
        inline = request_parts(request, ImagePart)
        assert len(inline) == 1
        assert inline[0].attachment == {"url": data_url(PNG2)}


async def test_without_attachments_service_references_reach_the_provider(tmp_path: Path) -> None:
    script = [ScriptedTurn(events=[TextDelta("ok"), finish("stop")])]
    session, provider, _sink, agent = make_agent(tmp_path, script)
    seed_user_message(session, user_message(images=(REF,)))

    await agent.send("describe")

    images = request_parts(provider.requests[0], ImagePart)
    assert len(images) == 1
    assert images[0].attachment == REF
    assert session.records_of("image/offload") == []


async def test_provider_image_budget_is_used_when_no_service_override(tmp_path: Path) -> None:
    script = [ScriptedTurn(events=[TextDelta("ok"), finish("stop")])]
    session, provider, _sink, agent = make_agent(
        tmp_path, script, services={"attachments": FakeAttachments({"att-1": PNG, "att-2": PNG2})}
    )
    provider.image_budget = ImageBudget(max_images=1, offload_count_quantum=1)  # type: ignore[attr-defined]
    seed_user_message(session, user_message(images=(REF, REF2)))

    await agent.send("compare")

    assert len(session.records_of("image/offload")) == 1
    assert placeholder_texts(provider.requests[0]) == [offloaded_image_text(REF)]
