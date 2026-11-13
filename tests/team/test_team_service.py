"""Tests for TeamService: child sessions, messaging and lifecycle phases."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.api import CHILD_DISABLED_TOOLS, SUBAGENT_TOOL_NAMES, TEAM_TOOL_NAMES
from deeppyro.team import (
    LIVE_PHASES,
    MAX_MEMBERS,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_IDLE,
    PHASE_RUNNING,
    PHASE_SPAWNING,
    MemberInfo,
    TeamError,
    TeamService,
)
from deeppyro.tools.registry import ToolRegistry
from tests.team.helpers import (
    FakeAgents,
    FakeGate,
    FakeSandbox,
    make_team,
    records,
    spin,
)


class FakeLlm:
    """LLM service double used to resolve named providers."""

    def provider(self, name: str) -> Any:
        if name != "remote":
            raise LookupError(f"unknown provider {name}")
        return SimpleNamespace(name=name)


async def test_spawn_creates_child_session_agent_and_records(tmp_path) -> None:
    env = make_team(tmp_path)
    info = env.service.spawn("scout", "researches the bug", "find the bug")
    assert isinstance(info, MemberInfo)
    assert info.id.startswith("member-")
    assert info.name == "scout"
    assert info.description == "researches the bug"
    assert info.provider == "local"
    assert info.model == "parent-model"
    assert info.preset == "default"
    assert info.phase in (PHASE_SPAWNING, PHASE_RUNNING)

    child = env.store.open(info.session_id)
    header = child.header
    assert header.parentSession == env.parent.session_id
    assert header.delegationDepth == 1
    assert header.isSeeded is True
    assert header.origin == "subagent"
    assert header.agentPreset == "default"
    assert header.cwd == str(tmp_path)

    created = env.agents.created[0]
    assert created["session"].session_id == info.session_id
    assert created["provider"] is env.provider
    assert created["registry"] == "registry"
    assert created["options"].model == "parent-model"
    assert created["options"].context_window == 8192
    assert created["cwd"] == str(tmp_path)
    assert created["preset"] == "default"
    assert created["preset_revision"] is None
    gate = created["permission"]
    assert isinstance(gate, FakeGate)
    assert gate.preset == "danger-full-access"
    assert gate.approval == "always"
    assert env.sandbox.calls[0]["workspace"] == tmp_path
    assert env.sandbox.calls[0]["ask"] == "ask-callback"

    final = await env.service.wait(info.id)
    assert final.phase == PHASE_COMPLETED
    assert final.last_reply == "ok"
    assert env.agents.agents[0].prompts == ["find the bug"]

    catalog = records(env.parent, "subagent/catalog")
    assert len(catalog) == 1
    assert catalog[0] == {
        "version": 1,
        "childId": info.id,
        "childCreatedAt": info.created_at,
        "mode": "spawn",
        "label": "scout",
    }
    descriptor = records(env.parent, "subagent/descriptor")[0]
    assert descriptor["version"] == 1
    assert descriptor["childId"] == info.id
    assert descriptor["mode"] == "spawn"
    assert descriptor["label"] == "scout"
    assert descriptor["provider"] == "local"
    assert descriptor["agentProvider"] == "local"
    assert descriptor["agentModel"] == "parent-model"

    member_records = records(env.parent, "team/member")
    payloads = [item["member"] for item in member_records]
    assert payloads[0]["phase"] == PHASE_SPAWNING
    assert PHASE_RUNNING in [item["phase"] for item in payloads]
    assert payloads[-1]["phase"] == PHASE_COMPLETED
    assert set(payloads[-1]) == {
        "id",
        "name",
        "description",
        "provider",
        "context",
        "phase",
        "model",
    }
    assert payloads[-1]["context"] == "default"
    assert member_records[-1]["version"] == 1
    assert member_records[-1]["teamId"] == env.parent.session_id
    await env.service.close()


async def test_member_agent_registry_excludes_nested_delegation_tools(tmp_path) -> None:
    names = [*TEAM_TOOL_NAMES, *SUBAGENT_TOOL_NAMES, "read"]
    registry = ToolRegistry([SimpleNamespace(name=name) for name in names])
    env = make_team(tmp_path, registry=registry)
    info = env.service.spawn("scout", "researches the bug", "find the bug")
    child_registry = env.agents.created[0]["registry"]
    assert set(child_registry.names()) == {"read"}
    assert not set(CHILD_DISABLED_TOOLS) & set(child_registry.names())
    # The caller's registry is untouched: only the member's view is narrowed.
    assert registry.names() == names
    await env.service.wait(info.id)
    await env.service.close()


async def test_spawn_requires_bound_session_services_and_configuration(tmp_path) -> None:
    unbound = TeamService()
    with pytest.raises(TeamError, match="no bound parent session"):
        unbound.spawn("scout", "d", "p")

    with pytest.raises(TeamError, match="name must be a non-empty string"):
        unbound.spawn("", "d", "p")

    unbound.bind_session(SimpleNamespace(session_id="session-parent"))
    with pytest.raises(TeamError, match="requires the agents and sessions services"):
        unbound.spawn("scout", "d", "p")

    store_holder = SimpleNamespace(store=SimpleNamespace(root=tmp_path / "sessions"))
    partial = TeamService(agents=FakeAgents(), sessions=store_holder, sandbox=FakeSandbox())
    partial.bind_session(SimpleNamespace(session_id="session-parent"))
    with pytest.raises(TeamError, match="not configured"):
        partial.spawn("scout", "d", "p")

    assert make_team(tmp_path).service is not None


async def test_spawn_enforces_live_limit_and_unique_names(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_team(tmp_path, agents=FakeAgents(gate=gate))
    first = env.service.spawn("m0", "d", "p")
    with pytest.raises(TeamError, match="already in use"):
        env.service.spawn("m0", "d", "p")
    for index in range(1, MAX_MEMBERS):
        env.service.spawn(f"m{index}", "d", "p")
    with pytest.raises(TeamError, match="team is full"):
        env.service.spawn("extra", "d", "p")
    await spin()
    assert all(info.phase in LIVE_PHASES for info in env.service.members())

    assert await env.service.cancel(first.id) is True
    replacement = env.service.spawn("extra", "d", "p")
    assert replacement.name == "extra"
    await env.service.close()


async def test_send_queues_while_busy_then_delivers_after_release(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_team(tmp_path, agents=FakeAgents(gate=gate))
    info = env.service.spawn("worker", "writes code", "initial task")
    await spin()
    assert env.service.get_member(info.id).phase in (PHASE_SPAWNING, PHASE_RUNNING)

    assert env.service.send(info.id, "follow up") == "queued"
    queued = records(env.parent, "team/message/queued")
    assert len(queued) == 1
    assert queued[0]["version"] == 1
    assert queued[0]["teamId"] == env.parent.session_id
    assert queued[0]["memberId"] == info.id
    assert queued[0]["message"] == {"text": "follow up"}

    gate.set()
    final = await env.service.wait(info.id)
    assert final.phase == PHASE_IDLE
    assert final.last_reply == "ok"
    assert env.agents.agents[0].prompts == ["initial task", "follow up"]
    delivered = records(env.parent, "team/message/delivered")
    assert [item["message"]["text"] for item in delivered] == ["follow up"]
    phases = [item["member"]["phase"] for item in records(env.parent, "team/member")]
    assert phases[-1] == PHASE_IDLE
    await env.service.close()


async def test_send_to_settled_member_starts_new_turn(tmp_path) -> None:
    env = make_team(tmp_path)
    info = env.service.spawn("worker", "d", "first task")
    first = await env.service.wait(info.id)
    assert first.phase == PHASE_COMPLETED
    assert env.service.send(info.id, "next task") == "started"
    second = await env.service.wait(info.id)
    assert second.phase == PHASE_IDLE
    assert second.last_reply == "ok"
    assert env.agents.agents[0].prompts == ["first task", "next task"]
    await env.service.close()


async def test_wait_times_out_for_busy_member(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_team(tmp_path, agents=FakeAgents(gate=gate))
    info = env.service.spawn("worker", "d", "blocked task")
    with pytest.raises(TeamError, match="timed out"):
        await env.service.wait(info.id, 0.02)
    await env.service.cancel(info.id)
    await env.service.close()


async def test_cancel_interrupts_member_and_drops_queue(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_team(tmp_path, agents=FakeAgents(gate=gate))
    info = env.service.spawn("worker", "d", "blocked task")
    await spin()
    assert env.service.send(info.id, "queued work") == "queued"

    assert await env.service.cancel(info.id) is True
    final = await env.service.wait(info.id)
    assert final.phase == PHASE_CANCELLED
    agent = env.agents.agents[0]
    assert agent.interrupted is True
    assert agent.prompts == ["blocked task"]
    assert records(env.parent, "team/member")[-1]["member"]["phase"] == PHASE_CANCELLED

    with pytest.raises(TeamError, match="cannot receive messages"):
        env.service.send(info.id, "late")
    assert await env.service.cancel(info.id) is False
    assert await env.service.cancel("member-unknown") is False
    with pytest.raises(TeamError, match="unknown team member"):
        await env.service.wait("member-unknown")
    await env.service.close()


async def test_spawn_honours_preset_model_and_provider_overrides(tmp_path) -> None:
    env = make_team(tmp_path, services={"llm": FakeLlm()}, preset="worker", model="lead-model")
    info = env.service.spawn(
        "worker",
        "d",
        "p",
        provider="remote",
        model="member-model",
        preset="default",
    )
    created = env.agents.created[0]
    assert created["preset"] == "default"
    assert created["preset_revision"] is None
    assert created["options"].model == "member-model"
    assert created["provider"].name == "remote"
    assert info.provider == "remote"
    assert info.model == "member-model"
    assert info.preset == "default"
    await env.service.wait(info.id)

    with pytest.raises(TeamError, match="unknown llm provider"):
        env.service.spawn("other", "d", "p", provider="missing")
    await env.service.close()


async def test_team_identity_defaults_and_override(tmp_path) -> None:
    env = make_team(tmp_path)
    assert env.service.parent_session_id == env.parent.session_id
    assert env.service.team_id == env.parent.session_id
    detached = TeamService()
    assert detached.parent_session_id == ""
    assert detached.team_id == "team"
    explicit = TeamService(team_id="team-alpha")
    explicit.bind_session(env.parent)
    assert explicit.team_id == "team-alpha"


async def test_append_record_tolerates_parent_without_append(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_team(tmp_path, agents=FakeAgents(gate=gate))
    env.service.bind_session(SimpleNamespace(session_id="session-fake"))
    info = env.service.spawn("worker", "d", "blocked")
    assert env.service.team_id == "session-fake"
    await env.service.cancel(info.id)
    await env.service.close()
