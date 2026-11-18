"""The native goal round driver: rounds as durable facts, not inbox fences.

Every test drives real :class:~deeppyro.goal.service.GoalService over a real
schema 1 :class:~deeppyro.session.schema.NativeSession; only the agent host is a
double, except for one end-to-end case that runs the real NativeAgentHost.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent.budget import BudgetExceeded, BudgetKind
from deeppyro.agent.native_host import NativeAgentHost
from deeppyro.agent.run_state import RunStatus
from deeppyro.builtin.goal_round_driver import (
    NATIVE_DRIVER_SERVICE,
    NativeGoalRoundDriverPlugin,
)
from deeppyro.core.ids import new_session_id
from deeppyro.core.llm import Finish, ModelRequest, StopReason, StreamEvent, TextDelta, Usage, UsageEvent
from deeppyro.core.messages import GoalSource, MessageSource
from deeppyro.goal.native_driver import NativeRoundDriver, is_native_host
from deeppyro.goal.round_driver import render_goal_round_prompt
from deeppyro.goal.service import GoalService
from deeppyro.goal.types import GOAL_BUS_EVENT, BlockedReason, GoalView
from deeppyro.hooks.service import HookService
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.schema import EventKind, NativeSession, validate_event_data
from tests.service_doubles import service_lookup, service_require

# -- doubles ---------------------------------------------------------------


@dataclass(frozen=True)
class HostCall:
    """One recorded host.send: the round prompt and its durable source."""

    text: str
    source: MessageSource | None


class FakeNativeHost:
    """A NativeAgentHost-shaped double with a scripted list of send outcomes.

    An outcome of None completes the round; a callable is awaited with the host
    (used to hang or to fail); a BaseException is raised inside send.
    """

    def __init__(self, session: NativeSession) -> None:
        self.native_session = session
        self.session_id = session.header.id
        self.busy = False
        self.closed = False
        self.calls: list[HostCall] = []
        self.interrupts = 0
        self.release = asyncio.Event()
        self._outcomes: list[Any] = []

    def queue(self, *outcomes: Any) -> None:
        self._outcomes.extend(outcomes)

    async def send(self, text: str, *, source: MessageSource | None = None) -> Any:
        if self.busy:
            raise RuntimeError("agent host already has a send in flight")
        self.calls.append(HostCall(text, source))
        self.busy = True
        try:
            outcome = self._outcomes.pop(0) if self._outcomes else None
            if outcome is None:
                result: Any = SimpleNamespace(status=RunStatus.COMPLETED)
            elif callable(outcome):
                result = await outcome(self)
                if result is None:
                    result = SimpleNamespace(status=RunStatus.COMPLETED)
            else:
                raise outcome
        finally:
            self.busy = False
        return result

    async def interrupt(self) -> bool:
        self.interrupts += 1
        self.release.set()
        return True

    async def aclose(self) -> None:
        self.closed = True


async def _hang(host: FakeNativeHost) -> Any:
    await host.release.wait()
    raise asyncio.CancelledError


class _FakeAgents:
    """The agents service surface the native plugin needs (native hosts only)."""

    def __init__(self) -> None:
        self.agents: list[Any] = []
        self.callbacks: list[Callable[[Any], None]] = []

    def live_agents(self) -> tuple[Any, ...]:
        return tuple(self.agents)

    def on_created(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        self.callbacks.append(callback)

        def dispose() -> None:
            with contextlib.suppress(ValueError):
                self.callbacks.remove(callback)

        return dispose

    def create(self, agent: Any) -> Any:
        self.agents.append(agent)
        for callback in tuple(self.callbacks):
            callback(agent)
        return agent


class StubContext:
    """The smallest plugin context the native driver plugin needs."""

    def __init__(self, **services: Any) -> None:
        self.services = services
        self.effects: list[tuple[Any, str]] = []
        self.provided: dict[str, Any] = {}

    def get(self, service: str | type[Any]) -> Any:
        return service_lookup(self.services, service)

    def require(self, service: type[Any]) -> Any:
        return service_require(self.services, service)

    def service(self, name: str) -> Any:
        if name in self.services:
            return self.services[name]
        raise KeyError(name)

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        self.effects.append((execute, label))
        return execute

    def provide(self, name: str, service: Any) -> None:
        self.provided[name] = service


class _ScriptedProvider:
    """A provider that replays pre-baked stream responses."""

    name = "scripted"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        for event in item:
            yield event

    async def aclose(self) -> None:
        return None


# -- fixtures and helpers --------------------------------------------------


def _native_session(tmp_path: Path) -> NativeSession:
    path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return NativeSession.create(path, cwd=tmp_path, session_id=path.stem)


def _goal_service(hooks: HookService) -> GoalService:
    service = GoalService(None)
    service.configure(emit=hooks.emit)
    return service


async def _drive(driver: NativeRoundDriver, host: FakeNativeHost) -> None:
    task = driver.request_drive(host)
    assert task is not None
    await task


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met within the timeout")
        await asyncio.sleep(0.005)


def _simple_response(text: str) -> tuple[StreamEvent, ...]:
    return (
        TextDelta(text=text),
        UsageEvent(usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5)),
        Finish(stop_reason=StopReason.STOP),
    )


@dataclass
class NativeHarness:
    """One real session, one real goal service and one fake native host."""

    session: NativeSession
    hooks: HookService
    service: GoalService
    driver: NativeRoundDriver
    host: FakeNativeHost

    async def create_goal(self, **options: Any) -> GoalView:
        return await self.service.create(self.session, "ship it", **options)

    async def drive(self) -> None:
        await _drive(self.driver, self.host)

    def view(self) -> SessionView:
        return fold(self.session.header, self.session.events)

    def facts(self) -> list[Any]:
        return [event for event in self.session.events if event.type == EventKind.GOAL_CHANGED.value]


@pytest.fixture
def native(tmp_path: Path) -> Iterator[NativeHarness]:
    session = _native_session(tmp_path)
    hooks = HookService()
    service = _goal_service(hooks)
    host = FakeNativeHost(session)
    driver = NativeRoundDriver(hooks=hooks, goals=service)
    driver.adopt(host)
    harness = NativeHarness(
        session=session,
        hooks=hooks,
        service=service,
        driver=driver,
        host=host,
    )
    try:
        yield harness
    finally:
        with contextlib.suppress(Exception):
            driver.detach()
        with contextlib.suppress(Exception):
            session.close()


# -- rounds advance only through recorded facts ----------------------------


async def test_multi_round_progress_uses_recorded_rounds(native: NativeHarness) -> None:
    goal = await native.create_goal(max_goal_rounds=5)
    native.host.queue(None, None, asyncio.CancelledError())
    await native.drive()

    assert len(native.host.calls) == 3
    view = native.view()
    assert view.state.rounds_started == 3
    assert view.state.goal is not None
    assert view.state.goal.phase == "active"
    assert view.state.goal.blocked_reason is None
    assert native.service.activation(native.session) == "disarmed"
    assert [call.source for call in native.host.calls] == [
        GoalSource(goal_id=goal.goal.id, revision=1, round=number) for number in (1, 2, 3)
    ]
    assert [call.text for call in native.host.calls] == [
        render_goal_round_prompt(goal.goal, number) for number in (1, 2, 3)
    ]
    # The driver writes no facts itself: create + three admitted rounds.
    assert len(native.facts()) == 4
    assert {event.type for event in native.session.events} == {EventKind.GOAL_CHANGED.value}


async def test_round_limit_blocks_through_a_fact(native: NativeHarness) -> None:
    goal = await native.create_goal(max_goal_rounds=2)
    native.host.queue(None, None)
    await native.drive()

    assert len(native.host.calls) == 2
    assert native.host.calls[0].text == render_goal_round_prompt(goal.goal, 1)
    view = native.view()
    assert view.state.rounds_started == 2
    current = view.state.goal
    assert current is not None
    assert current.phase == "blocked"
    assert current.revision == 2
    assert current.blocked_reason == BlockedReason(
        "round-limit", "Goal reached its configured limit of 2 rounds."
    )
    assert native.service.activation(native.session) == "disarmed"
    facts = native.facts()
    assert len(facts) == 4
    for fact in facts:
        validate_event_data(EventKind.GOAL_CHANGED.value, fact.data, seq=fact.seq)
    assert {event.type for event in native.session.events} == {EventKind.GOAL_CHANGED.value}


async def test_a_failed_round_disarms_without_blocking(native: NativeHarness) -> None:
    await native.create_goal(max_goal_rounds=4)
    native.host.queue(RuntimeError("provider exploded"))
    await native.drive()

    assert len(native.host.calls) == 1
    view = native.view()
    assert view.state.rounds_started == 1
    assert view.state.goal is not None
    assert view.state.goal.phase == "active"
    assert view.state.goal.blocked_reason is None
    assert native.service.activation(native.session) == "disarmed"
    assert len(native.facts()) == 2


@pytest.mark.parametrize(
    ("kind", "limit", "actual", "code", "token"),
    [
        (BudgetKind.STEPS, 2, 3, "max-steps", "max_steps"),
        (BudgetKind.TOKENS, 100, 150, "max-tokens", "max_tokens"),
        (BudgetKind.WALL_SECONDS, 5, 9, "max-wall-seconds", "max_wall_seconds"),
    ],
)
async def test_budget_exhaustion_blocks_the_goal(
    native: NativeHarness,
    kind: BudgetKind,
    limit: int,
    actual: int,
    code: str,
    token: str,
) -> None:
    await native.create_goal(max_goal_rounds=4)
    native.host.queue(BudgetExceeded(kind, limit, actual))
    await native.drive()

    assert len(native.host.calls) == 1
    view = native.view()
    assert view.state.rounds_started == 1
    current = view.state.goal
    assert current is not None
    assert current.phase == "blocked"
    assert current.blocked_reason == BlockedReason(
        code, f"Goal round 1 exceeded its {token} budget (limit {limit})."
    )
    assert native.service.activation(native.session) == "disarmed"
    assert len(native.facts()) == 3


# -- restart, arming and adoption ------------------------------------------


async def test_restart_starts_disarmed_and_resumes_from_the_fold(
    native: NativeHarness,
) -> None:
    goal = await native.create_goal(max_goal_rounds=4)
    assert goal.goal.objective == "ship it"
    # One round, then a user abort: the restart resumes from the record.
    native.host.queue(asyncio.CancelledError())
    await native.drive()
    assert native.view().state.rounds_started == 1

    path = native.session.path
    native.driver.detach()
    native.session.close()

    reopened = NativeSession.open(path)
    hooks = HookService()
    service = _goal_service(hooks)
    host = FakeNativeHost(reopened)
    driver = NativeRoundDriver(hooks=hooks, goals=service)
    driver.adopt(host)
    try:
        # Activation is process local: a restarted session is disarmed.
        assert service.activation(reopened) == "disarmed"
        reopened_view = fold(reopened.header, reopened.events)
        assert reopened_view.state.rounds_started == 1
        goal_now = reopened_view.state.goal
        assert goal_now is not None

        host.queue(None)
        await _drive(driver, host)
        assert host.calls == []

        service.arm(reopened)
        host.queue(None, None, None)
        await _drive(driver, host)
        assert len(host.calls) == 3
        assert host.calls[0].text == render_goal_round_prompt(goal_now, 2)
        assert [call.source for call in host.calls] == [
            GoalSource(goal_id=goal_now.id, revision=goal_now.revision, round=number) for number in (2, 3, 4)
        ]
        final = fold(reopened.header, reopened.events)
        assert final.state.rounds_started == 4
        assert final.state.goal is not None
        assert final.state.goal.phase == "blocked"
        assert final.state.goal.blocked_reason == BlockedReason(
            "round-limit", "Goal reached its configured limit of 4 rounds."
        )
    finally:
        driver.detach()
        with contextlib.suppress(Exception):
            reopened.close()


async def test_adoption_disarms_and_never_starts_a_round(native: NativeHarness) -> None:
    await native.create_goal(max_goal_rounds=3)
    assert native.service.activation(native.session) == "armed"

    driver = NativeRoundDriver(hooks=native.hooks, goals=native.service)
    other = FakeNativeHost(native.session)
    driver.adopt(other)
    try:
        assert native.service.activation(native.session) == "disarmed"
        await _drive(driver, other)
        assert other.calls == []
        assert native.view().state.rounds_started == 0
    finally:
        driver.detach()


async def test_a_busy_host_is_left_to_the_user(native: NativeHarness) -> None:
    await native.create_goal(max_goal_rounds=1)
    native.host.queue(None)
    native.host.busy = True
    await native.drive()
    assert native.host.calls == []

    native.host.busy = False
    await native.drive()
    assert len(native.host.calls) == 1
    view = native.view()
    assert view.state.rounds_started == 1
    assert view.state.goal is not None
    assert view.state.goal.phase == "blocked"


# -- pause, coalescing and detach ------------------------------------------


async def test_pause_interrupts_a_running_round(native: NativeHarness) -> None:
    native.driver.attach()
    native.host.queue(_hang)
    goal = await native.create_goal(max_goal_rounds=4)
    await _wait_until(lambda: len(native.host.calls) == 1)
    state = native.driver.state_for(native.session.header.id)
    assert state is not None
    run = state.run
    assert run is not None

    await native.service.pause(native.session, goal.goal.ref)
    await run

    assert native.host.interrupts == 1
    assert native.service.activation(native.session) == "disarmed"
    view = native.view()
    assert view.state.rounds_started == 1
    assert view.state.goal is not None
    assert view.state.goal.phase == "paused"
    assert len(native.facts()) == 3
    assert [fact for fact in native.facts() if (fact.data.get("goal") or {}).get("phase") == "blocked"] == []


async def test_request_drive_coalesces_on_one_task(native: NativeHarness) -> None:
    native.driver.attach()
    native.host.queue(_hang)
    goal = await native.create_goal(max_goal_rounds=2)
    await _wait_until(lambda: len(native.host.calls) == 1)

    first = native.driver.request_drive(native.host)
    second = native.driver.request_drive(native.host)
    assert first is not None
    assert second is first

    await native.service.pause(native.session, goal.goal.ref)
    await first
    assert native.host.interrupts == 1


async def test_detach_interrupts_a_running_round(native: NativeHarness) -> None:
    native.driver.attach()
    native.host.queue(_hang)
    await native.create_goal(max_goal_rounds=3)
    await _wait_until(lambda: len(native.host.calls) == 1)
    state = native.driver.state_for(native.session.header.id)
    assert state is not None
    run = state.run
    assert run is not None

    native.driver.detach()
    with contextlib.suppress(asyncio.CancelledError):
        await run
    await _wait_until(lambda: native.host.interrupts == 1)
    assert native.service.activation(native.session) == "disarmed"
    assert native.driver.states() == ()


async def test_an_armed_goal_change_starts_a_round(native: NativeHarness) -> None:
    native.driver.attach()
    native.host.queue(None)
    await native.create_goal(max_goal_rounds=1)

    def blocked() -> bool:
        goal = native.view().state.goal
        return goal is not None and goal.phase == "blocked"

    await _wait_until(blocked)
    assert len(native.host.calls) == 1
    assert native.view().state.rounds_started == 1


# -- replay and real-host integration --------------------------------------


async def test_facts_replay_into_a_fresh_service(native: NativeHarness) -> None:
    await native.create_goal(max_goal_rounds=2)
    native.host.queue(None, None)
    await native.drive()
    native.driver.detach()

    replayed = _goal_service(HookService()).state(native.session)
    folded = native.view().state
    assert replayed.goal == folded.goal
    assert replayed.rounds_started == folded.rounds_started == 2
    assert replayed.last_revision == (folded.goal.revision if folded.goal else 0)
    assert replayed.created_at > 0
    assert replayed.updated_at >= replayed.created_at
    assert replayed.seen_ids == frozenset({folded.goal.id}) if folded.goal else set()
    facts = native.facts()
    assert len(facts) == 4
    for fact in facts:
        validate_event_data(EventKind.GOAL_CHANGED.value, fact.data, seq=fact.seq)


async def test_is_native_host_is_duck_typed(native: NativeHarness) -> None:
    assert is_native_host(native.host) is True
    assert is_native_host(object()) is False
    assert is_native_host(None) is False


async def test_real_native_host_runs_two_rounds(tmp_path: Path) -> None:
    session = _native_session(tmp_path)
    hooks = HookService()
    service = _goal_service(hooks)
    provider = _ScriptedProvider([_simple_response("done"), _simple_response("done")])
    host = NativeAgentHost(session=session, provider=provider, model="scripted", cwd=tmp_path)
    driver = NativeRoundDriver(hooks=hooks, goals=service)
    try:
        driver.adopt(host)
        driver.attach()
        goal = await service.create(session, "ship it", max_goal_rounds=2)
        task = driver.request_drive(host)
        assert task is not None
        await task

        view = fold(session.header, session.events)
        assert view.state.rounds_started == 2
        current = view.state.goal
        assert current is not None
        assert current.phase == "blocked"
        assert current.blocked_reason == BlockedReason(
            "round-limit", "Goal reached its configured limit of 2 rounds."
        )
        sends = [event for event in session.events if event.type == EventKind.USER_MESSAGE.value]
        messages = [event.data["message"] for event in sends]
        assert [message["source"]["round"] for message in messages] == [1, 2]
        assert [message["source"]["kind"] for message in messages] == ["goal", "goal"]
        assert any(
            json.dumps(render_goal_round_prompt(goal.goal, number)) in json.dumps(message)
            for number, message in zip((1, 2), messages, strict=True)
        )
        facts = [event for event in session.events if event.type == EventKind.GOAL_CHANGED.value]
        assert len(facts) == 4
        assert len(provider.requests) == 2
    finally:
        driver.detach()
        with contextlib.suppress(Exception):
            await host.aclose()
        with contextlib.suppress(Exception):
            session.close()


# -- plugin wiring ---------------------------------------------------------


def test_native_plugin_attaches_when_enabled(tmp_path: Path) -> None:
    session = _native_session(tmp_path)
    hooks = HookService()
    service = _goal_service(hooks)
    host = FakeNativeHost(session)
    agents = _FakeAgents()
    agents.create(host)
    ctx = StubContext(hooks=hooks, agents=agents, goal=service)
    plugin = NativeGoalRoundDriverPlugin(ctx, {"goal": {"driver": True}})
    try:
        plugin.setup()
        assert plugin.enabled is True
        driver = plugin.driver
        assert driver is not None
        assert driver.attached is True
        assert hooks.listeners(GOAL_BUS_EVENT) != ()
        assert len(ctx.effects) == 1
        assert ctx.provided[NATIVE_DRIVER_SERVICE] is driver
        assert driver.state_for(host.session_id) is not None
        assert service.activation(session) == "disarmed"
    finally:
        for effect, _label in ctx.effects:
            effect()
        with contextlib.suppress(Exception):
            session.close()


def test_native_plugin_is_off_by_default(tmp_path: Path) -> None:
    session = _native_session(tmp_path)
    hooks = HookService()
    service = _goal_service(hooks)
    agents = _FakeAgents()
    ctx = StubContext(hooks=hooks, agents=agents, goal=service)
    plugin = NativeGoalRoundDriverPlugin(ctx, {})
    try:
        plugin.setup()
        assert plugin.enabled is False
        assert plugin.driver is None
        assert ctx.effects == []
        assert hooks.listeners(GOAL_BUS_EVENT) == ()
        assert agents.callbacks == []
        assert ctx.provided == {}
    finally:
        with contextlib.suppress(Exception):
            session.close()


def test_native_plugin_rejects_a_non_boolean_flag(tmp_path: Path) -> None:
    session = _native_session(tmp_path)
    hooks = HookService()
    service = _goal_service(hooks)
    ctx = StubContext(hooks=hooks, agents=_FakeAgents(), goal=service)
    plugin = NativeGoalRoundDriverPlugin(ctx, {"goal": {"driver": "yes"}})
    try:
        with pytest.raises(TypeError, match="driver"):
            plugin.setup()
    finally:
        with contextlib.suppress(Exception):
            session.close()
