"""Native goal round driver: advance goal rounds over a schema 1 session.

Contract: 'docs/native/goal.md'; the v4 driver in
:mod:deeppyro.goal.round_driver is the behaviour reference.

The native runtime has no inbox and no pre-step fence: one
NativeAgentHost owns one durable session and accepts one turn at a time, so
this driver simply awaits host.send and reads every round from recorded facts.
GoalService.admit_round appends the goal.changed fact that advances
rounds_started; this driver never keeps its own round counter, and it appends no
facts itself. The round prompt is the v4 prompt verbatim, sent as a normal user
message whose source is GoalSource, so the round is identifiable in the
transcript and trace.

Activation is process local. A restarted session therefore starts disarmed and
this driver never runs a round until a caller adopts the host, arms the goal and
requests a drive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from deeppyro.agent.budget import BudgetExceeded
from deeppyro.agent.run_state import RunStatus
from deeppyro.core.messages import GoalSource, MessageSource
from deeppyro.goal.round_driver import ROUND_LIMIT_CODE, render_goal_round_prompt
from deeppyro.goal.types import (
    GOAL_BUS_EVENT,
    Activation,
    BlockedReason,
    GoalError,
    GoalRef,
    GoalSnapshot,
    GoalState,
)
from deeppyro.hooks.service import HookService
from deeppyro.session.schema import EventKind

logger = logging.getLogger(__name__)

#: Budget kinds map onto lower-kebab blocked-reason codes.
_BUDGET_CODES = {
    "max_steps": "max-steps",
    "max_tokens": "max-tokens",
}


class NativeRoundHost(Protocol):
    """The host surface the driver drives; NativeAgentHost satisfies it."""

    @property
    def session_id(self) -> str: ...

    @property
    def native_session(self) -> Any: ...

    @property
    def busy(self) -> bool: ...

    async def send(self, text: str, *, source: MessageSource | None = None) -> Any: ...

    async def interrupt(self) -> bool: ...


class GoalFacts(Protocol):
    """The goal service surface the driver reads and mutates."""

    def state(self, session: Any) -> GoalState: ...

    def activation(self, session: Any) -> Activation: ...

    def invalidate(self, session: Any) -> None: ...

    def disarm(self, session: Any) -> Activation: ...

    def admit_round(self, session: Any, ref: GoalRef, round: int) -> GoalSnapshot: ...

    async def block(self, session: Any, ref: GoalRef, reason: BlockedReason) -> Any: ...


@dataclass(slots=True)
class NativeRoundState:
    """Bookkeeping for one adopted host.

    The round number deliberately lives in the session log, not here: this is
    only single-flight/coalescing state and the latest interrupt reason.
    """

    host: NativeRoundHost
    run: asyncio.Task[None] | None = None
    requested: bool = False
    stopping: bool = False
    interrupted_by_goal: bool = False


def is_native_host(candidate: Any) -> bool:
    """Whether an object has the native host surface (hosts are duck typed)."""
    if candidate is None or getattr(candidate, "native_session", None) is None:
        return False
    if not isinstance(getattr(candidate, "session_id", None), str):
        return False
    return callable(getattr(candidate, "send", None)) and callable(getattr(candidate, "interrupt", None))


def _drivable(goal: GoalSnapshot | None, activation: Activation) -> bool:
    """An armed goal in the active phase is the only thing a round may advance."""
    return goal is not None and goal.phase == "active" and activation == "armed"


def _unfinished_run(session: Any) -> bool:
    """Whether the log ends with a run.started that never saw run.finished.

    Resuming that run is a user decision, so the driver leaves it alone.
    """
    open_runs: set[str] = set()
    for event in getattr(session, "events", ()) or ():
        run_id = getattr(event, "run_id", None) or ""
        if event.type == EventKind.RUN_STARTED.value:
            open_runs.add(run_id)
        elif event.type == EventKind.RUN_FINISHED.value:
            open_runs.discard(run_id)
    return bool(open_runs)


class NativeRoundDriver:
    """Drive goal rounds on native hosts; one coalesced asyncio task per host."""

    def __init__(self, *, hooks: HookService, goals: GoalFacts | None = None) -> None:
        self._hooks = hooks
        self._goals = goals
        self._states: dict[str, NativeRoundState] = {}
        self._disposers: list[Callable[[], Any]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._attached = False

    # ------------------------------------------------------------- lifecycle

    @property
    def attached(self) -> bool:
        return self._attached

    def state_for(self, session_id: str) -> NativeRoundState | None:
        """The driver state for one session id, when the host is adopted."""
        return self._states.get(session_id)

    def states(self) -> tuple[NativeRoundState, ...]:
        """Every adopted host state (insertion order)."""
        return tuple(self._states.values())

    def attach(self) -> Callable[[], None]:
        """Listen for goal changes; returns the disposer (also detach())."""
        if self._attached:
            return self.detach
        self._attached = True
        self._disposers.append(self._hooks.on(GOAL_BUS_EVENT, self._on_goal_changed))
        return self.detach

    def detach(self) -> None:
        """Stop listening, interrupt live rounds and disarm every adopted host."""
        self._attached = False
        for state in tuple(self._states.values()):
            self._shutdown(state)
        self._states.clear()
        # Pending interrupt helpers are one-shot: cancelling them here would
        # drop the interrupt on the floor. They are fire-and-forget.
        self._tasks.clear()
        for dispose in self._disposers:
            with contextlib.suppress(Exception):
                dispose()
        self._disposers.clear()

    def adopt(self, host: NativeRoundHost) -> NativeRoundState:
        """Track one host; adoption never arms, so no round starts on its own."""
        if not is_native_host(host):
            raise ValueError("adopt() requires a native agent host")
        state = self._states.get(host.session_id)
        if state is not None:
            if state.host is not host:
                raise ValueError(f"session {host.session_id!r} already has a native host")
            return state
        state = NativeRoundState(host=host)
        self._states[host.session_id] = state
        self._disarm(host)
        return state

    def release(self, host: NativeRoundHost) -> None:
        """Stop driving one host and forget it (used when the host closes)."""
        state = self._states.pop(host.session_id, None)
        if state is not None:
            self._shutdown(state)

    # ---------------------------------------------------------------- driving

    def request_drive(self, host: NativeRoundHost) -> asyncio.Task[None] | None:
        """Coalesce one drive request into the single task for this host."""
        state = self.adopt(host)
        if state.stopping:
            return None
        state.requested = True
        if state.run is not None and not state.run.done():
            return state.run
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(self._drain(state))
        state.run = task
        task.add_done_callback(lambda done: self._retire(state, done))
        return task

    async def drive(self, host: NativeRoundHost) -> None:
        """Run rounds until the goal stops being drivable or the host is busy.

        Every round is admitted as a durable fact; the loop re-reads the goal
        state and rounds_started before each send, so it advances only through
        recorded goal facts.
        """
        state = self._states.get(host.session_id) or self.adopt(host)
        if state.stopping:
            return
        goals = self._goals
        if goals is None:
            logger.warning("native goal round driver: no goal service; cannot drive")
            return
        session = host.native_session
        retried = False
        while not state.stopping:
            if host.busy:
                # A user turn already owns the host; never queue a competing one.
                return
            if _unfinished_run(session):
                logger.debug("native goal round driver: leaving an unfinished run to the caller")
                return
            try:
                goal_state = goals.state(session)
                activation = goals.activation(session)
            except GoalError:
                logger.exception("native goal round driver: the goal state is unreadable")
                self._disarm(host)
                return
            goal = goal_state.goal
            if goal is None or not _drivable(goal, activation):
                return
            started = goal_state.rounds_started
            limit = goal.max_goal_rounds
            if started >= limit:
                await self._block(
                    state,
                    goal.ref,
                    BlockedReason(
                        ROUND_LIMIT_CODE,
                        f"Goal reached its configured limit of {limit} rounds.",
                    ),
                )
                return
            round_number = started + 1
            try:
                pre_round = goals.admit_round(session, goal.ref, round_number)
            except GoalError as exc:
                if retried:
                    logger.warning(
                        "native goal round driver: round %s was not admitted: %s",
                        round_number,
                        exc,
                    )
                    self._disarm(host)
                    return
                retried = True
                goals.invalidate(session)
                continue
            retried = False
            prompt = render_goal_round_prompt(pre_round, round_number)
            source = GoalSource(
                goal_id=pre_round.id,
                revision=pre_round.revision,
                round=round_number,
            )
            state.interrupted_by_goal = False
            try:
                result = await host.send(prompt, source=source)
            except asyncio.CancelledError:
                state.interrupted_by_goal = False
                if state.stopping:
                    raise
                if not self._stopping_reason(state, session):
                    # A plain user abort disarms, matching the v4 driver.
                    self._disarm(host)
                return
            except BudgetExceeded as exc:
                state.interrupted_by_goal = False
                await self._block_budget(state, pre_round.ref, exc, round_number)
                return
            except Exception:
                state.interrupted_by_goal = False
                logger.exception("native goal round driver: round %s failed", round_number)
                self._disarm(host)
                return
            state.interrupted_by_goal = False
            if getattr(result, "status", None) is not RunStatus.COMPLETED:
                logger.warning(
                    "native goal round driver: round %s finished as %r",
                    round_number,
                    getattr(result, "status", None),
                )
                self._disarm(host)
                return
            # Yield so a goal pause/block/complete can land between rounds.
            await asyncio.sleep(0)

    async def _drain(self, state: NativeRoundState) -> None:
        try:
            while state.requested and not state.stopping:
                state.requested = False
                await self._wait_idle(state.host)
                if state.stopping:
                    break
                await self.drive(state.host)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("native goal round driver: drive failed")
            self._disarm(state.host)

    async def _wait_idle(self, host: NativeRoundHost) -> None:
        """Wait for a user-owned turn to finish before starting a goal round."""
        wait = getattr(host, "wait_for_idle", None)
        if not callable(wait):
            return
        waiter = cast("Callable[[], Awaitable[Any]]", wait)
        with contextlib.suppress(Exception):
            await waiter()

    # ------------------------------------------------------------- listeners

    def _on_goal_changed(self, payload: Mapping[str, Any]) -> None:
        """React to durable goal facts: stop a round for non-active phases."""
        session_id = payload.get("session")
        if not isinstance(session_id, str):
            return
        state = self._states.get(session_id)
        if state is None or state.stopping:
            return
        change = payload.get("change")
        goal = change.get("goal") if isinstance(change, Mapping) else None
        phase = goal.get("phase") if isinstance(goal, Mapping) else None
        if phase == "active":
            # A create/edit/resume makes the goal drivable again.
            self.request_drive(state.host)
            return
        if state.host.busy:
            state.interrupted_by_goal = True
            self._spawn(self._interrupt(state.host))

    async def _interrupt(self, host: NativeRoundHost) -> None:
        with contextlib.suppress(Exception):
            await host.interrupt()

    # --------------------------------------------------------------- helpers

    def _stopping_reason(self, state: NativeRoundState, session: Any) -> bool:
        """Whether the cancellation came from a goal change (not a user abort)."""
        if state.interrupted_by_goal:
            return True
        goals = self._goals
        if goals is None:
            return False
        try:
            goal_state = goals.state(session)
            activation = goals.activation(session)
        except GoalError:
            return False
        return not _drivable(goal_state.goal, activation)

    async def _block(self, state: NativeRoundState, ref: GoalRef, reason: BlockedReason) -> None:
        goals = self._goals
        if goals is None:
            return
        session = state.host.native_session
        try:
            current = goals.state(session)
        except GoalError:
            current = None
        if current is not None:
            goal = current.goal
            if goal is None or goal.id != ref.id or goal.revision != ref.revision:
                # The goal moved on; another actor owns the outcome.
                return
            if goal.phase != "active":
                return
        try:
            await goals.block(session, ref, reason)
        except GoalError:
            logger.warning("native goal round driver: could not block goal", exc_info=True)
            self._disarm(state.host)

    async def _block_budget(
        self,
        state: NativeRoundState,
        ref: GoalRef,
        exc: BudgetExceeded,
        round_number: int,
    ) -> None:
        """Map one per-round budget failure onto a schema-valid blocked reason."""
        reason = exc.turn_reason()
        kind = str(reason.get("kind") or "")
        token = kind if kind in _BUDGET_CODES else str(exc.kind.value)
        code = _BUDGET_CODES.get(token, token.replace("_", "-"))
        message = f"Goal round {round_number} exceeded its {token} budget (limit {exc.limit})."
        await self._block(state, ref, BlockedReason(code, message))

    def _shutdown(self, state: NativeRoundState) -> None:
        state.stopping = True
        run = state.run
        if state.host.busy:
            # Let the host cancel its own run, exactly like a user interrupt.
            self._spawn(self._interrupt(state.host))
        elif run is not None and not run.done():
            run.cancel()
        self._disarm(state.host)

    def _disarm(self, host: NativeRoundHost) -> None:
        goals = self._goals
        if goals is None:
            return
        with contextlib.suppress(Exception):
            goals.disarm(host.native_session)

    def _spawn(self, coroutine: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _retire(self, state: NativeRoundState, task: asyncio.Task[None]) -> None:
        if state.run is task:
            state.run = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("native goal round driver: drive task failed", exc_info=error)
        if state.requested and not state.stopping:
            self.request_drive(state.host)


__all__ = [
    "GoalFacts",
    "NativeRoundDriver",
    "NativeRoundHost",
    "NativeRoundState",
    "is_native_host",
]
