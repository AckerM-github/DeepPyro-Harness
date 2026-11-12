"""Fixture-driven tests: real DSH records must convert to valid chat payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.protocols import ToolSchema
from deeppyro.core.wire import Message, ToolCallPart
from deeppyro.llm.messages import message_to_chat, tool_to_json

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
BY_TYPE = FIXTURES / "records" / "by-type"

if not BY_TYPE.is_dir():
    pytest.skip(
        "real DSH fixtures not present; run: node tools/gen_fixtures.mjs",
        allow_module_level=True,
    )


def _record(name: str) -> dict[str, Any]:
    line = (BY_TYPE / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)


def test_request_header_tools_convert_to_openai_tools() -> None:
    header = _record("request_header")["data"]["header"]
    raw_tools = header["tools"]
    assert len(raw_tools) >= 60, "fixture should carry the full tool catalog"
    converted = [tool_to_json(ToolSchema.model_validate(tool)) for tool in raw_tools]
    for tool in converted:
        assert tool["type"] == "function"
        assert tool["function"]["name"]
        assert isinstance(tool["function"]["parameters"], dict)


def test_assistant_message_from_fixture_converts() -> None:
    message = Message.model_validate(_record("assistant_message")["data"]["message"])
    converted = message_to_chat(message)
    assert converted is not None
    assert converted["role"] == "assistant"
    assert converted["content"]
    assert converted["reasoning_content"]
    assert isinstance(converted["reasoning_content"], str)


def test_tool_call_record_arguments_stay_a_json_string() -> None:
    data = _record("tool_call")["data"]
    part = ToolCallPart.model_validate(
        {"type": "tool-call", "id": data["callId"], "name": data["name"], "arguments": data["arguments"]}
    )
    message = Message(role="assistant", content=[part])
    converted = message_to_chat(message)
    assert converted is not None
    call = converted["tool_calls"][0]
    assert call["id"] == data["callId"]
    # Byte-for-byte fidelity matters for prompt-cache replay: no re-serialisation.
    assert call["function"]["arguments"] == data["arguments"]
    assert isinstance(call["function"]["arguments"], str)
    json.loads(call["function"]["arguments"])  # still valid JSON


def test_tool_result_message_from_fixture_converts() -> None:
    message = Message.model_validate(_record("tool_result")["data"]["message"])
    converted = message_to_chat(message)
    assert converted is not None
    assert converted["role"] == "tool"
    assert converted["tool_call_id"] == message.toolCallId
    assert converted["content"]
