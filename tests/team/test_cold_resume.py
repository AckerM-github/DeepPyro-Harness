"""Cold resume: rebuild a team from the records of its lead session."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from deeppyro.team import (
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_RUNNING,
)
from deeppyro.team.manager import TeamService
from tests.team.helpers import (
    FakeAgents,
    TeamEnv,
    make_team,
    records,
    spin,
)


def rebound(env: TeamEnv, *, agents: FakeAgents | None = None) -> TeamService:
    """Build a second service around the same lead session, as after a restart."""
    service = TeamService(
        agents=agents if agents is not None else env.agents,
        sessions=SimpleNamespace(store=env.store),
        sandbox=env.sandbox,
    )
    service.configure(
        cwd=env.cwd,
        provider=env.provider,
        registry="registry",
        permission=env.permission,
        model="parent-model",
        context_window=8192,
        preset="default",
        preset_revision=None,
        services={},
        ask="ask-callback",
    )
    service.bind_session(env.parent)
    return service


async def test_spawn_member_id_is_child_session_id(tmp_path) -> None:
    env = make_team(tmp_path)
    info = env.service.spawn("scout", "researches the bug", "find the bug")
    await spin()
    assert info.id == info.session_id
    assert info.id.startswith("session-")
    child = env.store.open(info.id)
    assert child.header.id == info.id
    assert child.header.parentSession == env.parent.session_id
    assert child.header.agentPreset == "default"
    assert records(env.parent, "subagent/catalog")[0]["childId"] == info.id
    assert records(env.parent, "subagent/descriptor")[0]["childId"] == info.id
    await env.service.close()


async def test_restore_replays_roster_and_board(tmp_path) -> None:
    env = make_team(tmp_path)
    scout = env.service.spawn("scout", "researches the bug", "find the bug")
    writer = env.service.spawn("writer", "writes the fix", "write it up")
    await spin(6)
    first = env.service.create_task("Write tests", "cover cold resume", write_scopes=["tests/team"])
    second = env.service.create_task("Review tests", blocked_by=[first.id])
    claimed = env.service.update_task(first.id, "claim")
    await env.service.close()

    service = rebound(env)
    restored = await service.restore()
    assert [item.id for item in restored] == [scout.id, writer.id]
    assert [item.name for item in restored] == ["scout", "writer"]
    assert [item.session_id for item in restored] == [scout.id, writer.id]
    assert all(item.phase in (PHASE_COMPLETED, PHASE_IDLE) for item in restored)

    board = {task.id: task for task in service.tasks()}
    assert board[first.id] == claimed
    assert board[first.id].owner == "lead"
    assert board[first.id].created_at == first.created_at
    assert board[second.id].blocked_by == (first.id,)
    assert board[second.id].created_at == second.created_at

    # Idempotent: a second restore neither replays records nor duplicates rows.
    record_count = len(records(env.parent, "team/task"))
    again = await service.restore()
    assert [item.id for item in again] == [scout.id, writer.id]
    assert len(service.tasks()) == 2
    assert len(records(env.parent, "team/task")) == record_count
    await service.close()


async def test_restore_queued_message_auto_resumes(tmp_path) -> None:
    gate = asyncio.Event()
    env = make_team(tmp_path, agents=FakeAgents(gate=gate))
    info = env.service.spawn("scout", "researches the bug", "first task")
    await spin()
    assert env.service.send(info.id, "second task") == "queued"

    restarted_agents = FakeAgents()
    service = rebound(env, agents=restarted_agents)
    restored = await service.restore()
    assert [item.id for item in restored] == [info.id]
    assert restored[0].phase == PHASE_RUNNING

    await spin(6)
    member = service.get_member(info.id)
    assert member is not None
    assert member.phase == PHASE_IDLE
    assert member.last_reply == "ok"
    assert len(restarted_agents.created) == 1
    assert restarted_agents.created[0]["session"].session_id == info.id
    queued = [item["message"]["text"] for item in records(env.parent, "team/message/queued")]
    delivered = [item["message"]["text"] for item in records(env.parent, "team/message/delivered")]
    assert queued == ["second task"]
    assert delivered == ["second task"]

    member_records = len(records(env.parent, "team/member"))
    await service.restore()
    assert len(records(env.parent, "team/member")) == member_records

    # Release the crashed service so its blocked task can finish before shutdown.
    gate.set()
    await spin(8)
    await env.service.close()
    await service.close()


async def test_restore_ignores_other_team_records(tmp_path) -> None:
    env = make_team(tmp_path)
    info = env.service.spawn("scout", "researches the bug", "first task")
    await spin(6)
    env.parent.append(
        "team/member",
        {
            "version": 1,
            "teamId": "other-team",
            "member": {
                "id": "session-other",
                "name": "intruder",
                "description": "",
                "provider": "local",
                "context": "default",
                "phase": "idle",
                "model": "parent-model",
            },
        },
    )
    service = rebound(env)
    restored = await service.restore()
    assert [item.id for item in restored] == [info.id]
    await service.close()


async def test_send_cold_wakes_idle_member(tmp_path) -> None:
    env = make_team(tmp_path)
    info = env.service.spawn("scout", "researches the bug", "first task")
    await spin(6)
    await env.service.close()

    agents = FakeAgents()
    service = rebound(env, agents=agents)
    restored = await service.restore()
    assert restored[0].phase == PHASE_COMPLETED
    assert agents.created == []

    assert service.send(info.id, "ping") == "started"
    assert len(agents.created) == 1
    created = agents.created[0]
    assert created["session"].session_id == info.id
    assert created["preset"] == "default"
    await spin(6)
    member = service.get_member(info.id)
    assert member is not None
    assert member.phase == PHASE_IDLE
    assert member.last_reply == "ok"
    delivered = [item["message"]["text"] for item in records(env.parent, "team/message/delivered")]
    assert "ping" in delivered
    await service.close()


async def test_cold_wake_closes_open_turn(tmp_path) -> None:
    env = make_team(tmp_path)
    info = env.service.spawn("scout", "researches the bug", "first task")
    await spin(6)
    child = env.store.open(info.id)
    child.append("turn/start", {"turn": 1})
    child.append("step/start", {"turn": 1, "step": 1})

    service = rebound(env, agents=FakeAgents())
    await service.restore()
    assert service.send(info.id, "resume") == "started"
    await spin(6)
    repaired = env.store.open(info.id)
    assert repaired.records[-1].type == "turn/end"
    assert repaired.records[-1].data == {"turn": 1, "reason": {"kind": "interrupted"}}
    assert "step/end" in [record.type for record in repaired.records]
    await env.service.close()
    await service.close()


async def test_restore_missing_session_marks_failed(tmp_path) -> None:
    env = make_team(tmp_path)
    gone = env.service.spawn("gone", "its session is lost", "first task")
    alive = env.service.spawn("alive", "its session is present", "second task")
    await spin(6)
    env.store.open(gone.id).path.unlink()

    service = rebound(env)
    restored = await service.restore()
    by_id = {item.id: item for item in restored}
    assert by_id[gone.id].phase == PHASE_FAILED
    assert by_id[gone.id].error is not None
    assert "cannot restore team member session" in by_id[gone.id].error
    assert by_id[alive.id].phase == PHASE_COMPLETED
    await service.close()


async def test_restore_legacy_member_id_without_session(tmp_path) -> None:
    env = make_team(tmp_path)
    info = env.service.spawn("scout", "researches the bug", "first task")
    await spin(6)
    env.parent.append(
        "team/member",
        {
            "version": 1,
            "teamId": env.parent.session_id,
            "member": {
                "id": "member-legacy",
                "name": "legacy",
                "description": "old team record",
                "provider": "local",
                "context": "default",
                "phase": "idle",
                "model": "parent-model",
            },
        },
    )
    service = rebound(env)
    restored = await service.restore()
    by_id = {item.id: item for item in restored}
    assert by_id["member-legacy"].phase == PHASE_FAILED
    assert "cannot restore team member session" in (by_id["member-legacy"].error or "")
    assert by_id[info.id].phase == PHASE_COMPLETED
    await service.close()


async def test_restore_empty_session_returns_empty(tmp_path) -> None:
    env = make_team(tmp_path)
    service = rebound(env)
    assert await service.restore() == ()
    assert service.members() == []
    assert service.tasks() == []
    await service.close()


async def test_restore_without_parent_is_empty(tmp_path) -> None:
    env = make_team(tmp_path)
    service = TeamService(
        agents=env.agents,
        sessions=SimpleNamespace(store=env.store),
        sandbox=env.sandbox,
    )
    assert await service.restore() == ()


async def test_close_closes_agents_and_keeps_roster(tmp_path) -> None:
    env = make_team(tmp_path)
    info = env.service.spawn("scout", "researches the bug", "first task")
    await spin(6)
    task = env.service.create_task("Write tests")
    await env.service.close()
    assert env.agents.agents
    assert all(agent.closed for agent in env.agents.agents)
    assert [item.id for item in env.service.members()] == [info.id]
    assert [item.id for item in env.service.tasks()] == [task.id]
