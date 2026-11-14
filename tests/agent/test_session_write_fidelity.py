"""Write-fidelity tests: request records, tool errors, and abort results.

These pin the durable shapes to the reference session log: request/header and
request/context carry no turn/step position, tool/result may carry a structured
error, and a cancelled turn records one tool/result per outstanding call (the
dispatched ones as ABORTED, the never-dispatched ones as
ABORTED_BEFORE_DISPATCH with a synthesized tool/call so the pair stays legal).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.agent.request import build_header
from deeppyro.agent.session import ToolCallDraft
from deeppyro.core.protocols import ToolCallArgsDelta, ToolCallStart, ToolResult
from deeppyro.session import SessionStore

from .fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    FakeTool,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)

TOOLS = [{"name": "read", "description": "read a file", "parameters": {"type": "object"}}]


def make_agent(tmp_path: Path, registry: FakeRegistry, script: list[ScriptedTurn]):
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=registry,
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
    )
    return session, provider, sink, agent


class StructuredErrorTool:
    """A tool that reports a structured error beside its text result."""

    name = "failing"
    description = "always fails"
    parameters: ClassVar[dict[str, object]] = {"type": "object", "properties": {}}
    parallel_safe = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(self, args: dict[str, object], ctx: object) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult(
            text="Error: boom",
            is_error=True,
            meta={"cwd": "C:/work"},
            error={"name": "BoomError", "code": "BOOM"},
        )


async def test_tool_result_persists_structured_error(tmp_path: Path) -> None:
    """The agent-side writer persists result.error as tool/result.data.error.

    Exercised directly; test_tool_result_structured_error_survives_dispatch below
    covers the end-to-end path once the tools pipeline forwards result.error.
    """
    session, _provider, _sink, agent = make_agent(
        tmp_path, FakeRegistry(), [ScriptedTurn(events=[finish("stop")])]
    )
    call_seq = agent._tool_call_log(1, 1, {"callId": "call_1", "name": "failing", "arguments": "{}"})
    agent._append_tool_result(
        1,
        1,
        ToolCallDraft(call_id="call_1", name="failing"),
        ToolResult(
            text="Error: boom",
            is_error=True,
            meta={"cwd": "C:/work"},
            error={"name": "BoomError", "code": "BOOM"},
        ),
        call_seq=call_seq,
    )

    results = session.records_of("tool/result")
    assert len(results) == 1
    data = results[0].data
    assert set(data) == {"turn", "step", "message", "meta", "error"}
    assert data["error"] == {"name": "BoomError", "code": "BOOM"}
    assert data["message"]["isError"] is True
    assert data["message"]["source"] == {"kind": "tool", "callId": "call_1"}
    assert results[0].sourceEventSeqs == [call_seq]
    assert results[0].surfaceOp == "append"


async def test_tool_result_structured_error_survives_dispatch(tmp_path: Path) -> None:
    """A tool-reported structured error reaches the log through real dispatch."""
    tool = StructuredErrorTool()
    registry = FakeRegistry([tool])  # type: ignore[list-item]
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "failing"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[finish("stop")]),
    ]
    session, _provider, _sink, agent = make_agent(tmp_path, registry, script)

    await agent.send("go")

    calls = session.records_of("tool/call")
    results = session.records_of("tool/result")
    assert len(calls) == 1 and len(results) == 1
    data = results[0].data
    assert set(data) == {"turn", "step", "message", "meta", "error"}
    assert data["error"] == {"name": "BoomError", "code": "BOOM"}
    assert data["message"]["isError"] is True
    assert data["meta"] == {"cwd": "C:/work"}
    assert results[0].sourceEventSeqs == [calls[0].seq]


async def test_tool_result_has_no_error_key_without_a_structured_error(tmp_path: Path) -> None:
    echo = FakeTool("echo", text="ok")
    registry = FakeRegistry([echo])
    script = [
        ScriptedTurn(
            events=[ToolCallStart("call_1", "echo"), ToolCallArgsDelta("call_1", "{}"), finish("tool-calls")]
        ),
        ScriptedTurn(events=[finish("stop")]),
    ]
    session, _provider, _sink, agent = make_agent(tmp_path, registry, script)

    await agent.send("go")

    data = session.records_of("tool/result")[0].data
    assert "error" not in data


async def test_interrupt_records_aborted_and_before_dispatch_results(tmp_path: Path) -> None:
    slow = FakeTool("slow", parallel_safe=False, delay=5.0)
    later = FakeTool("later", parallel_safe=False)
    registry = FakeRegistry([slow, later])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_slow", "slow"),
                ToolCallArgsDelta("call_slow", "{}"),
                ToolCallStart("call_later", "later"),
                ToolCallArgsDelta("call_later", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[finish("stop")]),
    ]
    session, _provider, _sink, agent = make_agent(tmp_path, registry, script)

    task = asyncio.create_task(agent.send("go"))
    for _ in range(500):
        if slow.calls:
            break
        await asyncio.sleep(0.01)
    assert slow.calls, "the first tool call never started"
    await agent.interrupt()
    await task

    calls = session.records_of("tool/call")
    results = session.records_of("tool/result")
    # The dispatched call was already logged by the pipeline; the abort
    # synthesizes the call record for the group that never started.
    assert [call.data["name"] for call in calls] == ["slow", "later"]
    assert calls[1].data["callId"] == "call_later"
    assert {result.data["message"]["toolCallId"] for result in results} == {"call_slow", "call_later"}
    by_call = {result.data["message"]["toolCallId"]: result for result in results}

    aborted = by_call["call_slow"]
    assert aborted.data["message"]["content"] == [{"type": "text", "text": "Error: tool call aborted"}]
    assert aborted.data["message"]["isError"] is True
    assert aborted.data["error"] == {"name": "AbortError", "code": "ABORTED"}
    assert aborted.sourceEventSeqs == [calls[0].seq]

    before = by_call["call_later"]
    assert before.data["message"]["content"] == [
        {"type": "text", "text": "Error: tool call aborted before dispatch"}
    ]
    assert before.data["message"]["isError"] is True
    assert before.data["error"] == {"name": "AbortError", "code": "ABORTED_BEFORE_DISPATCH"}
    assert before.sourceEventSeqs == [calls[1].seq]
    assert before.surfaceOp == "append"

    # Every call still has exactly one result, so history remains pairable.
    call_ids = [call.data["callId"] for call in calls]
    result_ids = [result.data["message"]["toolCallId"] for result in results]
    assert sorted(call_ids) == sorted(result_ids)
    texts = [result.data["message"]["content"][0]["text"] for result in results]
    assert not any("interrupted" in text for text in texts)
    assert session.records_of("turn/end")[0].data["reason"] == {
        "kind": "aborted",
        "reason": {"kind": "user"},
    }


async def test_real_session_accepts_abort_records_and_round_trips(tmp_path: Path) -> None:
    """The durable writer accepts the shapes above, including a missing error."""
    store = SessionStore(tmp_path / "sessions")
    session = store.create(tmp_path, session_id="session-fidelity")
    try:
        header = build_header(provider="fake", model="m", max_tokens=1, tools=TOOLS)
        session.append("request/header", {"header": header, "reason": "initial", "startsSeries": True})
        call_seq = session.append(
            "tool/call",
            {"turn": 1, "step": 1, "callId": "call_1", "name": "read", "arguments": "{}"},
        )
        session.append(
            "tool/result",
            {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "tool",
                    "content": [{"type": "text", "text": "Error: tool call aborted"}],
                    "id": "msg_tool",
                    "source": {"kind": "tool", "callId": "call_1"},
                    "toolCallId": "call_1",
                    "isError": True,
                },
                "error": {"name": "AbortError", "code": "ABORTED"},
            },
            surfaceOp="append",
            sourceEventSeqs=[call_seq],
        )
        session.close()
    finally:
        session.close()

    reopened = store.open("session-fidelity")
    try:
        header_record = next(record for record in reopened.records if record.type == "request/header")
        assert set(header_record.data) == {"header", "reason", "startsSeries"}
        result = next(record for record in reopened.records if record.type == "tool/result")
        assert result.data["error"] == {"name": "AbortError", "code": "ABORTED"}
        assert [message.role for message in reopened.derive_messages()] == ["tool"]
    finally:
        reopened.close()
