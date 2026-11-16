"""Behaviour snapshot pinned before splitting AgentSession into layers.

The split of `deeppyro.agent.session` is meant to be a pure move: method
bodies, record order, live events and log text must not change.  These tests
describe the observable session-loop contract at that boundary -- a complete
turn's durable records, the interrupt path, and the open-turn helper filters --
so the structural refactor can prove it changed nothing.

They are written against the public AgentSession API and existing fakes only
(no network, no new fixtures).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.protocols import (
    Finish,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    ToolContext,
    ToolResult,
)
from deeppyro.hooks import HookService
from deeppyro.hooks.types import AGENT_STATUS

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


def make_agent(
    tmp_path: Path,
    script: list[ScriptedTurn],
    *,
    registry: FakeRegistry | None = None,
    services: dict[str, Any] | None = None,
) -> tuple[FakeSessionLog, ScriptedProvider, RecordingSink, AgentSession]:
    """Build a loop over in-memory fakes (same wiring as the other agent tests)."""
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=registry if registry is not None else FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        services=services,
    )
    return session, provider, sink, agent


async def _wait_for(predicate: Any, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached before timeout")


async def test_complete_tool_turn_record_sequence_and_final_text(tmp_path: Path) -> None:
    """Pin the exact durable record order and final text of one complete turn."""
    echo = FakeTool("echo", text="echo:{text}")
    registry = FakeRegistry([echo])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", '{"text": "a"}'),
                ToolCallStart("call_2", "echo"),
                ToolCallArgsDelta("call_2", '{"text": "b"}'),
                usage(),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, _provider, _sink, agent = make_agent(tmp_path, script, registry=registry)

    assert await agent.send("use tools") == "done"

    assert session.types() == [
        "agent/inbox/spliced",
        "turn/start",
        "agent/inbox/spliced",
        "step/start",
        "system/message",
        "user/message",
        "user/message",
        "request/header",
        "request/context",
        "assistant/message",
        "tool/call",
        "tool/call",
        "tool/result",
        "tool/result",
        "step/end",
        "step/start",
        "assistant/message",
        "step/end",
        "turn/end",
    ]
    assert session.records_of("turn/end")[0].data == {"turn": 1, "reason": {"kind": "completed"}}
    calls = session.records_of("tool/call")
    assert [call.data["callId"] for call in calls] == ["call_1", "call_2"]
    results = session.records_of("tool/result")
    assert [result.data["message"]["content"][0]["text"] for result in results] == [
        "echo:a",
        "echo:b",
    ]
    # Final text comes from the step that ended the turn, and the derived
    # history keeps both tool results in order.
    assert session.derive_messages()[-1].text() == "done"
    assert [message.role for message in session.derive_messages()] == [
        "system",
        "user",
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert agent.busy is False


async def test_interrupt_path_records_and_phase_changes(tmp_path: Path) -> None:
    """Pin the user-abort path: partial assistant record, records, phases."""
    hooks = HookService()
    phases: list[Any] = []
    hooks.on(AGENT_STATUS, phases.append)
    pause = asyncio.Event()
    script = [
        ScriptedTurn(
            events=[TextDelta("par"), Finish(kind="stop")],
            pause=pause,
            pause_after=1,
        )
    ]
    session, provider, sink, agent = make_agent(tmp_path, script, services={"hooks": hooks})

    task = asyncio.create_task(agent.send("long"))
    await provider.started.wait()
    await _wait_for(lambda: bool(sink.of_type("text")))
    await agent.interrupt()

    assert await task == "par"
    assert session.types() == [
        "agent/inbox/spliced",
        "turn/start",
        "agent/inbox/spliced",
        "step/start",
        "system/message",
        "user/message",
        "user/message",
        "request/header",
        "request/context",
        "assistant/message",
        "step/end",
        "turn/end",
    ]
    assistant = session.records_of("assistant/message")[0].data
    assert assistant["interrupted"] is True
    assert assistant["message"]["content"] == [{"type": "text", "text": "par"}]
    assert session.records_of("turn/end")[0].data == {
        "turn": 1,
        "reason": {"kind": "aborted", "reason": {"kind": "user"}},
    }
    # The status hook sees the driver phases; the live sink sees the step
    # statuses but no final event for an aborted turn.
    assert [payload["status"] for payload in phases] == ["running", "idle"]
    assert [event["phase"] for event in sink.of_type("status")] == ["thinking", "done"]
    assert sink.of_type("final") == []
    assert agent.busy is False


class _CapturingTool(FakeTool):
    """Echo tool that snapshots the open-turn helpers while it runs."""

    def __init__(self, captures: list[dict[str, Any]]) -> None:
        super().__init__("echo", text="echo:{text}")
        self.captures = captures

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent = ctx.agent
        assert isinstance(agent, AgentSession)
        # A hook journal record lands inside the open turn and must not make
        # the turn look like human input.
        agent.record_hook_event("hook/invoked", {"hook": "agent/pre-step", "mode": "serial"})
        self.captures.append(
            {
                "types": [record.type for record in agent.current_turn_records()],
                "human": agent.current_turn_human_input(),
                "goal_round": agent.current_turn_goal_round(),
            }
        )
        return await super().run(args, ctx)


async def test_current_turn_helpers_filter_hook_and_goal_records(tmp_path: Path) -> None:
    """current_turn_* reads the open turn and only counts user-sourced messages."""
    captures: list[dict[str, Any]] = []
    echo = _CapturingTool(captures)
    registry = FakeRegistry([echo])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", '{"text": "a"}'),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
        ScriptedTurn(
            events=[
                ToolCallStart("call_2", "echo"),
                ToolCallArgsDelta("call_2", '{"text": "b"}'),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("goal done"), finish("stop")]),
    ]
    session, _provider, _sink, agent = make_agent(tmp_path, script, registry=registry)

    assert await agent.send("use tools") == "done"

    human_turn = captures[0]
    assert human_turn["human"] is True
    assert human_turn["goal_round"] is None
    assert human_turn["types"][0] == "turn/start"
    assert human_turn["types"][-1] == "hook/invoked"
    assert "tool/call" in human_turn["types"]
    assert "assistant/message" in human_turn["types"]
    # After the turn closed the helper still reports the last turn's records.
    finished_turn = [record.type for record in agent.current_turn_records()]
    assert finished_turn[0] == "turn/start"
    assert finished_turn[-1] == "turn/end"
    assert "hook/invoked" in finished_turn
    assert "tool/result" in finished_turn
    assert agent.current_turn_human_input() is True
    hook_records = session.records_of("hook/invoked")
    assert len(hook_records) == 1
    assert hook_records[0].data == {"hook": "agent/pre-step", "mode": "serial"}
    assert hook_records[0].surfaceOp is None

    # A goal-admitted turn carries no human input even with tool and hook
    # records in the turn.
    agent.followup(
        "goal work",
        source={"kind": "goal", "goalId": "g1", "revision": 2, "round": 3},
    )
    # Waking the driver must close the idle gate: a stale event from the
    # previous drive would make wait_for_idle spin without yielding to the
    # driver task, so assert the gate is shut before waiting on it.
    assert agent._idle.is_set() is False
    await agent.wait_for_idle()

    goal_turn = captures[1]
    assert goal_turn["human"] is False
    assert goal_turn["goal_round"] == {"goalId": "g1", "revision": 2, "round": 3}
    assert goal_turn["types"][-1] == "hook/invoked"
    assert "tool/call" in goal_turn["types"]
    current = agent.current_turn_records()
    turn_starts = [record.data["turn"] for record in current if record.type == "turn/start"]
    assert turn_starts == [2]
    assert agent.current_turn_human_input() is False
    assert agent.current_turn_goal_round() == {"goalId": "g1", "revision": 2, "round": 3}
    # The normal message content helpers are not disturbed by the filter.
    assert session.types().count("turn/start") == 2
