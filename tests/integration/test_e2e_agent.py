"""End-to-end integration: real Session log + real ToolRegistry + AgentSession.

This is the Lead acceptance test for the turn/step loop.  It uses the durable
session.v4 log (not a fake log), the real built-in tool pipeline, and a
scripted in-process provider, then asserts the exact record sequence, the
surface-derived messages, and the "append durably before emitting" ordering
guarantee from INTERFACES section 7.
"""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.session import SessionStore
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.registry import ToolRegistry
from tests.integration.fake_provider import Response, ScriptedProvider, tool_response


class EchoTool(BaseTool):
    """Minimal parallel-safe tool that never touches the filesystem."""

    name = "echo"
    description = "Echo the supplied text back."
    parallel_safe = True
    parameters = make_parameters({"text": {"type": "string"}}, ["text"])

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        return ToolResult(text=f"echo:{args.get('text', '')}")


class RecordingSink:
    """Event sink that snapshots the newest durable record at emit time."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.snapshots: list[str | None] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.snapshots.append(None)


def make_sink(session: Any) -> RecordingSink:
    sink = RecordingSink()

    def emit(event: dict[str, Any]) -> None:
        sink.events.append(event)
        sink.snapshots.append(session.records[-1].type if session.records else None)

    sink.emit = emit  # type: ignore[method-assign]
    return sink


@pytest.mark.asyncio
async def test_two_step_turn_with_tool_call(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    session = store.create(tmp_path)
    tool = EchoTool()
    provider = ScriptedProvider(
        [
            tool_response("call-1", "echo", '{"text":"hi"}', text="checking"),
            Response(text="done"),
        ]
    )
    registry = ToolRegistry([tool])
    sink = make_sink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=registry,
        options=AgentOptions(max_steps=5, model="scripted-model"),
        sink=sink,
        permission=None,
        cwd=tmp_path,
    )

    result = await agent.send("please echo hi")

    # -- the turn's durable record grammar ---------------------------------
    assert result == "done"
    assert [record.type for record in session.records] == [
        "turn/start",
        "step/start",
        "system/message",
        "user/message",
        "request/header",
        "request/context",
        "assistant/message",
        "tool/call",
        "tool/result",
        "step/end",
        "step/start",
        "assistant/message",
        "step/end",
        "turn/end",
    ]
    assert [record.seq for record in session.records] == list(range(len(session.records)))
    assert session.records[-1].data["reason"] == {"kind": "completed"}
    assert session.turn_count() == 1
    assert session.last_step() == (1, 2)

    # -- the provider saw the right requests -------------------------------
    assert len(provider.requests) == 2
    first, second = provider.requests
    assert [schema.name for schema in first.tools] == ["echo"]
    assert first.provider == "scripted"
    assert second.messages[-1].role == "tool"
    assert "echo:hi" in second.messages[-1].text()
    assert second.messages[-2].tool_calls()[0].id == "call-1"

    # -- derived messages follow the surface fold --------------------------
    messages = session.derive_messages()
    assert [message.role for message in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[1].text() == "please echo hi"
    assert messages[2].text() == "checking"
    assert messages[2].tool_calls()[0].name == "echo"
    assert messages[3].isError is False
    assert messages[3].toolCallId == "call-1"
    assert messages[4].text() == "done"

    # -- tool/call records carry the raw JSON arguments --------------------
    call_records = [record for record in session.records if record.type == "tool/call"]
    assert [record.data["name"] for record in call_records] == ["echo"]
    assert call_records[0].data["arguments"] == '{"text":"hi"}'
    assert tool.calls == [{"text": "hi"}]

    # -- append happens before the matching UI event -----------------------
    event_types = [event["type"] for event in sink.events]
    # The CLI emits the single `session` event; a bare AgentSession does not.
    assert "session" not in event_types
    # A UI-only step hint is emitted right after each durable step/start record.
    assert event_types[0] == "step"
    assert "status" in event_types
    assert event_types.index("tool_call") < event_types.index("tool_result")
    assert event_types[-2:] == ["final", "status"]
    assert sink.events[-1] == {"type": "status", "phase": "done"}
    for index, event in enumerate(sink.events):
        if event["type"] == "tool_call":
            assert sink.snapshots[index] == "tool/call"
        elif event["type"] == "tool_result":
            assert sink.snapshots[index] == "tool/result"
        elif event["type"] == "final":
            assert sink.snapshots[index] == "turn/end"

    # -- durability: reopen the log and derive the same messages -----------
    reopened = store.open(session.session_id)
    assert [record.type for record in reopened.records] == [record.type for record in session.records]
    assert [message.role for message in reopened.derive_messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    header_records = [record for record in reopened.records if record.type == "request/header"]
    assert [entry["name"] for entry in header_records[0].data["header"]["tools"]] == ["echo"]
    session.close()
    reopened.close()
