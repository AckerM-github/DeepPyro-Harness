"""A tool may end its turn instead of asking the model for another step."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.protocols import TextDelta, ToolCallStart, ToolResult

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


class ConcludingTool(FakeTool):
    """A tool that asks the loop to close the turn after it returns."""

    def __init__(self, name: str = "conclude", reason: dict[str, Any] | None = None) -> None:
        super().__init__(name)
        self.reason = reason or {"kind": "completed"}

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        assert ctx.conclude_turn is not None
        ctx.conclude_turn(self.reason)
        return ToolResult(text="closed")


def _agent(tmp_path: Path, tool: Any, script: list[ScriptedTurn]) -> tuple[Any, AgentSession]:
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry([tool]),
        options=AgentOptions(model="test-model"),
        sink=RecordingSink(session),
        permission=FakePermission(),
        cwd=tmp_path,
    )
    return provider, agent


def _call_turn(name: str) -> ScriptedTurn:
    return ScriptedTurn(events=[ToolCallStart(call_id="call-1", name=name), finish("tool-calls")])


async def test_a_conclusion_ends_the_turn_without_another_request(tmp_path: Path) -> None:
    tool = ConcludingTool()
    provider, agent = _agent(
        tmp_path,
        tool,
        [_call_turn("conclude"), ScriptedTurn(events=[TextDelta("never"), finish("stop")])],
    )

    await agent.send("go")

    assert len(provider.requests) == 1, "the loop asked the model again after a conclusion"
    ends = [record.data for record in agent._session.records if record.type == "turn/end"]
    assert ends[-1]["reason"] == {"kind": "completed"}


async def test_without_a_conclusion_the_loop_keeps_going(tmp_path: Path) -> None:
    provider, agent = _agent(
        tmp_path,
        FakeTool("echo"),
        [_call_turn("echo"), ScriptedTurn(events=[TextDelta("done"), finish("stop")])],
    )

    assert await agent.send("go") == "done"
    assert len(provider.requests) == 2


async def test_only_the_first_conclusion_wins(tmp_path: Path) -> None:
    tool = ConcludingTool()
    provider, agent = _agent(
        tmp_path,
        tool,
        [_call_turn("conclude"), ScriptedTurn(events=[TextDelta("never"), finish("stop")])],
    )

    await agent.send("go")

    assert len(provider.requests) == 1
    conclude = agent._request_turn_conclusion
    agent._turn_conclusion = {"kind": "completed"}
    conclude({"kind": "blocked"})
    assert agent._turn_conclusion == {"kind": "completed"}
