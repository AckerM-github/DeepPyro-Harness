"""Tests for SubagentService: child sessions, records, tool filtering and runs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from deeppyro.api.types import CHILD_DISABLED_TOOLS
from deeppyro.subagent import SubagentError, SubagentResult, SubagentService
from tests.subagent.helpers import (
    FakeAgentNoClose,
    FakeAgents,
    FakeGate,
    FakeSandboxNoAsk,
    FakeWithoutRegistry,
    make_registry,
    make_subagent,
    records,
    wait_for,
)


async def test_run_returns_reply_and_creates_child_session(tmp_path) -> None:
    env = make_subagent(tmp_path, agents=FakeAgents(reply="found the bug"))

    result = await env.service.run("find the bug", description="research the bug")

    assert isinstance(result, SubagentResult)
    assert result.text == "found the bug"
    assert result.is_error is False
    assert result.error == ""
    assert result.label == "subagent-1"
    assert result.child_id.startswith("subagent-")
    assert len(result.child_id) == len("subagent-") + 12
    assert result.provider == "local"
    assert result.model == "parent-model"
    assert result.session_id

    child = env.store.open(result.session_id)
    header = child.header
    assert header.parentSession == env.parent.session_id
    assert header.delegationDepth == 1
    assert header.isSeeded is True
    assert header.origin == "subagent"
    assert header.agentPreset == "default"
    assert header.cwd == str(tmp_path)
    assert header.createdAt > 0

    agent = env.agents.agents[0]
    assert agent.prompts == ["find the bug"]
    assert agent.closed is True
    assert agent.close_calls == 1
    assert env.service.live_runs == 0


async def test_child_agent_wiring(tmp_path) -> None:
    env = make_subagent(tmp_path)

    result = await env.service.run("do it", description="d")

    created = env.agents.created[0]
    assert created["session"].session_id == result.session_id
    assert created["provider"] is env.provider
    assert created["registry"].names() == ["read", "write"]
    assert created["options"].model == "parent-model"
    assert created["options"].context_window == 8192
    assert created["cwd"] == str(tmp_path)
    assert created["services"] == {"llm": env.llm}
    assert created["preset"] == "default"
    assert created["preset_revision"] == 7

    gate = created["permission"]
    assert isinstance(gate, FakeGate)
    assert gate is not env.permission
    assert gate.preset == "workspace-write"
    assert gate.approval == "ask"
    assert env.sandbox.calls == [{"workspace": tmp_path, "ask": "ask-callback"}]

    sink = created["sink"]
    assert sink.emit({"type": "text-delta"}) is None


async def test_parent_gets_catalog_and_descriptor_records(tmp_path) -> None:
    env = make_subagent(tmp_path, agents=FakeAgents(reply="done"))

    result = await env.service.run("do it", description="d")
    child = env.store.open(result.session_id)

    catalog = records(env.parent, "subagent/catalog")
    assert catalog == [
        {
            "version": 1,
            "childId": result.child_id,
            "childCreatedAt": child.header.createdAt,
            "mode": "spawn",
            "label": "subagent-1",
        }
    ]
    descriptor = records(env.parent, "subagent/descriptor")
    assert descriptor == [
        {
            "version": 1,
            "childId": result.child_id,
            "mode": "spawn",
            "provider": "local",
            "label": "subagent-1",
            "agentProvider": "local",
            "agentModel": "parent-model",
        }
    ]


async def test_child_registry_prefers_without(tmp_path) -> None:
    registry = FakeWithoutRegistry(("read", "write", "subagent", "team_spawn"))
    env = make_subagent(tmp_path, registry=registry)

    result = await env.service.run("do it", description="d")

    assert registry.calls == [tuple(CHILD_DISABLED_TOOLS)]
    child_registry = env.agents.created[0]["registry"]
    assert child_registry.names() == ["read", "write"]
    assert "subagent" not in child_registry
    assert not any(name.startswith("team_") for name in child_registry.names())
    assert result.text == "ok"


async def test_labels_are_generated_or_taken_from_name(tmp_path) -> None:
    env = make_subagent(tmp_path)

    first = await env.service.run("one", description="d")
    second = await env.service.run("two", description="d")
    named = await env.service.run("three", description="d", name="scout")
    third = await env.service.run("four", description="d")

    assert first.label == "subagent-1"
    assert second.label == "subagent-2"
    assert named.label == "scout"
    assert third.label == "subagent-3"
    ids = [first.child_id, second.child_id, named.child_id, third.child_id]
    assert len(set(ids)) == 4


async def test_provider_and_model_overrides(tmp_path) -> None:
    env = make_subagent(tmp_path)

    result = await env.service.run(
        "do it",
        description="d",
        provider="other",
        model="child-model",
    )

    assert env.llm.calls == ["other"]
    created = env.agents.created[0]
    assert created["provider"] is env.llm.providers["other"]
    assert created["options"].model == "child-model"
    assert result.provider == "other"
    assert result.model == "child-model"
    descriptor = records(env.parent, "subagent/descriptor")[0]
    assert descriptor["agentProvider"] == "other"
    assert descriptor["agentModel"] == "child-model"


async def test_provider_object_is_used_directly(tmp_path) -> None:
    env = make_subagent(tmp_path)
    direct = SimpleNamespace(name="direct")

    result = await env.service.run("do it", description="d", provider=direct)

    assert env.agents.created[0]["provider"] is direct
    assert result.provider == "direct"
    assert env.llm.calls == []


async def test_unknown_provider_raises_before_creating_child(tmp_path) -> None:
    env = make_subagent(tmp_path)

    with pytest.raises(SubagentError, match="unknown llm provider 'missing'"):
        await env.service.run("do it", description="d", provider="missing")

    assert env.agents.created == []
    assert records(env.parent, "subagent/catalog") == []
    assert records(env.parent, "subagent/descriptor") == []
    assert env.service.live_runs == 0


async def test_preset_override_drops_revision(tmp_path) -> None:
    env = make_subagent(tmp_path)

    await env.service.run("do it", description="d", preset="other")

    created = env.agents.created[0]
    assert created["preset"] == "other"
    assert created["preset_revision"] is None


async def test_default_preset_keeps_revision(tmp_path) -> None:
    env = make_subagent(tmp_path)

    await env.service.run("do it", description="d")

    assert env.agents.created[0]["preset"] == "default"
    assert env.agents.created[0]["preset_revision"] == 7


async def test_child_failure_returns_error_result(tmp_path) -> None:
    env = make_subagent(tmp_path, agents=FakeAgents(error=RuntimeError("boom")))

    result = await env.service.run("do it", description="d")

    assert result.is_error is True
    assert result.error == "RuntimeError: boom"
    assert result.text == ""
    assert result.label == "subagent-1"
    assert env.agents.agents[0].closed is True
    assert env.service.live_runs == 0
    assert len(records(env.parent, "subagent/catalog")) == 1
    assert len(records(env.parent, "subagent/descriptor")) == 1


async def test_agent_without_aclose_is_accepted(tmp_path) -> None:
    agents = FakeAgents(agent_factory=lambda: FakeAgentNoClose("ok"))
    env = make_subagent(tmp_path, agents=agents)

    result = await env.service.run("do it", description="d")

    assert result.text == "ok"
    assert env.service.live_runs == 0


async def test_sandbox_without_ask_keyword_falls_back(tmp_path) -> None:
    sandbox = FakeSandboxNoAsk()
    env = make_subagent(tmp_path, sandbox=sandbox)

    await env.service.run("do it", description="d")

    assert sandbox.calls == [{"workspace": tmp_path, "ask": None}]
    gate = env.agents.created[0]["permission"]
    assert gate.preset == "workspace-write"
    assert gate.approval == "ask"


async def test_max_live_runs(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_subagent(tmp_path, agents=FakeAgents(reply="done", gate=gate), max_runs=2)

    tasks = [asyncio.create_task(env.service.run(f"task {index}", description="d")) for index in range(2)]
    await wait_for(lambda: len(env.agents.agents) == 2)
    assert env.service.live_runs == 2

    with pytest.raises(SubagentError, match="at most 2 concurrent subagent runs"):
        await env.service.run("extra", description="d")

    gate.set()
    results = await asyncio.gather(*tasks)
    assert [result.text for result in results] == ["done", "done"]
    assert env.service.live_runs == 0


async def test_close_cancels_in_flight_run(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_subagent(tmp_path, agents=FakeAgents(gate=gate))

    task = asyncio.create_task(env.service.run("do it", description="d"))
    await wait_for(lambda: bool(env.agents.agents))
    assert env.service.live_runs == 1

    await env.service.close()

    results = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert env.service.live_runs == 0
    with pytest.raises(SubagentError, match="closed"):
        await env.service.run("again", description="d")


async def test_cancel_all_returns_cancelled_count(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_subagent(tmp_path, agents=FakeAgents(gate=gate))

    tasks = [asyncio.create_task(env.service.run(f"t{index}", description="d")) for index in range(2)]
    await wait_for(lambda: len(env.agents.agents) == 2)

    cancelled = await env.service.cancel_all()

    assert cancelled == 2
    await asyncio.gather(*tasks, return_exceptions=True)
    assert env.service.live_runs == 0
    gate.set()
    result = await env.service.run("later", description="d")
    assert result.text == "ok"


async def test_run_requires_configuration(tmp_path) -> None:
    env = make_subagent(tmp_path, configure=False)

    with pytest.raises(SubagentError, match="not configured"):
        await env.service.run("do it", description="d")


async def test_run_requires_bound_parent_session(tmp_path) -> None:
    env = make_subagent(tmp_path)
    env.service.bind_session(None)

    with pytest.raises(SubagentError, match="no bound parent session"):
        await env.service.run("do it", description="d")


async def test_run_requires_tool_registry(tmp_path) -> None:
    env = make_subagent(tmp_path)
    env.service.configure(
        cwd=tmp_path,
        provider=env.provider,
        registry=None,
        permission=env.permission,
        model="m",
        context_window=8192,
    )

    with pytest.raises(SubagentError, match="requires a tool registry"):
        await env.service.run("do it", description="d")


async def test_run_requires_non_empty_prompt(tmp_path) -> None:
    env = make_subagent(tmp_path)

    with pytest.raises(SubagentError, match="prompt must be a non-empty string"):
        await env.service.run("   ", description="d")


async def test_prompt_is_trimmed(tmp_path) -> None:
    env = make_subagent(tmp_path)

    await env.service.run("  find it  ", description="d")

    assert env.agents.agents[0].prompts == ["find it"]


def test_service_defaults() -> None:
    service = SubagentService()

    assert service.max_runs == 4
    assert service.live_runs == 0
    assert service.parent_session_id == ""


def test_filter_child_tools_fallback_removes_orchestration_tools() -> None:
    from deeppyro.subagent import filter_child_tools

    filtered = filter_child_tools(make_registry())

    assert filtered.names() == ["read", "write"]
    assert "subagent" not in filtered
    assert not any(name.startswith("team_") for name in filtered.names())
