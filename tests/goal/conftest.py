"""Fakes for the goal round driver tests: real bus, real inbox, fake agent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent.inbox import TARGET_NEXT_STEP, TARGET_NEXT_TURN, Inbox
from deeppyro.core.wire import Message, TextPart
from deeppyro.goal.round_driver import RoundDriver
from deeppyro.goal.types import BlockedReason, GoalRef, GoalSnapshot, GoalState
from deeppyro.hooks import HookService


def message(text: str, *, source: dict[str, Any] | None = None, message_id: str = "") -> Message:
    """A user message with the given source and id."""
    return Message(role="user", content=[TextPart(text=text)], id=message_id, source=source)


def goal_round_message(
    *,
    goal_id: str = "g1",
    revision: int = 1,
    round_number: int = 1,
    text: str = "round",
    message_id: str = "round-1",
) -> Message:
    """The message the driver queues for one goal round."""
    return message(
        text,
        source={"kind": "goal", "goalId": goal_id, "revision": revision, "round": round_number},
        message_id=message_id,
    )


def goal_snapshot(
    *,
    goal_id: str = "g1",
    revision: int = 1,
    phase: str = "active",
    max_goal_rounds: int = 4,
    objective: str = "ship it",
) -> GoalSnapshot:
    return GoalSnapshot(
        id=goal_id,
        revision=revision,
        objective=objective,
        phase=phase,
        max_goal_rounds=max_goal_rounds,
    )


class FakeSession:
    """The durable log surface the driver subscribes to."""

    def __init__(self, session_id: str = "s1", *, origin: str | None = "user") -> None:
        self.header = SimpleNamespace(id=session_id, origin=origin, parentSession=None)
        self.session_id = session_id
        self.subscribers: list[Any] = []
        self.records: list[tuple[str, Any, int]] = []
        self.seq = 0

    def subscribe(self, callback: Any) -> Any:
        self.subscribers.append(callback)

        def dispose() -> None:
            if callback in self.subscribers:
                self.subscribers.remove(callback)

        return dispose

    def emit(self, record_type: str, data: Any) -> None:
        """Append one durable record and notify the subscribers synchronously."""
        self.seq += 1
        self.records.append((record_type, data, self.seq))
        for callback in tuple(self.subscribers):
            callback(record_type, data, self.seq)


class FakeAgent:
    """An agent session surface: real inbox, no provider, no loop."""

    def __init__(
        self,
        session_id: str = "s1",
        *,
        origin: str | None = "user",
        busy: bool = False,
        preset: str = "default",
    ) -> None:
        self.session = FakeSession(session_id, origin=origin)
        self.session_id = session_id
        self.busy = busy
        self.preset = preset
        self.inbox = Inbox(emit=self._on_inbox)
        self.inbox_events: list[tuple[str, Any]] = []
        self.followups: list[tuple[str, dict[str, Any], Message]] = []
        self.interrupts: list[dict[str, Any]] = []
        self._counter = 0

    def _on_inbox(self, event_type: str, payload: Any) -> None:
        self.inbox_events.append((event_type, payload))

    def followup(self, text: str, *, source: dict[str, Any] | None = None) -> None:
        self._counter += 1
        payload = dict(source or {"kind": "user"})
        queued = message(text, source=payload, message_id=f"{self.session_id}-m{self._counter}")
        self.inbox.push(TARGET_NEXT_TURN, queued)
        self.followups.append((text, payload, queued))

    def inject(self, text: str, *, source: dict[str, Any] | None = None) -> None:
        self._counter += 1
        payload = dict(source or {"kind": "user"})
        queued = message(text, source=payload, message_id=f"{self.session_id}-s{self._counter}")
        self.inbox.prepend(TARGET_NEXT_STEP, queued)
        self.followups.append((text, payload, queued))

    async def interrupt(self, *, clear_inbox: bool = True) -> None:
        self.interrupts.append({"clear_inbox": clear_inbox})
        if clear_inbox:
            self.inbox.clear()


class FakeAgents:
    """The slice of the agents service the driver uses."""

    def __init__(self) -> None:
        self.agents: list[Any] = []
        self.callbacks: list[Any] = []

    def live_agents(self) -> tuple[Any, ...]:
        return tuple(self.agents)

    def is_live(self, agent: Any) -> bool:
        return agent in self.agents

    def on_created(self, callback: Any) -> Any:
        self.callbacks.append(callback)

        def dispose() -> None:
            if callback in self.callbacks:
                self.callbacks.remove(callback)

        return dispose

    def create(self, agent: Any) -> Any:
        self.agents.append(agent)
        for callback in tuple(self.callbacks):
            callback(agent)
        return agent


class FakeGoals:
    """The slice of the goal service the driver uses; records every mutation."""

    def __init__(self) -> None:
        self.goals: dict[str, GoalSnapshot] = {}
        self.rounds: dict[str, int] = {}
        self.activations: dict[str, str] = {}
        self.blocked: list[dict[str, Any]] = []
        self.paused: list[dict[str, Any]] = []
        self.invalidated: list[str] = []
        self.disarmed: list[str] = []
        self.armed: list[str] = []
        self.state_error: Exception | None = None
        self.block_error: Exception | None = None

    def set_goal(self, session: Any, snapshot: GoalSnapshot, *, rounds_started: int = 0) -> None:
        self.goals[session.session_id] = snapshot
        self.rounds[session.session_id] = rounds_started

    def clear_goal(self, session: Any) -> None:
        self.goals.pop(session.session_id, None)
        self.rounds[session.session_id] = 0

    def state(self, session: Any) -> GoalState:
        if self.state_error is not None:
            raise self.state_error
        return GoalState(
            goal=self.goals.get(session.session_id),
            rounds_started=self.rounds.get(session.session_id, 0),
        )

    def activation(self, session: Any) -> str:
        return self.activations.get(session.session_id, "disarmed")

    def arm(self, session: Any) -> None:
        self.activations[session.session_id] = "armed"
        self.armed.append(session.session_id)

    def disarm(self, session: Any) -> None:
        self.activations[session.session_id] = "disarmed"
        self.disarmed.append(session.session_id)

    def invalidate(self, session: Any) -> None:
        self.invalidated.append(session.session_id)

    async def block(self, session: Any, ref: GoalRef, reason: BlockedReason) -> None:
        if self.block_error is not None:
            error, self.block_error = self.block_error, None
            raise error
        self.blocked.append({"session": session.session_id, "ref": ref, "reason": reason})
        current = self.goals.get(session.session_id)
        if current is not None:
            self.goals[session.session_id] = current.with_phase(
                "blocked",
                revision=current.revision + 1,
                blocked_reason=reason,
            )
        self.activations[session.session_id] = "disarmed"

    async def pause(self, session: Any, ref: GoalRef) -> None:
        self.paused.append({"session": session.session_id, "ref": ref})
        current = self.goals.get(session.session_id)
        if current is not None:
            self.goals[session.session_id] = current.with_phase(
                "paused",
                revision=current.revision + 1,
            )
        self.activations[session.session_id] = "disarmed"

    async def resume(self, session: Any, ref: GoalRef) -> None:
        self.arm(session)


@dataclass
class Harness:
    """A driver wired to real hooks plus the three fakes."""

    hooks: HookService
    agents: FakeAgents
    goals: FakeGoals
    driver: RoundDriver
    states: dict[str, Any] = field(default_factory=dict)

    def scope(self, session_id: str = "s1") -> dict[str, str]:
        return {"session": session_id}

    async def start(
        self,
        agent: FakeAgent,
        *,
        goal: GoalSnapshot | None = None,
        rounds_started: int = 0,
        armed: bool = False,
    ) -> Any:
        """Register one agent, attach the driver and optionally arm its goal."""
        self.agents.create(agent)
        if goal is not None:
            self.goals.set_goal(agent.session, goal, rounds_started=rounds_started)
        self.driver.attach()
        state = self.driver.state_for(agent)
        assert state is not None, "the driver did not adopt the agent"
        self.states[agent.session_id] = state
        await self.settle(agent.session_id)
        if armed:
            self.goals.arm(agent.session)
        return state

    async def settle(self, session_id: str = "s1", *, limit: int = 200) -> None:
        """Wait for the coalesced drive loop to go quiet."""
        state = self.states[session_id]
        for _ in range(limit):
            await asyncio.sleep(0)
            if state.run is None and not state.requested:
                break
        else:  # pragma: no cover - a stuck loop is a bug, not a test detail
            raise AssertionError("the round driver never settled")
        await asyncio.sleep(0.01)

    async def drive(self, session_id: str = "s1") -> None:
        state = self.states[session_id]
        self.driver.request_drive(state)
        await self.settle(session_id)

    async def emit(self, name: str, payload: dict[str, Any], *, session_id: str = "s1") -> None:
        await self.hooks.emit(name, payload, scope=self.scope(session_id))


@pytest.fixture
def harness() -> Harness:
    hooks = HookService()
    agents = FakeAgents()
    goals = FakeGoals()
    driver = RoundDriver(hooks=hooks, agents=agents, goals=goals)
    return Harness(hooks=hooks, agents=agents, goals=goals, driver=driver)
