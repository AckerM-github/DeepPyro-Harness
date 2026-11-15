"""A forked or reopened session never drives itself until a human arms it."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.goal import GoalService
from deeppyro.hooks.service import HookService
from tests.agent.fakes import FakeSessionLog

pytestmark = pytest.mark.asyncio


def _copy(src: FakeSessionLog) -> FakeSessionLog:
    clone = FakeSessionLog(cwd=src.header.cwd)
    for record in src.records:
        clone.append(record.type, dict(record.data))
    return clone


async def test_a_forked_session_keeps_the_goal_but_starts_disarmed(tmp_path: Path) -> None:
    origin = FakeSessionLog(cwd=tmp_path)
    goals = GoalService({"maxChars": 2000})
    goals.configure(emit=HookService().emit)
    goals.bind(origin, record=origin.append)
    await goals.create(origin, "ship the plugin")
    assert goals.activation(origin) == "armed"

    forked = _copy(origin)
    goals.bind(forked, record=forked.append)

    view = goals.view(forked)
    assert view is not None
    assert view.goal.objective == "ship the plugin"
    assert view.rounds_started == 0
    assert view.activation == "disarmed"


async def test_arm_state_is_per_session(tmp_path: Path) -> None:
    first = FakeSessionLog(cwd=tmp_path)
    second = FakeSessionLog(cwd=tmp_path)
    goals = GoalService({"maxChars": 2000})
    goals.configure(emit=HookService().emit)
    for session in (first, second):
        goals.bind(session, record=session.append)

    await goals.create(first, "first goal")

    assert goals.activation(first) == "armed"
    assert goals.activation(second) == "disarmed"
    assert goals.goal(second) is None
