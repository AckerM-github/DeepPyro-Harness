"""KI-015: team members surface real provider usage per run and in aggregate."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.session.session import Session
from deeppyro.subagent import SubagentUsage
from deeppyro.team import PHASE_CANCELLED, PHASE_FAILED, PHASE_IDLE, MemberRun, TeamService
from tests.session import support
from tests.team.helpers import FakeAgent, FakeAgents, make_team, records, spin


class TeamUsageAgent(FakeAgent):
    """Team member double that writes native usage facts into its child session."""

    def __init__(
        self,
        session: Session,
        *,
        per_turn: list[tuple[int, int, int]],
        reply: str = "ok",
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(reply, gate=gate)
        self.member_session = session
        self.per_turn = list(per_turn)
        self.error = error
        self.turn = 0
        self.turns: list[int] = []

    async def send(self, text: str) -> str:
        self.prompts.append(text)
        self.turn += 1
        self.turns.append(self.turn)
        turn = self.turn
        script = self.per_turn[min(turn - 1, len(self.per_turn) - 1)]
        self.member_session.append("turn/start", {"turn": turn})
        self.member_session.append("step/start", {"turn": turn, "step": 1})
        self.member_session.append("user/message", support.user_message(f"msg_user_{turn}", text))
        payload = support.assistant_message(f"msg_assistant_{turn}", f"reply {turn}", turn=turn, step=1)
        payload["usage"] = {
            "inputTokens": script[0],
            "outputTokens": script[1],
            "cacheReadTokens": script[2],
        }
        self.member_session.append("assistant/message", payload)
        self.member_session.append("step/end", {"turn": turn, "step": 1})
        self.member_session.append("turn/end", {"turn": turn})
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.reply


class ScriptedTeamAgents(FakeAgents):
    """Agents double that builds a TeamUsageAgent per member from a script."""

    def __init__(self, scripts: list[dict[str, Any]]) -> None:
        super().__init__()
        self.scripts = list(scripts)

    def create(self, **kwargs: Any) -> TeamUsageAgent:
        script = self.scripts.pop(0)
        agent = TeamUsageAgent(kwargs["session"], **script)
        self.created.append(kwargs)
        self.agents.append(agent)
        return agent


class RecordingSink:
    """Optional usage sink double (structural match for the KI-018 service)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> None:
        self.calls.append(
            {
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
            }
        )


async def _wait_for(predicate: Any, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


async def test_single_member_usage_is_recorded_per_run(tmp_path: Path) -> None:
    agents = ScriptedTeamAgents([{"per_turn": [(12, 4, 3)]}])
    env = make_team(tmp_path, agents=agents)
    member = env.service.spawn("worker", "does work", "first task")
    await env.service.wait(member.id)

    runs = env.service.runs(member.id)
    assert len(runs) == 1
    run = runs[0]
    assert isinstance(run, MemberRun)
    assert run.ok is True
    assert run.cancelled is False
    assert run.error == ""
    assert (run.usage.input_tokens, run.usage.output_tokens, run.usage.cached_tokens) == (12, 4, 3)
    assert run.usage.steps == 1
    assert run.usage.latency_ms >= 0
    assert run.usage.model == "parent-model"
    assert run.usage.provider == "local"
    assert env.service.run_usage(run.run_id) == run.usage
    assert env.service.run(run.run_id) == run
    assert env.service.usage(member.id) == run.usage
    assert env.service.usage() == run.usage
    assert env.service.get_member(member.id) is not None
    assert env.service.get_member(member.id).phase == "completed"

    facts = records(env.parent, "team/usage")
    assert len(facts) == 1
    assert facts[0]["runId"] == run.run_id
    assert facts[0]["memberId"] == member.id
    assert facts[0]["usage"]["inputTokens"] == 12
    assert facts[0]["ok"] is True


async def test_member_turns_are_distinct_runs_and_aggregate(tmp_path: Path) -> None:
    agents = ScriptedTeamAgents([{"per_turn": [(10, 2, 1), (20, 5, 2)]}])
    env = make_team(tmp_path, agents=agents)
    member = env.service.spawn("worker", "does work", "first task")
    await env.service.wait(member.id)

    assert env.service.send(member.id, "second task") == "started"
    await env.service.wait(member.id)

    runs = env.service.runs(member.id)
    assert len(runs) == 2
    assert [(run.usage.input_tokens, run.usage.output_tokens, run.usage.cached_tokens) for run in runs] == [
        (10, 2, 1),
        (20, 5, 2),
    ]
    total = env.service.usage()
    assert (total.input_tokens, total.output_tokens, total.cached_tokens) == (30, 7, 3)
    assert total.steps == 2
    assert len(records(env.parent, "team/usage")) == 2


async def test_failed_member_keeps_partial_usage(tmp_path: Path) -> None:
    agents = ScriptedTeamAgents([{"per_turn": [(7, 3, 1)], "error": RuntimeError("member exploded")}])
    env = make_team(tmp_path, agents=agents)
    member = env.service.spawn("worker", "does work", "first task")
    info = await env.service.wait(member.id)

    assert info.phase == PHASE_FAILED
    runs = env.service.runs(member.id)
    assert len(runs) == 1
    run = runs[0]
    assert run.ok is False
    assert "RuntimeError: member exploded" in run.error
    assert (run.usage.input_tokens, run.usage.output_tokens, run.usage.cached_tokens) == (7, 3, 1)
    assert run.usage.steps == 1


async def test_cancelled_member_keeps_partial_usage(tmp_path: Path) -> None:
    gate = asyncio.Event()
    agents = ScriptedTeamAgents([{"per_turn": [(9, 5, 2)], "gate": gate}])
    env = make_team(tmp_path, agents=agents)
    member = env.service.spawn("worker", "does work", "first task")

    async def child_has_facts() -> bool:
        child = env.store.open(member.id)
        return any(record.type == "assistant/message" for record in child.records)

    for _ in range(100):
        if await child_has_facts():
            break
        await spin(1)
    else:  # pragma: no cover - defensive
        raise AssertionError("member never wrote usage facts")

    assert await env.service.cancel(member.id) is True
    await _wait_for(lambda: len(env.service.runs(member.id)) == 1)
    run = env.service.runs(member.id)[0]
    assert run.cancelled is True
    assert run.ok is False
    assert (run.usage.input_tokens, run.usage.output_tokens, run.usage.cached_tokens) == (9, 5, 2)
    info = env.service.get_member(member.id)
    assert info is not None
    assert info.phase == PHASE_CANCELLED


async def test_restore_recovers_usage_runs(tmp_path: Path) -> None:
    agents = ScriptedTeamAgents([{"per_turn": [(10, 2, 1), (20, 5, 2)]}])
    env = make_team(tmp_path, agents=agents)
    member = env.service.spawn("worker", "does work", "first task")
    await env.service.wait(member.id)
    assert env.service.send(member.id, "second task") == "started"
    await env.service.wait(member.id)
    expected = {run.run_id: run.usage for run in env.service.runs()}
    assert len(expected) == 2

    fresh = TeamService(
        agents=env.agents,
        sessions=SimpleNamespace(store=env.store),
        sandbox=env.sandbox,
    )
    fresh.configure(
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
    fresh.bind_session(env.parent)
    restored = await fresh.restore()

    assert [item.id for item in restored] == [member.id]
    assert {run.run_id: run.usage for run in fresh.runs()} == expected
    assert fresh.usage() == env.service.usage()
    assert fresh.get_member(member.id) is not None
    assert fresh.get_member(member.id).phase == PHASE_IDLE


async def test_team_usage_sink_is_optional_and_decoupled(tmp_path: Path) -> None:
    sink = RecordingSink()
    agents = ScriptedTeamAgents([{"per_turn": [(11, 6, 2)]}])
    env = make_team(tmp_path, agents=agents, services={"usage": sink})
    member = env.service.spawn("worker", "does work", "first task")
    await env.service.wait(member.id)

    assert sink.calls == [
        {
            "provider": "local",
            "model": "parent-model",
            "input_tokens": 11,
            "output_tokens": 6,
            "cached_tokens": 2,
        }
    ]
    usage = env.service.usage()
    assert isinstance(usage, SubagentUsage)
