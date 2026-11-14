"""Integration checks for the P0 bus alignment (header/context fidelity, tool payload).

These tests drive the real AgentSession over the in-memory fakes and assert the
wire shapes P0 pinned against the reference agent loop: request records carry no
turn/step position, and tool hooks receive the executing agent plus the visible
tool set.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.protocols import Finish, TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.hooks import HookService
from deeppyro.hooks.types import TOOLS_EXECUTE, TOOLS_POST_EXECUTE, TOOLS_PRE_EXECUTE
from tests.agent.fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    FakeTool,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)


def make_agent(
    tmp_path: Path,
    script: list[ScriptedTurn],
    *,
    registry: FakeRegistry | None = None,
    services: dict[str, Any] | None = None,
) -> tuple[FakeSessionLog, AgentSession]:
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
    return session, agent


async def test_request_records_have_no_position_and_series_semantics(tmp_path: Path) -> None:
    """request/header and request/context are wire-shaped; startsSeries only on a series start."""
    script = [
        ScriptedTurn(events=[TextDelta("one"), finish("stop")]),
        ScriptedTurn(events=[TextDelta("two"), finish("stop")]),
    ]
    session, agent = make_agent(tmp_path, script)

    assert await agent.send("first") == "one"
    assert await agent.send("second") == "two"

    headers = session.records_of("request/header")
    contexts = session.records_of("request/context")
    assert len(headers) == 2
    first, second = (record.data for record in headers)
    assert set(first) == {"header", "reason", "startsSeries"}
    assert first["reason"] == "initial"
    assert first["startsSeries"] is True
    assert "turn" not in first and "step" not in first
    assert set(second) == {"header", "reason"}
    assert second["reason"] == "series"
    assert "startsSeries" not in second
    for record in contexts:
        assert set(record.data) <= {
            "provider",
            "model",
            "contextWindow",
            "systemPromptUpdate",
        }
        assert record.data["provider"] and record.data["model"]
        assert "turn" not in record.data and "step" not in record.data


async def test_tool_hooks_receive_the_agent_and_the_visible_tool_set(tmp_path: Path) -> None:
    """Every tool hook payload carries agent and toolSet from the batch caller."""
    hooks = HookService()
    payloads: list[dict[str, Any]] = []

    async def observe(payload: dict[str, Any], next_step: Any) -> Any:
        payloads.append(payload)
        return await next_step()

    for name in (TOOLS_PRE_EXECUTE, TOOLS_EXECUTE):
        hooks.on(name, observe)

    async def accept(payload: dict[str, Any], next_step: Any) -> Any:
        payloads.append(payload)
        return await next_step()

    hooks.on(TOOLS_POST_EXECUTE, accept)

    registry = FakeRegistry([FakeTool("read"), FakeTool("echo")])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", "{}"),
                Finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, agent = make_agent(tmp_path, script, registry=registry, services={"hooks": hooks})

    assert await agent.send("go") == "done"

    assert [record.type for record in session.records_of("tool/result")] == ["tool/result"]
    assert payloads, "tool hooks were never dispatched"
    for payload in payloads:
        assert payload["agent"] is agent
        assert payload["toolSet"] == ["echo", "read"]


async def test_tool_hook_agent_is_none_for_a_bare_pipeline_caller() -> None:
    """A direct run_tool_batch caller keeps the keys but leaves agent unset."""
    from deeppyro.core.protocols import ToolContext
    from deeppyro.tools.pipeline import run_tool_batch

    seen: list[dict[str, Any]] = []
    hooks = HookService()

    async def observe(payload: dict[str, Any], next_step: Any) -> Any:
        seen.append(payload)
        return await next_step()

    hooks.on(TOOLS_PRE_EXECUTE, observe)
    registry = FakeRegistry([FakeTool("echo")])

    class Request:
        call_id = "call_1"
        name = "echo"
        arguments = "{}"

    def factory(_request: Any) -> ToolContext:
        return ToolContext(
            cwd=Path("."),
            session_id="s",
            turn=1,
            step=1,
            call_id="call_1",
            cancel=asyncio.Event(),
            log=lambda *args, **kwargs: 1,
        )

    await run_tool_batch(
        [Request()],
        registry=registry,
        ctx_factory=factory,
        hooks=hooks,
    )
    assert seen[0]["agent"] is None
    assert "toolSet" not in seen[0]
