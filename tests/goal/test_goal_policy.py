"""The goal tools keep DSH's prompt guidance and close their own turn."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from deeppyro.builtin.goal import GOAL_POLICY, GoalPlugin
from deeppyro.core.protocols import ToolContext
from deeppyro.goal.service import GoalService
from deeppyro.session.schema import NativeSession
from deeppyro.tools.builtin.goal import UpdateGoalTool

SESSION_ID = "session-11111111-1111-1111-1111-111111111111"


class _Ctx:
    def __init__(self, session_id: str = "session-1") -> None:
        self.session_id = session_id


class _Agent:
    """The native agent host surface the goal ladder reads."""

    def __init__(self, session: NativeSession, *, human: bool) -> None:
        self.session = session
        self.preset = ""
        self.current_turn_human_input = lambda: human
        self.current_turn_goal_round = lambda: None


@pytest.fixture
def session(tmp_path: Path) -> Iterator[NativeSession]:
    native = NativeSession.create(
        tmp_path / f"{SESSION_ID}.jsonl",
        cwd=tmp_path,
        session_id=SESSION_ID,
    )
    try:
        yield native
    finally:
        native.close()


def _context(service: GoalService, session: NativeSession, agent: _Agent) -> ToolContext:
    return ToolContext(
        cwd=Path("."),
        session_id=session.header.id,
        turn=1,
        step=1,
        call_id="call-1",
        cancel=asyncio.Event(),
        log=lambda record_type, data: 0,
        services={"goal": service},
        agent=agent,
    )


def test_the_policy_section_carries_the_threshold() -> None:
    plugin = GoalPlugin.__new__(GoalPlugin)
    plugin.blocked_after = 5

    text = GoalPlugin._policy(plugin, None, _Ctx())

    assert text.startswith("## Goal tool")
    assert "at least 5 consecutive goal rounds" in text
    assert GOAL_POLICY.format(rounds=5) in text


def test_the_policy_section_is_skipped_without_a_session() -> None:
    plugin = GoalPlugin.__new__(GoalPlugin)
    plugin.blocked_after = 3

    assert GoalPlugin._policy(plugin, None, _Ctx(session_id="")) == ""


async def test_complete_inside_a_goal_round_closes_the_turn(session: NativeSession) -> None:
    service = GoalService()
    agent = _Agent(session, human=False)
    concluded: list[dict[str, Any]] = []
    ctx = _context(service, session, agent)
    ctx.conclude_turn = concluded.append
    view = await service.create(session, "keep going")
    agent.current_turn_goal_round = lambda: {  # type: ignore[method-assign]
        "goalId": view.goal.id,
        "revision": view.goal.revision,
        "round": 1,
    }

    result = await UpdateGoalTool().run(
        {"goal_id": view.goal.id, "revision": view.goal.revision, "action": "complete"},
        ctx,
    )

    assert "complete" in result.text
    assert concluded == [{"kind": "completed"}]


async def test_complete_in_a_human_turn_leaves_the_loop_alone(session: NativeSession) -> None:
    service = GoalService()
    agent = _Agent(session, human=True)
    concluded: list[dict[str, Any]] = []
    ctx = _context(service, session, agent)
    ctx.conclude_turn = concluded.append
    view = await service.create(session, "keep going")

    await UpdateGoalTool().run(
        {"goal_id": view.goal.id, "revision": view.goal.revision, "action": "complete"},
        ctx,
    )

    assert concluded == []


def test_the_service_is_not_touched_by_the_policy_helper() -> None:
    # guard against the helper accidentally reading state during prompt assembly
    plugin = GoalPlugin.__new__(GoalPlugin)
    plugin.blocked_after = 3
    plugin.service = GoalService({})

    assert "consecutive goal rounds" in GoalPlugin._policy(plugin, None, _Ctx())
