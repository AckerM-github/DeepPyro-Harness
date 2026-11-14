"""Fork-mode subagent tests: seed boundary, records, activation and settlement."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.subagent import SubagentError
from deeppyro.tools.builtin.subagent import SubagentForkTool
from tests.subagent.helpers import FakeAgents, make_subagent, records
from tests.tools.helpers import make_context


class FakeParentAgent:
    """Parent agent double that records settlement notices."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def followup(self, text: str, *, source: dict[str, Any] | None = None) -> None:
        self.calls.append((text, source))


def _add_turn(session: Any, turn: int) -> None:
    session.append("turn/start", {"turn": turn})
    session.append("turn/end", {"turn": turn})


async def test_fork_seeds_only_completed_turns(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_subagent(tmp_path, agents=FakeAgents(reply="forked reply", gate=gate))
    _add_turn(env.parent, 1)
    env.parent.append("turn/start", {"turn": 2})
    parent_agent = FakeParentAgent()
    env.service.bind_parent(parent_agent)

    activation = await asyncio.wait_for(
        env.service.start("continue the work", description="d", mode="fork"),
        timeout=1,
    )

    assert activation.mode == "fork"
    assert activation.child_id == activation.session_id
    assert activation.child_id.startswith("session-")
    assert activation.label == "subagent-1"
    assert activation.created_at > 0

    child = env.store.open(activation.child_id)
    assert [record.type for record in child.records] == [
        "turn/start",
        "turn/end",
        "session/end-seed",
        "subagent/descriptor",
    ]
    assert [record.data["turn"] for record in child.records[:2]] == [1, 1]
    header = child.header
    assert header.parentSession == env.parent.session_id
    assert header.origin == "subagent"
    assert header.delegationDepth == 1
    assert header.agentPreset == "default"
    assert header.isSeeded is True
    assert header.cwd == str(tmp_path)

    descriptor = records(child, "subagent/descriptor")
    assert descriptor == [
        {
            "version": 1,
            "childId": activation.child_id,
            "mode": "fork",
            "provider": "local",
            "label": "subagent-1",
            "agentProvider": "local",
            "agentModel": "parent-model",
            "inheritedEventCount": 2,
        }
    ]
    catalog = records(env.parent, "subagent/catalog")
    assert catalog == [
        {
            "version": 1,
            "childId": activation.child_id,
            "childCreatedAt": activation.created_at,
            "mode": "fork",
            "label": "subagent-1",
        }
    ]
    assert records(env.parent, "subagent/descriptor") == descriptor

    gate.set()
    await env.service.wait_children()

    assert len(parent_agent.calls) == 1
    text, source = parent_agent.calls[0]
    assert source is not None
    assert source["kind"] == "subagent-settled"
    assert source["form"] == "notice"
    assert source["senderSessionId"] == activation.child_id
    assert source["summary"] == (
        f"Background subagent {activation.child_id} finished and will do no further work "
        "unless you send it more."
    )
    assert text.startswith(source["summary"])
    assert "Its closing message:\nforked reply" in text
    assert env.agents.agents[0].closed is True
    assert env.service.live_runs == 0


async def test_fork_without_completed_turn_uses_empty_seed(tmp_path) -> None:
    env = make_subagent(tmp_path)

    activation = await env.service.start("fresh fork", description="d", mode="fork")
    await env.service.wait_children()

    child = env.store.open(activation.child_id)
    assert [record.type for record in child.records] == ["subagent/descriptor"]
    assert records(child, "subagent/descriptor")[0]["inheritedEventCount"] == 0
    assert child.header.delegationDepth == 1


async def test_fork_rejects_provider_overrides(tmp_path) -> None:
    env = make_subagent(tmp_path)

    with pytest.raises(SubagentError, match="does not support"):
        await env.service.start("p", description="d", mode="fork", provider="other")

    assert env.agents.created == []
    assert records(env.parent, "subagent/catalog") == []
    assert env.service.live_runs == 0


async def test_spawn_activation_uses_child_session_id(tmp_path) -> None:
    env = make_subagent(tmp_path)

    activation = await env.service.start("do it", description="d")
    await env.service.wait_children()

    assert activation.mode == "spawn"
    assert activation.child_id == activation.session_id
    assert env.store.open(activation.child_id).header.parentSession == env.parent.session_id


async def test_wait_children_timeout_does_not_cancel(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_subagent(tmp_path, agents=FakeAgents(gate=gate))
    activation = await env.service.start("p", description="d", mode="fork")

    with pytest.raises(SubagentError, match="timed out waiting for 1 background subagent"):
        await env.service.wait_children(timeout=0)

    assert env.service.live_runs == 1
    assert env.agents.agents[0].closed is False

    gate.set()
    await env.service.wait_children()
    assert env.service.live_runs == 0
    assert env.store.open(activation.child_id).header.parentSession == env.parent.session_id


async def test_close_cancels_background_fork(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_subagent(tmp_path, agents=FakeAgents(gate=gate))
    activation = await env.service.start("p", description="d", mode="fork")

    await env.service.close()

    assert env.service.live_runs == 0
    assert env.agents.agents[0].closed is True
    with pytest.raises(SubagentError, match="closed"):
        await env.service.start("again", description="d", mode="fork")
    assert env.store.open(activation.child_id)


async def test_fork_tool_returns_activation_immediately(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_subagent(tmp_path, agents=FakeAgents(reply="done", gate=gate))
    _add_turn(env.parent, 1)
    env.service.bind_parent(FakeParentAgent())
    tool = SubagentForkTool()
    ctx = make_context(tmp_path, services={"subagent": env.service})

    result = await asyncio.wait_for(
        tool.run({"prompt": "p", "description": "d"}, ctx),
        timeout=1,
    )

    assert result.is_error is False
    payload = json.loads(result.text)
    assert payload["kind"] == "activation"
    child_id = payload["subagentId"]
    assert child_id.startswith("session-")
    assert result.meta == {
        "childSessionId": child_id,
        "label": "subagent-1",
        "mode": "fork",
    }
    assert env.service.live_runs == 1
    assert env.agents.agents[0].closed is False

    gate.set()
    await env.service.wait_children()
    assert env.service.live_runs == 0


async def test_fork_tool_reports_start_failures_as_tool_errors(tmp_path) -> None:
    class BrokenService:
        async def start(self, *args: Any, **kwargs: Any) -> Any:
            raise SubagentError("boom")

    tool = SubagentForkTool()
    ctx = make_context(tmp_path, services={"subagent": BrokenService()})

    result = await tool.run({"prompt": "p", "description": "d"}, ctx)

    assert result.is_error is True
    assert result.text == "subagent fork failed: boom"


async def test_fork_tool_rejects_empty_prompt(tmp_path) -> None:
    env = make_subagent(tmp_path)
    tool = SubagentForkTool()
    ctx = make_context(tmp_path, services={"subagent": env.service})

    with pytest.raises(ToolError, match="prompt must be a non-empty string"):
        await tool.run({"prompt": "", "description": "d"}, ctx)


async def test_fork_tool_schema_and_missing_service(tmp_path) -> None:
    tool = SubagentForkTool()

    assert tool.name == "subagent_fork"
    assert tool.parallel_safe is False
    assert tool.parameters["required"] == ["prompt", "description"]
    assert set(tool.parameters["properties"]) == {"prompt", "description", "name"}

    ctx = make_context(tmp_path)
    with pytest.raises(ToolError, match="subagent service is not available"):
        await tool.run({"prompt": "p", "description": "d"}, ctx)


async def test_failed_fork_sends_failure_notice(tmp_path) -> None:
    env = make_subagent(tmp_path, agents=FakeAgents(error=RuntimeError("boom")))
    _add_turn(env.parent, 1)
    parent_agent = FakeParentAgent()
    env.service.bind_parent(parent_agent)

    await env.service.start("p", description="d", mode="fork")
    await env.service.wait_children()

    assert len(parent_agent.calls) == 1
    text, source = parent_agent.calls[0]
    assert source is not None
    assert source["summary"] == (
        f"Background subagent {source['senderSessionId']} failed before it finished."
    )
    assert text == source["summary"] + "\nIt left no closing message."
    assert source["kind"] == "subagent-settled"
    assert source["form"] == "notice"


async def test_fork_notice_is_dropped_without_parent_agent(tmp_path, caplog) -> None:
    env = make_subagent(tmp_path)
    _add_turn(env.parent, 1)

    with caplog.at_level(logging.WARNING, logger="deeppyro.subagent.manager"):
        await env.service.start("p", description="d", mode="fork")
        await env.service.wait_children()

    assert "dropping settlement notice" in caplog.text
    assert env.service.live_runs == 0
