"""The goal round fence: ``docs/spec/goal.md`` section 7.5."""

from __future__ import annotations

import asyncio
from typing import Any

from conftest import FakeAgent, Harness, goal_snapshot, message

from deeppyro.goal.round_driver import PHASE_CLAIMED, PROMPT_REJECTED_CODE
from deeppyro.goal.types import BlockedReason
from deeppyro.hooks.types import AGENT_PRE_STEP


def _payload(messages: list[Any], signal: asyncio.Event) -> dict[str, Any]:
    return {
        "agent": None,
        "messages": list(messages),
        "turn": 1,
        "step": 1,
        "signal": signal,
        "modelSelection": {"provider": "scripted", "model": "scripted"},
        "tools": [],
    }


async def _fence(
    harness: Harness,
    messages: list[Any],
    *,
    fallback: Any = None,
    signal: asyncio.Event | None = None,
) -> Any:
    if fallback is None:
        fallback = {"kind": "enter", "messages": [item.model_dump() for item in messages]}
    payload = _payload(messages, signal if signal is not None else asyncio.Event())
    return await harness.hooks.waterfall(AGENT_PRE_STEP, payload, fallback, scope=harness.scope())


async def _driven(harness: Harness) -> tuple[FakeAgent, Any, Any]:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot())
    harness.goals.arm(agent.session)
    await harness.drive()
    assert state.attempt is not None
    return agent, state, agent.followups[0][2]


# -- 1. a valid reservation enters its step --------------------------------


async def test_valid_reservation_enters_and_starts_a_request_series(harness: Harness) -> None:
    agent, state, round_message = await _driven(harness)

    decision = await _fence(harness, [round_message])

    assert decision["kind"] == "enter"
    assert decision["startsRequestSeries"] is True
    assert state.attempt is not None
    assert state.attempt.phase == PHASE_CLAIMED
    assert agent.inbox.next_step == ()


async def test_a_message_without_a_round_source_passes_straight_through(
    harness: Harness,
) -> None:
    await _driven(harness)

    decision = await _fence(harness, [message("just a human")])

    assert decision["kind"] == "enter"
    assert "startsRequestSeries" not in decision


# -- 2. an invalid reservation is rejected and restores the rest -----------


async def test_stale_reservation_rejects_and_restores_the_other_claimed(harness: Harness) -> None:
    agent, state, round_message = await _driven(harness)
    harness.goals.goals["s1"] = goal_snapshot(revision=2)
    first = message("human one", message_id="h1")
    second = message("human two", message_id="h2")

    decision = await _fence(harness, [first, round_message, second])

    assert decision == {"kind": "reject"}
    assert state.attempt is None
    assert [item.id for item in agent.inbox.next_step] == ["h1", "h2"]
    assert round_message.id not in {item.id for item in agent.inbox.next_step}
    assert harness.goals.blocked == []


async def test_a_reservation_invalidated_while_entering_is_rejected(harness: Harness) -> None:
    agent, state, round_message = await _driven(harness)
    other = message("human", message_id="h1")

    def stale() -> dict[str, Any]:
        harness.goals.goals["s1"] = goal_snapshot(revision=9)
        return {"kind": "enter", "messages": []}

    decision = await _fence(harness, [other, round_message], fallback=stale)

    assert decision == {"kind": "reject"}
    assert state.attempt is None
    assert [item.id for item in agent.inbox.next_step] == ["h1"]


# -- 3. a downstream rejection blocks the goal -----------------------------


async def test_prompt_rejection_blocks_the_goal(harness: Harness) -> None:
    agent, state, round_message = await _driven(harness)

    decision = await _fence(
        harness,
        [round_message],
        fallback={"kind": "reject", "reason": "not this time"},
    )

    assert decision["kind"] == "reject"
    assert state.attempt is None
    assert len(harness.goals.blocked) == 1
    blocked = harness.goals.blocked[0]
    assert blocked["session"] == "s1"
    assert blocked["reason"] == BlockedReason(
        code=PROMPT_REJECTED_CODE,
        message="Goal round was rejected before entering its step.",
    )
    assert agent.inbox.next_step == ()


async def test_prompt_rejection_does_not_block_a_goal_that_moved_on(harness: Harness) -> None:
    _agent, state, round_message = await _driven(harness)

    def moving() -> dict[str, Any]:
        harness.goals.goals["s1"] = goal_snapshot(goal_id="g2", revision=4)
        return {"kind": "reject", "reason": "not this time"}

    decision = await _fence(harness, [round_message], fallback=moving)

    assert decision["kind"] == "reject"
    assert state.attempt is None
    assert harness.goals.blocked == []


# -- 4. an aborted step is not the driver'"'"'s business ------------------------


async def test_aborted_enter_restores_the_other_claimed_and_keeps_the_attempt(
    harness: Harness,
) -> None:
    agent, state, round_message = await _driven(harness)
    other = message("human", message_id="h1")
    signal = asyncio.Event()

    def aborting() -> dict[str, Any]:
        signal.set()
        return {"kind": "enter", "messages": [other.model_dump()]}

    decision = await _fence(harness, [other, round_message], fallback=aborting, signal=signal)

    assert decision["kind"] == "enter"
    assert state.attempt is not None
    assert state.attempt.cancelled is False
    assert [item.id for item in agent.inbox.next_step] == ["h1"]
    assert harness.goals.blocked == []
