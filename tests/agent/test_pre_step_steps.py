"""``agent/pre-step`` is per step, exactly like DSH's loop admission.

DSH dispatches ``agent/pre-step`` before every proposed step and hands the
listener only the messages that step claimed -- not the whole history. A
listener may replace them or reject the step, and a rejection leaves no
``step/start`` behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.core.protocols import TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.hooks import HookService
from deeppyro.hooks.types import AGENT_PRE_STEP

from .fakes import FakeRegistry, FakeTool, ScriptedTurn, finish
from .test_agent_p0 import make_agent


def tool_turn() -> ScriptedTurn:
    """One step that asks for the echo tool."""
    return ScriptedTurn(
        events=[ToolCallStart("call_1", "echo"), ToolCallArgsDelta("call_1", "{}"), finish("tool-calls")]
    )


async def test_pre_step_fires_once_per_step_with_only_the_claimed_messages(tmp_path: Path) -> None:
    hooks = HookService()
    seen: list[tuple[int, int, list[str]]] = []

    async def record(payload: dict[str, Any], nxt: Any) -> Any:
        seen.append((payload["turn"], payload["step"], [message.text() for message in payload["messages"]]))
        return await nxt()

    hooks.on(AGENT_PRE_STEP, record)
    registry = FakeRegistry([FakeTool("echo", text="ok")])
    script = [tool_turn(), ScriptedTurn(events=[TextDelta("done"), finish("stop")])]

    _session, _provider, _sink, agent = make_agent(
        tmp_path, script, registry=registry, services={"hooks": hooks}
    )

    assert await agent.send("go") == "done"

    assert seen == [(1, 1, ["go"]), (1, 2, [])]


async def test_a_rejection_on_a_later_step_stops_the_turn(tmp_path: Path) -> None:
    hooks = HookService()

    def reject_second(payload: dict[str, Any], nxt: Any) -> Any:
        if payload["step"] == 2:
            return {"kind": "reject", "reason": "no second step today"}
        return nxt()

    hooks.on(AGENT_PRE_STEP, reject_second)
    registry = FakeRegistry([FakeTool("echo", text="ok")])
    script = [tool_turn(), ScriptedTurn(events=[TextDelta("never"), finish("stop")])]

    session, provider, _sink, agent = make_agent(
        tmp_path, script, registry=registry, services={"hooks": hooks}
    )

    assert await agent.send("go") == ""

    assert len(session.records_of("step/start")) == 1
    assert provider.calls == 1
    reason = session.records_of("turn/end")[0].data["reason"]
    assert reason == {
        "kind": "blocked",
        "reason": {"kind": "pre-step", "reason": "no second step today"},
    }
