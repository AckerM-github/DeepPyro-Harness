"""Tests for the shared native turn-message conversion helpers."""

from __future__ import annotations

import dataclasses

import pytest

from deeppyro.agent.errors import ProviderStreamError
from deeppyro.agent.turn_messages import (
    assistant_message,
    image_part_from_wire,
    notice_message,
    tool_message,
    tool_spec,
    tool_specs,
    user_message,
)
from deeppyro.core.llm import ModelResponse, NativeToolCall, StopReason, Usage
from deeppyro.core.messages import (
    ImagePart,
    ModelSource,
    PluginSource,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.core.protocols import ToolResult


class _FakeTool:
    """Minimal structural Tool for spec conversion."""

    def __init__(self, name: str = "read", *, output_schema: dict | None = None) -> None:
        self.name = name
        self.description = "read a file"
        self.parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
        self.parallel_safe = True
        self.output_schema = output_schema

    async def run(self, args: dict, ctx: object) -> ToolResult:
        return ToolResult(text="")


def _response(**overrides: object) -> ModelResponse:
    base: dict[str, object] = {
        "text": "hello",
        "reasoning": "thinking",
        "tool_calls": (NativeToolCall(call_id="call_" + "a" * 16, name="read", arguments='{"path": "x"}'),),
        "finish_reason": StopReason.TOOL_CALLS,
        "usage": Usage(input_tokens=3, output_tokens=2),
        "latency_ms": 7,
    }
    base.update(overrides)
    return ModelResponse(**base)  # type: ignore[arg-type]


def test_user_message_defaults_and_explicit_fields() -> None:
    message = user_message("hello")
    assert message.role == "user"
    assert message.text() == "hello"
    assert isinstance(message.source, UserSource)
    assert message.created_at > 0

    plugin = notice_message("notice text", plugin="scheduler")
    explicit = user_message("again", source=plugin.source, created_at=123)
    assert explicit.created_at == 123
    assert isinstance(explicit.source, PluginSource)
    assert explicit.source.plugin == "scheduler"


def test_user_message_rejects_empty_text() -> None:
    with pytest.raises(ValueError):
        user_message("")
    with pytest.raises(ValueError):
        user_message(5)  # type: ignore[arg-type]


def test_assistant_message_maps_parts_and_source() -> None:
    message = assistant_message(_response(), provider="local", model="m")
    assert message.role == "assistant"
    assert [type(part) for part in message.content] == [ReasoningPart, TextPart, ToolCallPart]
    assert message.content[0].text == "thinking"
    assert message.content[2].id == "call_" + "a" * 16
    assert isinstance(message.source, ModelSource)
    assert message.source.provider == "local"
    assert message.source.model == "m"


def test_assistant_message_rejects_empty_response() -> None:
    empty = ModelResponse(
        text="",
        reasoning="",
        tool_calls=(),
        finish_reason=StopReason.STOP,
        usage=Usage(),
        latency_ms=0,
    )
    with pytest.raises(ProviderStreamError):
        assistant_message(empty, provider="local", model="m")


def test_tool_message_maps_text_images_and_error_flag() -> None:
    result = ToolResult(
        text="",
        is_error=True,
        parts=[{"type": "image", "attachment": "att_1", "media_type": "image/jpeg"}],
    )
    message = tool_message("call_" + "b" * 16, result)
    assert message.role == "tool"
    assert message.tool_call_id == "call_" + "b" * 16
    assert isinstance(message.source, ToolSource)
    assert message.source.call_id == message.tool_call_id
    assert message.is_error is True
    assert message.content[0].text == " "
    images = [part for part in message.content if isinstance(part, ImagePart)]
    assert len(images) == 1
    assert images[0].attachment == "att_1"
    assert images[0].media_type == "image/jpeg"


def test_tool_message_rejects_empty_call_id() -> None:
    with pytest.raises(ValueError):
        tool_message("", ToolResult(text="x"))


def test_image_part_from_wire_accepts_supported_shapes() -> None:
    plain = image_part_from_wire({"type": "image", "attachment": "http://example/a.png"})
    assert plain.attachment == "http://example/a.png"
    assert plain.media_type == "image/png"

    data_url = image_part_from_wire({"attachment": "data:image/webp;base64,AAAA"})
    assert data_url.media_type == "image/webp"

    reference = image_part_from_wire({"attachment": {"id": "att_9"}, "media_type": "image/gif"})
    assert reference.attachment == "att_9"
    assert reference.media_type == "image/gif"

    bare = image_part_from_wire({"attachment": "att_2"})
    assert bare.media_type == "image/png"

    class _Part:
        def to_dict(self) -> dict[str, str]:
            return {"type": "image", "attachment": "att_3", "media_type": "image/jpeg"}

    assert image_part_from_wire(_Part()).attachment == "att_3"


def test_image_part_from_wire_rejects_unsupported_shapes() -> None:
    with pytest.raises(ValueError):
        image_part_from_wire({"type": "text", "attachment": "x"})
    with pytest.raises(ValueError):
        image_part_from_wire({"attachment": "att_1", "media_type": "application/pdf"})
    with pytest.raises(ValueError):
        image_part_from_wire({"type": "image"})
    with pytest.raises(ValueError):
        image_part_from_wire("not-a-part")


def test_tool_spec_copies_parameters_and_output_schema() -> None:
    tool = _FakeTool("write", output_schema={"type": "object"})
    spec = tool_spec(tool)
    assert spec.name == "write"
    assert spec.description == "read a file"
    assert spec.parameters["properties"]["path"]["type"] == "string"
    assert spec.output == {"type": "object"}
    tool.parameters["properties"]["path"]["type"] = "number"
    assert spec.parameters["properties"]["path"]["type"] == "string"
    assert [item.name for item in tool_specs([tool, _FakeTool("read")])] == ["write", "read"]


def test_notice_message_requires_plugin_and_text() -> None:
    message = notice_message("directory changed", plugin="working-directory", form="notice")
    assert isinstance(message.source, PluginSource)
    assert message.source.plugin == "working-directory"
    assert message.text() == "directory changed"
    with pytest.raises(ValueError):
        notice_message("", plugin="p")
    with pytest.raises(ValueError):
        notice_message("text", plugin="")
    with pytest.raises(ValueError):
        notice_message("text", plugin="p", form="")


def test_messages_use_schema_valid_roles() -> None:
    for message in (user_message("u"), assistant_message(_response(), provider="p", model="m")):
        assert message.role in {"user", "assistant"}
        assert message.tool_call_id is None
    tool = tool_message("call_" + "c" * 16, ToolResult(text="ok"))
    assert tool.role == "tool"
    assert dataclasses.is_dataclass(tool)
