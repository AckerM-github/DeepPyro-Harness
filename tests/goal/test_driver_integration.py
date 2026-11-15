"""The round driver driving a real AgentSession end to end."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.protocols import TextDelta
from deeppyro.goal import GoalService
from deeppyro.goal.round_driver import RoundDriver
from deeppyro.hooks.service import HookService
from tests.agent.fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)
from tests.goal.conftest import FakeAgents

pytestmark = pytest.mark.asyncio


def _agent(tmp_path: Path, script: list[ScriptedTurn]) -> tuple[Any, AgentSession]:
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry([]),
        options=AgentOptions(model="test-model"),
        sink=RecordingSink(session),
        permission=FakePermission(),
        cwd=tmp_path,
    )
    return provider, agent


async def _wait_for(predicate: Any, *, tries: int = 500) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def _setup(tmp_path: Path, script: list[ScriptedTurn]) -> tuple[Any, AgentSession, GoalService, RoundDriver]:
    provider, agent = _agent(tmp_path, script)
    goals = GoalService({"maxChars": 2000})
    hooks = HookService()
    goals.configure(emit=hooks.emit)
    goals.bind(agent.session, record=agent.session.append)
    agents = FakeAgents()
    agents.create(agent)
    driver = RoundDriver(hooks=hooks, agents=agents, goals=goals)
    driver.attach()
    return provider, agent, goals, driver


async def test_the_driver_queues_a_round_that_the_real_loop_answers(tmp_path: Path) -> None:
    provider, agent, goals, driver = _setup(
        tmp_path,
        [ScriptedTurn(events=[TextDelta("round done"), finish("stop")])],
    )
    try:
        # Adopting a session disarms it, so a goal created afterwards is the one
        # a human would arm next (`/goal <text>` or `/goal resume`).
        await goals.create(agent.session, "ship the plugin", max_goal_rounds=1)
        await _wait_for(lambda: goals.state(agent.session).rounds_started == 1)
        await agent.wait_for_idle()

        sources = [
            record.data.get("source") for record in agent.session.records if record.type == "user/message"
        ]
        rounds = [source for source in sources if isinstance(source, dict) and source.get("kind") == "goal"]
        assert rounds, sources
        assert rounds[0]["round"] == 1
        assert rounds[0]["revision"] == 1
        assert str(rounds[0]["goalId"]).startswith("goal-")
        ends = [record.data for record in agent.session.records if record.type == "turn/end"]
        assert ends and ends[-1]["reason"] == {"kind": "completed"}
        assert provider.requests, "the loop never ran the round"
    finally:
        driver.detach()


async def test_a_disabled_driver_leaves_the_session_untouched(tmp_path: Path) -> None:
    provider, agent = _agent(tmp_path, [ScriptedTurn(events=[TextDelta("never"), finish("stop")])])
    goals = GoalService({"maxChars": 2000})
    goals.configure(emit=HookService().emit)
    goals.bind(agent.session, record=agent.session.append)
    await goals.create(agent.session, "quiet goal", max_goal_rounds=4)

    driver = RoundDriver(hooks=HookService(), agents=FakeAgents(), goals=goals)
    assert driver.states() == ()
    assert provider.requests == []
    assert not [record for record in agent.session.records if record.type == "user/message"]
