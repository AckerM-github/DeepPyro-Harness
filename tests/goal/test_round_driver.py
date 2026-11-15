"""The goal round driver: the nine ``docs/spec/goal.md`` section 7 invariants."""

from __future__ import annotations

import asyncio
from typing import Any

from conftest import FakeAgent, FakeGoals, Harness, goal_snapshot, message

from deeppyro.agent.inbox import CLAIMED_EVENT, DISCARDED_EVENT, INSERTED_EVENT, TARGET_NEXT_TURN
from deeppyro.builtin.goal_round_driver import GoalRoundDriverPlugin
from deeppyro.goal.round_driver import (
    PHASE_CANCELLED,
    PHASE_CLAIMED,
    PHASE_IDLE,
    PHASE_QUEUED,
    RoundDriver,
    is_goal_round_source,
    render_goal_round_prompt,
)
from deeppyro.goal.types import GOAL_BUS_EVENT
from deeppyro.hooks.types import AGENT_ERROR, AGENT_PRE_STEP


class _ExplodingAgent(FakeAgent):
    """A session whose next-turn queue refuses the round."""

    def followup(self, text: str, *, source: dict[str, Any] | None = None) -> None:
        raise RuntimeError("boom")


class _VanishingAgent(FakeAgent):
    """A session that loses its goal exactly while the round is queued."""

    goals: FakeGoals | None = None

    def followup(self, text: str, *, source: dict[str, Any] | None = None) -> None:
        assert self.goals is not None
        self.goals.clear_goal(self.session)
        raise RuntimeError("gone")


class StubContext:
    """The smallest plugin context the driver plugin needs."""

    def __init__(self, **services: Any) -> None:
        self.services = services
        self.effects: list[tuple[Any, str]] = []
        self.provided: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        return self.services.get(name)

    def service(self, name: str) -> Any:
        if name in self.services:
            return self.services[name]
        raise KeyError(name)

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        self.effects.append((execute, label))
        return execute

    def provide(self, name: str, service: Any) -> None:
        self.provided[name] = service


def _round_message(agent: FakeAgent, index: int = 0) -> Any:
    return agent.followups[index][2]


async def _driven_agent(harness: Harness) -> tuple[FakeAgent, Any, Any]:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot())
    harness.goals.arm(agent.session)
    await harness.drive()
    assert state.attempt is not None
    return agent, state, _round_message(agent)


# -- 1. drivable and readyToDrive ------------------------------------------


def test_drivable_requires_an_active_armed_goal() -> None:
    assert RoundDriver.drivable(None, "armed") is False
    assert RoundDriver.drivable(goal_snapshot(phase="paused"), "armed") is False
    assert RoundDriver.drivable(goal_snapshot(phase="blocked"), "armed") is False
    assert RoundDriver.drivable(goal_snapshot(phase="complete"), "armed") is False
    assert RoundDriver.drivable(goal_snapshot(), "disarmed") is False
    assert RoundDriver.drivable(goal_snapshot(), "armed") is True


async def test_ready_to_drive_tracks_busy_competition_liveness_and_stop(
    harness: Harness,
) -> None:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot())
    driver = harness.driver
    assert driver.ready_to_drive(state) is True

    agent.busy = True
    assert driver.ready_to_drive(state) is False
    agent.busy = False

    state.competing_queued = True
    assert driver.ready_to_drive(state) is False
    state.competing_queued = False

    state.stopping = True
    assert driver.ready_to_drive(state) is False
    state.stopping = False

    harness.agents.agents.remove(agent)
    assert driver.ready_to_drive(state) is False
    harness.agents.agents.append(agent)
    assert driver.ready_to_drive(state) is True

    driver.detach()
    assert driver.ready_to_drive(state) is False


# -- 2. one round per drive, with the frozen prompt ------------------------


async def test_drive_queues_the_first_round_prompt(harness: Harness) -> None:
    agent = FakeAgent()
    goal = goal_snapshot()
    state = await harness.start(agent, goal=goal, armed=True)

    await harness.drive()

    assert len(agent.followups) == 1
    text, source, queued = agent.followups[0]
    assert text == render_goal_round_prompt(goal, 1)
    assert source == {"kind": "goal", "goalId": goal.id, "revision": goal.revision, "round": 1}
    assert is_goal_round_source(source) is True
    assert state.attempt is not None
    assert state.attempt.round == 1
    assert state.attempt.phase == PHASE_QUEUED
    assert state.attempt.message_id == queued.id
    assert queued in agent.inbox.next_turn


async def test_drive_uses_the_next_round_after_an_admitted_round(harness: Harness) -> None:
    agent = FakeAgent()
    goal = goal_snapshot()
    state = await harness.start(agent, goal=goal, rounds_started=1, armed=True)

    await harness.drive()

    text, source, _queued = agent.followups[0]
    assert source["round"] == 2
    assert text == render_goal_round_prompt(goal, 2)
    assert state.attempt is not None
    assert state.attempt.round == 2


async def test_drive_does_nothing_while_disarmed(harness: Harness) -> None:
    agent = FakeAgent()
    await harness.start(agent, goal=goal_snapshot(), armed=False)

    await harness.drive()

    assert agent.followups == []


# -- 3. failures -----------------------------------------------------------


async def test_round_limit_blocks_instead_of_queueing(harness: Harness) -> None:
    agent = FakeAgent()
    goal = goal_snapshot(max_goal_rounds=2)
    state = await harness.start(agent, goal=goal, rounds_started=2, armed=True)

    await harness.drive()

    assert agent.followups == []
    assert state.attempt is None
    assert len(harness.goals.blocked) == 1
    reason = harness.goals.blocked[0]["reason"]
    assert reason.code == "round-limit"
    assert reason.message == "Goal reached its configured limit of 2 rounds."


async def test_queue_failure_blocks_with_the_exception(harness: Harness) -> None:
    agent = _ExplodingAgent()
    state = await harness.start(agent, goal=goal_snapshot(), armed=True)

    await harness.drive()

    assert state.attempt is None
    assert state.phase == PHASE_IDLE
    assert len(harness.goals.blocked) == 1
    reason = harness.goals.blocked[0]["reason"]
    assert reason.code == "queue-failed"
    assert reason.message == "Could not queue goal round 1: boom"


async def test_queue_failure_without_a_goal_does_not_block(harness: Harness) -> None:
    agent = _VanishingAgent()
    agent.goals = harness.goals
    await harness.start(agent, goal=goal_snapshot(), armed=True)

    await harness.drive()

    assert harness.goals.blocked == []


# -- 4. pause interrupts only the driven session ---------------------------


async def test_pause_interrupts_a_busy_driven_agent(harness: Harness) -> None:
    agent = FakeAgent()
    goal = goal_snapshot()
    state = await harness.start(agent, goal=goal, armed=True)
    await harness.drive()
    assert state.attempt is not None

    agent.busy = True
    harness.goals.goals["s1"] = goal.with_phase("paused", revision=2)
    harness.goals.disarm(agent.session)
    await harness.emit(GOAL_BUS_EVENT, {"session": "s1", "goal": None})
    await asyncio.sleep(0.01)

    assert agent.interrupts == [{"clear_inbox": True}]


async def test_goal_change_for_another_session_never_interrupts(harness: Harness) -> None:
    agent = FakeAgent()
    goal = goal_snapshot()
    state = await harness.start(agent, goal=goal, armed=True)
    await harness.drive()
    assert state.attempt is not None

    agent.busy = True
    harness.goals.goals["s1"] = goal.with_phase("paused", revision=2)
    await harness.emit(GOAL_BUS_EVENT, {"session": "s9", "goal": None}, session_id="s9")
    await asyncio.sleep(0.01)

    assert agent.interrupts == []


async def test_goal_change_without_an_attempt_never_interrupts(harness: Harness) -> None:
    agent = FakeAgent()
    await harness.start(agent, goal=goal_snapshot(), armed=True)
    agent.busy = True

    await harness.emit(GOAL_BUS_EVENT, {"session": "s1", "goal": None})
    await asyncio.sleep(0.01)

    assert agent.interrupts == []


# -- 5. competing input and the reservation lifecycle ----------------------


async def test_competing_insert_marks_the_queued_attempt_stale(harness: Harness) -> None:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot(), armed=True)
    await harness.drive()
    assert state.attempt is not None

    competing = message("a human interrupt", message_id="h1")
    agent.inbox.push(TARGET_NEXT_TURN, competing)
    await harness.emit(INSERTED_EVENT, {"message": competing.model_dump(exclude_none=True)})

    assert state.competing_queued is True
    assert state.attempt is not None
    assert state.attempt.stale is True
    assert harness.driver.ready_to_drive(state) is False


async def test_insert_of_the_round_itself_is_not_competing(harness: Harness) -> None:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot(), armed=True)
    await harness.drive()
    queued = _round_message(agent)

    await harness.emit(INSERTED_EVENT, {"message": queued.model_dump(exclude_none=True)})

    assert state.competing_queued is False
    assert state.attempt is not None
    assert state.attempt.stale is False


async def test_claim_marks_the_attempt_claimed(harness: Harness) -> None:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot(), armed=True)
    await harness.drive()
    queued = _round_message(agent)

    await harness.emit(
        CLAIMED_EVENT,
        {"message": queued.model_dump(exclude_none=True), "turn": 1},
    )

    assert state.attempt is not None
    assert state.attempt.phase == PHASE_CLAIMED
    assert state.phase == PHASE_CLAIMED


async def test_discard_cancels_the_attempt(harness: Harness) -> None:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot(), armed=True)
    await harness.drive()
    queued = _round_message(agent)

    await harness.emit(DISCARDED_EVENT, {"message": queued.model_dump(exclude_none=True)})

    assert state.attempt is not None
    assert state.attempt.cancelled is True
    assert state.phase == PHASE_CANCELLED


async def test_admission_is_seen_on_the_durable_log(harness: Harness) -> None:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot(), armed=True)
    await harness.drive()
    queued = _round_message(agent)

    agent.session.emit("user/message", queued.model_dump(exclude_none=True))

    assert state.attempt is not None
    assert state.attempt.phase == "admitted"


# -- 6. turn ends ---------------------------------------------------------


async def test_turn_end_max_tokens_disarms(harness: Harness) -> None:
    agent = FakeAgent()
    await harness.start(agent, goal=goal_snapshot(), armed=True)
    await harness.drive()

    agent.session.emit("turn/end", {"turn": 1, "reason": {"kind": "max-tokens"}})

    assert harness.goals.activation(agent.session) == "disarmed"


async def test_turn_end_aborted_cancels_a_claimed_round(harness: Harness) -> None:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot(), armed=True)
    await harness.drive()
    assert state.attempt is not None
    state.attempt.phase = PHASE_CLAIMED

    agent.session.emit("turn/end", {"turn": 1, "reason": {"kind": "aborted", "reason": {"kind": "user"}}})

    assert state.attempt is not None
    assert state.attempt.cancelled is True
    assert state.phase == PHASE_CANCELLED
    assert harness.goals.activation(agent.session) == "armed"


async def test_turn_end_aborted_disposed_disarms(harness: Harness) -> None:
    agent = FakeAgent()
    await harness.start(agent, goal=goal_snapshot(), armed=True)
    await harness.drive()

    agent.session.emit(
        "turn/end",
        {"turn": 1, "reason": {"kind": "aborted", "reason": {"kind": "disposed"}}},
    )

    assert harness.goals.activation(agent.session) == "disarmed"


async def test_agent_error_disarms(harness: Harness) -> None:
    agent = FakeAgent()
    await harness.start(agent, goal=goal_snapshot(), armed=True)

    await harness.emit(AGENT_ERROR, {"agent": agent, "error": RuntimeError("boom"), "phase": "turn"})

    assert harness.goals.activation(agent.session) == "disarmed"


# -- 7. adoption -----------------------------------------------------------


async def test_adopt_disarms_and_skips_subagents(harness: Harness) -> None:
    root = FakeAgent("s1")
    subagent = FakeAgent("s2", origin="subagent")
    harness.agents.create(root)
    harness.agents.create(subagent)
    harness.goals.set_goal(root.session, goal_snapshot())
    harness.goals.arm(root.session)

    harness.driver.attach()

    assert harness.driver.state_for(subagent) is None
    assert harness.driver.state_for(root) is not None
    assert harness.goals.activation(root.session) == "disarmed"
    harness.states["s1"] = harness.driver.state_for(root)
    await harness.settle("s1")


async def test_created_agent_is_adopted(harness: Harness) -> None:
    harness.driver.attach()
    agent = FakeAgent("s3")
    harness.goals.set_goal(agent.session, goal_snapshot())

    harness.agents.create(agent)

    state = harness.driver.state_for(agent)
    assert state is not None
    harness.states["s3"] = state
    await harness.settle("s3")
    assert harness.goals.activation(agent.session) == "disarmed"


# -- 8. single flight ------------------------------------------------------


async def test_request_drive_coalesces_onto_the_running_task(harness: Harness) -> None:
    agent = FakeAgent()
    state = await harness.start(agent, goal=goal_snapshot())
    harness.goals.arm(agent.session)
    gate = asyncio.Event()
    original = harness.driver.drive

    async def slow(candidate: Any) -> None:
        await gate.wait()
        await original(candidate)

    harness.driver.drive = slow  # type: ignore[method-assign]
    harness.driver.request_drive(state)
    running = state.run
    assert running is not None
    harness.driver.request_drive(state)
    assert state.run is running
    gate.set()
    await running
    await harness.settle()
    assert len(agent.followups) == 1


async def test_detach_interrupts_a_running_round_without_dropping_the_inbox(
    harness: Harness,
) -> None:
    agent, state, _round = await _driven_agent(harness)
    agent.busy = True

    harness.driver.detach()
    await asyncio.sleep(0)

    assert state.stopping is True
    assert agent.interrupts == [{"clear_inbox": False}]
    assert state.disposers == []


# -- 9. opt-in mounting ----------------------------------------------------


def test_disabled_driver_registers_no_listeners_and_no_traffic(harness: Harness) -> None:
    agent = FakeAgent()
    harness.agents.create(agent)
    ctx = StubContext(hooks=harness.hooks, agents=harness.agents, goal=harness.goals)
    plugin = GoalRoundDriverPlugin(ctx, {"driver": False})

    plugin.setup(ctx, {"driver": False})

    assert plugin.enabled is False
    assert plugin.driver is None
    assert ctx.effects == []
    assert harness.hooks.listeners(GOAL_BUS_EVENT) == ()
    assert harness.hooks.listeners(AGENT_PRE_STEP) == ()
    assert harness.agents.callbacks == []


def test_default_driver_is_off(harness: Harness) -> None:
    ctx = StubContext(hooks=harness.hooks, agents=harness.agents, goal=harness.goals)
    plugin = GoalRoundDriverPlugin(ctx, {})

    plugin.setup(ctx, {})

    assert plugin.enabled is False
    assert plugin.driver is None
    assert harness.hooks.listeners(GOAL_BUS_EVENT) == ()


def test_enabled_driver_attaches_and_disarms_on_load(harness: Harness) -> None:
    agent = FakeAgent()
    harness.agents.create(agent)
    harness.goals.set_goal(agent.session, goal_snapshot())
    harness.goals.arm(agent.session)
    ctx = StubContext(hooks=harness.hooks, agents=harness.agents, goal=harness.goals)
    plugin = GoalRoundDriverPlugin(ctx, {"goal": {"driver": True}})

    plugin.setup(ctx, {"goal": {"driver": True}})

    assert plugin.enabled is True
    assert plugin.driver is not None
    assert harness.hooks.listeners(GOAL_BUS_EVENT) != []
    assert harness.hooks.listeners(AGENT_PRE_STEP) != []
    assert len(ctx.effects) == 1
    assert harness.goals.activation(agent.session) == "disarmed"


def test_driver_flag_must_be_a_boolean(harness: Harness) -> None:
    ctx = StubContext(hooks=harness.hooks, agents=harness.agents, goal=harness.goals)
    plugin = GoalRoundDriverPlugin(ctx, {"driver": "yes"})

    try:
        plugin.setup(ctx, {"driver": "yes"})
    except TypeError as error:
        assert "driver" in str(error)
    else:  # pragma: no cover - a string flag must never enable the driver
        raise AssertionError("a string driver flag was accepted")
