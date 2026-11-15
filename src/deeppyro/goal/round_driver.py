"""The goal round driver: keep one armed goal moving round after round.

The driver is plugin-level, not part of the agent loop. It watches one live
``AgentSession`` through the hook bus and the durable session log, and queues a
``user/message`` carrying ``source={"kind": "goal", ...}`` whenever the session
is idle, the goal is active, and the goal is armed.

Invariants (docs/spec/goal.md section 7):

* ``drivable``/``ready_to_drive`` gate every attempt, so a paused, blocked,
  completed, disarmed or busy session is never driven.
* ``drive`` walks the nine steps in order and stops at the first failed gate.
  At most one reservation exists per session and the loop is single flight.
* One reservation is tracked per session (``reserving`` -> ``queued`` ->
  ``claimed`` -> ``admitted``, or ``cancelled``); the pre-step fence validates
  it before the step enters and restores other claimed input when it does not.

Documented divergences from the DSH reference implementation:

* deeppyro has no ``agents.currentInitiator()``: the driver only ever
  interrupts the session it is driving itself, never somebody else's turn.
* deeppyro has no ``subagents.waitForChildren``: a round does not wait for
  child agents, because their records already land in the parent log.
* deeppyro has no ``sessions.flush``: every record is appended (and fsynced) by
  the session store, so the checkpoint step is a no-op.
* deeppyro has no ``agent.status`` property and no ``agent/created`` /
  ``agent/disposed`` bus producers: idleness comes from ``agent.busy`` and the
  live-agent registry of the ``agents`` service supplies created/disposed.
* ``goal/changed`` still carries the legacy payload ``{"session", "goal"}`` and
  ``goal/activation-changed`` is never published, so the driver treats every
  goal change as ``needs_checkpoint`` and notices a disarm by re-reading state.
* ``GoalService.state`` caches its fold, so the driver invalidates the cache
  whenever a user message or a goal change may have moved the round counter.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from deeppyro.agent.inbox import CLAIMED_EVENT, DISCARDED_EVENT, INSERTED_EVENT
from deeppyro.core.wire import Message
from deeppyro.goal.types import GOAL_BUS_EVENT, BlockedReason, GoalRef, GoalSnapshot
from deeppyro.hooks import AGENT_ERROR, AGENT_PRE_STEP, AGENT_STATUS

logger = logging.getLogger(__name__)

#: The ``source.kind`` a goal round message carries.
GOAL_SOURCE_KIND = "goal"

NEXT_STEP = "next-step"
NEXT_TURN = "next-turn"
TURN_END = "turn/end"
USER_MESSAGE = "user/message"

PHASE_IDLE = "idle"
PHASE_RESERVING = "reserving"
PHASE_QUEUED = "queued"
PHASE_CLAIMED = "claimed"
PHASE_ADMITTED = "admitted"
PHASE_CANCELLED = "cancelled"

ROUND_LIMIT_CODE = "round-limit"
QUEUE_FAILED_CODE = "queue-failed"
PROMPT_REJECTED_CODE = "prompt-rejected"


def render_goal_round_prompt(goal: GoalSnapshot, round: int) -> str:
    """The exact ``<goal_round>`` prompt a round queues."""
    objective = json.dumps(goal.objective, ensure_ascii=False)
    return (
        "<goal_round>\n"
        f"Objective: {objective}\n"
        f"Round: {round}/{goal.max_goal_rounds}\n"
        "\n"
        "Continue working toward the objective in this same session. Treat the current "
        "workspace, tool results, and durable session state as authoritative; inspect them "
        "instead of assuming earlier narration is still current. Make concrete progress and "
        "verify the result. Before claiming completion, gather evidence that the whole "
        "objective is achieved, read the current goal, and mark it complete. If work remains, "
        "leave the goal active for the next round. Follow the configured goal-tool policy "
        "before reporting a blocker.\n"
        "</goal_round>"
    )


# -- message helpers ------------------------------------------------------
def _source_of(message: Any) -> Mapping[str, Any]:
    if isinstance(message, Mapping):
        source = message.get("source")
    else:
        source = getattr(message, "source", None)
    return source if isinstance(source, Mapping) else {}


def is_goal_round_source(source: Any) -> bool:
    """Whether source marks a goal round message (``kind`` plus ``round > 0``)."""
    if not isinstance(source, Mapping) or source.get("kind") != GOAL_SOURCE_KIND:
        return False
    number = source.get("round")
    if isinstance(number, bool) or not isinstance(number, int):
        return False
    return number > 0


def _message_id(message: Any) -> str:
    if isinstance(message, Mapping):
        value = message.get("id")
    else:
        value = getattr(message, "id", None)
    return value if isinstance(value, str) else ""


def _message_text(message: Any) -> str:
    if isinstance(message, Mapping):
        content = message.get("content") or ()
        texts = [part.get("text", "") for part in content if isinstance(part, Mapping)]
        return "".join(str(text) for text in texts)
    text = getattr(message, "text", None)
    return str(text()) if callable(text) else ""


def _as_message(value: Any) -> Message | None:
    if isinstance(value, Message):
        return value
    if isinstance(value, Mapping):
        try:
            return Message.model_validate(dict(value))
        except Exception:  # noqa: BLE001 - a malformed dump must not break the fence
            logger.debug("goal round driver: dropping an unusable claimed message")
            return None
    return None


def _same_round(attempt: RoundAttempt, source: Mapping[str, Any]) -> bool:
    return (
        source.get("goalId") == attempt.goal_id
        and source.get("revision") == attempt.revision
        and source.get("round") == attempt.round
    )


def _same_queued(message: Any, attempt: RoundAttempt) -> bool:
    """Whether message is the reservation this attempt queued."""
    if not is_goal_round_source(_source_of(message)):
        return False
    if not _same_round(attempt, _source_of(message)):
        return False
    if attempt.message_id:
        return _message_id(message) == attempt.message_id
    return _message_text(message) == attempt.content


def _is_root_agent(agent: Any) -> bool:
    """Whether agent is a top-level agent (the only kind a driver may drive)."""
    header = getattr(getattr(agent, "session", None), "header", None)
    origin = getattr(header, "origin", None)
    if origin is not None:
        return origin != "subagent"
    return not str(getattr(agent, "preset", "")).startswith("subagent")


def _agent_session_id(agent: Any) -> str:
    value = getattr(agent, "session_id", None)
    if isinstance(value, str) and value:
        return value
    header = getattr(getattr(agent, "session", None), "header", None)
    fallback = getattr(header, "id", None)
    return fallback if isinstance(fallback, str) else ""


def _is_busy(agent: Any) -> bool:
    return bool(getattr(agent, "busy", False))


def _signal_aborted(payload: Mapping[str, Any]) -> bool:
    signal = payload.get("signal")
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _changed_session(payload: Mapping[str, Any]) -> str | None:
    for key in ("session", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    change = payload.get("change")
    if isinstance(change, Mapping):
        for key in ("session", "sessionId"):
            value = change.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _pending_messages(inbox: Any) -> list[Any]:
    messages: list[Any] = []
    for name in (NEXT_STEP, NEXT_TURN):
        messages.extend(getattr(inbox, name, ()) or ())
    return messages


def _pending_ids(agent: Any) -> set[str]:
    inbox = getattr(agent, "inbox", None)
    if inbox is None:
        return set()
    return {_message_id(message) for message in _pending_messages(inbox)}


def _queued_message_id(agent: Any, before: set[str]) -> str:
    """The id of the message ``followup`` just queued (it returns no id)."""
    inbox = getattr(agent, "inbox", None)
    if inbox is None:
        return ""
    for message in reversed(getattr(inbox, NEXT_TURN, ()) or ()):
        identifier = _message_id(message)
        if identifier and identifier not in before:
            return identifier
    return ""


@dataclass(slots=True)
class RoundAttempt:
    """One reservation: the round message the driver queued and its fate."""

    goal_id: str
    revision: int
    round: int
    content: str
    message_id: str = ""
    phase: str = PHASE_QUEUED
    cancelled: bool = False
    stale: bool = False


@dataclass(slots=True)
class DriverState:
    """Everything the driver tracks for one session."""

    agent: Any
    attempt: RoundAttempt | None = None
    competing_queued: bool = False
    needs_checkpoint: bool = False
    requested: bool = False
    run: asyncio.Task[Any] | None = None
    stopping: bool = False
    phase: str = PHASE_IDLE
    last_drivable: bool | None = None
    fold_dirty: bool = True
    goal_state: Any = None
    disposers: list[Callable[[], None]] = field(default_factory=list)


class RoundDriver:
    """Drives armed goals by queueing goal-round prompts on idle sessions."""

    def __init__(self, *, hooks: Any, agents: Any, goals: Any | None = None) -> None:
        self._hooks = hooks
        self._agents = agents
        self._goals = goals
        self._states: dict[str, DriverState] = {}
        self._disposers: list[Callable[[], None]] = []
        self._attached = False

    # -- wiring ------------------------------------------------------------
    def attach(self) -> Callable[[], None]:
        """Observe the bus and every live agent; returns the disposer."""
        if self._attached:
            return self.detach
        self._attached = True
        self._disposers.append(self._hooks.on(GOAL_BUS_EVENT, self._on_goal_changed))
        on_created = getattr(self._agents, "on_created", None)
        if callable(on_created):
            self._disposers.append(on_created(self.adopt))
        for agent in self._live_agents():
            self.adopt(agent)
        return self.detach

    def detach(self) -> None:
        """Stop every listener, cancel every loop and disarm every session."""
        if not self._attached:
            return
        self._attached = False
        for state in list(self._states.values()):
            self._shutdown(state)
        self._states.clear()
        for dispose in self._disposers:
            _safe_dispose(dispose)
        self._disposers.clear()

    def adopt(self, agent: Any) -> DriverState | None:
        """Start tracking one agent, unless it is a subagent or already tracked."""
        session_id = _agent_session_id(agent)
        if not session_id or session_id in self._states or not _is_root_agent(agent):
            return None
        state = DriverState(agent=agent)
        self._states[session_id] = state
        scope = {"session": session_id}
        listeners = (
            (AGENT_STATUS, _handler(self._on_status, state)),
            (AGENT_ERROR, _handler(self._on_agent_error, state)),
            (INSERTED_EVENT, _handler(self._on_inserted, state)),
            (CLAIMED_EVENT, _handler(self._on_claimed, state)),
            (DISCARDED_EVENT, _handler(self._on_discarded, state)),
            (AGENT_PRE_STEP, self._fence_for(state)),
        )
        for name, listener in listeners:
            state.disposers.append(self._hooks.on(name, listener, scope=scope))
        subscribe = getattr(getattr(agent, "session", None), "subscribe", None)
        if callable(subscribe):
            state.disposers.append(subscribe(lambda *args: self._on_record(state, *args)))
        self._disarm(state)
        self.request_drive(state)
        return state

    def state_for(self, agent: Any) -> DriverState | None:
        return self._states.get(_agent_session_id(agent))

    def states(self) -> tuple[DriverState, ...]:
        return tuple(self._states.values())

    def _live_agents(self) -> tuple[Any, ...]:
        live = getattr(self._agents, "live_agents", None)
        if not callable(live):
            return ()
        return tuple(live())

    # -- gates -------------------------------------------------------------
    @staticmethod
    def drivable(goal: GoalSnapshot | None, activation: str) -> bool:
        """Whether a goal may be driven at all: active and armed."""
        return goal is not None and goal.phase == "active" and activation == "armed"

    def ready_to_drive(self, state: DriverState) -> bool:
        """Whether this session can accept a new round right now."""
        if not self._attached or state.stopping or state.competing_queued:
            return False
        if not self._agent_is_live(state.agent):
            return False
        return not _is_busy(state.agent)

    def ready_after_checkpoint(self, state: DriverState) -> bool:
        return not state.needs_checkpoint and self.ready_to_drive(state)

    def _agent_is_live(self, agent: Any) -> bool:
        is_live = getattr(self._agents, "is_live", None)
        if callable(is_live):
            return bool(is_live(agent))
        live = getattr(self._agents, "live_agents", None)
        if callable(live):
            return any(candidate is agent for candidate in live())
        return True

    # -- the drive loop ----------------------------------------------------
    def request_drive(self, state: DriverState) -> None:
        """Ask for one coalesced drive pass (single flight)."""
        if state.stopping:
            return
        state.requested = True
        if state.run is not None and not state.run.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop means nothing to drive
            state.requested = False
            return
        state.run = loop.create_task(self._drain(state))
        state.run.add_done_callback(lambda task: self._retire(state, task))

    async def _drain(self, state: DriverState) -> None:
        while state.requested and not state.stopping:
            state.requested = False
            try:
                await self.drive(state)
            except asyncio.CancelledError:
                raise
            except Exception:  # a driver failure disarms, never raises
                logger.exception("goal round driver: a drive attempt failed")
                self._disarm(state)

    def _retire(self, state: DriverState, task: asyncio.Task[Any]) -> None:
        if state.run is task:
            state.run = None
        if not task.cancelled():
            error = task.exception()
            if error is not None:  # pragma: no cover - _drain already disarmed
                logger.error("goal round driver: the drive loop failed", exc_info=error)
                self._disarm(state)
        if not state.stopping and state.requested:
            self.request_drive(state)

    async def drive(self, state: DriverState) -> None:
        """Run one drive attempt: at most one new round per call."""
        if not self.ready_to_drive(state):
            return
        if state.needs_checkpoint:
            try:
                await self.checkpoint(state)
            except Exception:  # a failed checkpoint disarms
                logger.exception("goal round driver: the checkpoint failed")
                self._disarm(state)
                return
            state.needs_checkpoint = False
            if not self.ready_after_checkpoint(state):
                return
        if state.attempt is not None:
            # A reservation survived the last turn: settle it before retrying.
            state.attempt = None
            state.phase = PHASE_IDLE
            state.needs_checkpoint = True
            state.requested = True
            return
        goal, activation = self.current_goal(state)
        if not self.drivable(goal, activation):
            return
        assert goal is not None
        started = self.rounds_started(state)
        limit = goal.max_goal_rounds
        if started >= limit:
            await self.block(
                state,
                goal.ref,
                BlockedReason(
                    code=ROUND_LIMIT_CODE,
                    message=f"Goal reached its configured limit of {limit} rounds.",
                ),
            )
            return
        round_number = started + 1
        content = render_goal_round_prompt(goal, round_number)
        attempt = RoundAttempt(
            goal_id=goal.id,
            revision=goal.revision,
            round=round_number,
            content=content,
        )
        before = _pending_ids(state.agent)
        state.attempt = attempt
        state.phase = PHASE_RESERVING
        try:
            state.agent.followup(
                content,
                source={
                    "kind": GOAL_SOURCE_KIND,
                    "goalId": goal.id,
                    "revision": goal.revision,
                    "round": round_number,
                },
            )
        except Exception as exc:  # queueing must not kill the loop
            logger.exception("goal round driver: could not queue a round")
            state.attempt = None
            state.phase = PHASE_IDLE
            current, current_activation = self.current_goal(state)
            if (
                current is not None
                and current.id == goal.id
                and current.revision == goal.revision
                and self.drivable(current, current_activation)
            ):
                await self.block(
                    state,
                    goal.ref,
                    BlockedReason(
                        code=QUEUE_FAILED_CODE,
                        message=f"Could not queue goal round {round_number}: {exc}",
                    ),
                )
            return
        attempt.message_id = _queued_message_id(state.agent, before)
        attempt.phase = PHASE_QUEUED
        state.phase = PHASE_QUEUED

    async def checkpoint(self, state: DriverState) -> None:
        """No-op: the session store persists each record as it is appended."""
        return

    # -- the pre-step fence ------------------------------------------------
    def _fence_for(self, state: DriverState) -> Callable[[Any, Any], Any]:
        async def handler(payload: Mapping[str, Any], next_step: Any) -> Any:
            return await self.fence(state, payload, next_step)

        return handler

    async def fence(
        self,
        state: DriverState,
        payload: Mapping[str, Any],
        next_step: Any,
    ) -> Any:
        """Validate (or reject) a claim that is about to enter its step."""
        messages = list(payload.get("messages") or ())
        submitted = next(
            (message for message in messages if is_goal_round_source(_source_of(message))),
            None,
        )
        if submitted is None:
            return await next_step()
        source = dict(_source_of(submitted))
        attempt = state.attempt
        if attempt is not None and attempt.phase == PHASE_QUEUED and _same_queued(submitted, attempt):
            # The claim is the proof: do not wait for the async inbox event.
            attempt.phase = PHASE_CLAIMED
            state.phase = PHASE_CLAIMED
        if not self.valid_reservation(state, submitted, source):
            self._retire_attempt(state, source)
            self.restore_other_claimed(state.agent, messages, _message_id(submitted))
            self.request_drive(state)
            return {"kind": "reject"}
        return await self._fence_decision(state, payload, submitted, source, messages, next_step)

    async def _fence_decision(
        self,
        state: DriverState,
        payload: Mapping[str, Any],
        submitted: Any,
        source: Mapping[str, Any],
        messages: list[Any],
        next_step: Any,
    ) -> Any:
        try:
            decision = await next_step()
        except BaseException:
            if not _signal_aborted(payload):
                self._retire_attempt(state, source)
                self.request_drive(state)
            raise
        mapping = decision if isinstance(decision, Mapping) else None
        kind = mapping.get("kind") if mapping is not None else None
        if _signal_aborted(payload):
            if kind == "enter":
                admitted = mapping.get("messages") if mapping is not None else None
                restore = list(admitted) if isinstance(admitted, list) else messages
                self.restore_other_claimed(state.agent, restore, _message_id(submitted))
            return decision
        if kind == "reject":
            self._retire_attempt(state, source)
            await self._block_rejected(state)
            return decision
        if kind != "enter":
            return decision
        if not self.valid_reservation(state, submitted, source):
            self._retire_attempt(state, source)
            self.restore_other_claimed(state.agent, messages, _message_id(submitted))
            self.request_drive(state)
            return {"kind": "reject"}
        assert mapping is not None
        admitted_decision = dict(mapping)
        admitted_decision["startsRequestSeries"] = True
        return admitted_decision

    def valid_reservation(
        self,
        state: DriverState,
        submitted: Any,
        source: Mapping[str, Any],
    ) -> bool:
        """Whether the claim still matches the live reservation (ten checks)."""
        attempt = state.attempt
        if not self._attached or state.stopping or attempt is None:
            return False
        if attempt.phase not in (PHASE_QUEUED, PHASE_CLAIMED) or attempt.stale:
            return False
        if not _same_queued(submitted, attempt):
            return False
        goal, activation = self.current_goal(state)
        if not self.drivable(goal, activation):
            return False
        assert goal is not None
        if source.get("goalId") != goal.id or source.get("revision") != goal.revision:
            return False
        if source.get("round") != self.rounds_started(state) + 1:
            return False
        return source.get("round") == attempt.round

    def restore_other_claimed(
        self,
        agent: Any,
        messages: Any,
        message_id: str,
    ) -> list[str]:
        """Put the other claimed input back at the front of next-step."""
        inbox = getattr(agent, "inbox", None)
        if inbox is None:
            return []
        pending = {_message_id(message) for message in _pending_messages(inbox)}
        retained = [
            message
            for message in messages or ()
            if _message_id(message) != message_id and not _is_round_zero(message)
        ]
        restored: list[str] = []
        for raw in reversed(retained):
            message = _as_message(raw)
            if message is None or not message.id or message.id in pending:
                continue
            try:
                inbox.prepend(NEXT_STEP, message)
            except Exception:  # restoring must never break the step
                logger.exception("goal round driver: could not restore claimed input")
                continue
            pending.add(message.id)
            restored.append(message.id)
        return restored

    # -- bus handlers ------------------------------------------------------
    async def _on_status(self, state: DriverState, payload: Mapping[str, Any]) -> None:
        if payload.get("status") != "idle":
            return
        state.competing_queued = False
        attempt = state.attempt
        if attempt is None:
            self.request_drive(state)
            return
        goal, activation = self.current_goal(state)
        armed = self.drivable(goal, activation) and goal is not None
        same_goal = goal is not None and goal.id == attempt.goal_id
        awaiting = attempt.phase in (PHASE_QUEUED, PHASE_CLAIMED)
        pause = bool(armed and same_goal and (awaiting or attempt.cancelled))
        if not pause and attempt.phase != PHASE_QUEUED:
            self.request_drive(state)
            return
        queued = attempt.phase == PHASE_QUEUED
        state.attempt = None
        state.phase = PHASE_IDLE
        if queued:
            self._drop_pending(state, attempt)
        if pause and not attempt.cancelled and goal is not None:
            await self.pause_goal(state, goal)
        self.request_drive(state)

    async def _on_agent_error(self, state: DriverState, payload: Mapping[str, Any]) -> None:
        self._disarm(state)

    async def _on_inserted(self, state: DriverState, payload: Mapping[str, Any]) -> None:
        message = payload.get("message")
        if not isinstance(message, Mapping):
            return
        attempt = state.attempt
        if attempt is not None and _same_queued(message, attempt):
            return
        inbox = getattr(state.agent, "inbox", None)
        pending = {_message_id(item) for item in getattr(inbox, NEXT_TURN, ()) or ()}
        identifier = _message_id(message)
        if not identifier or identifier not in pending:
            return
        state.competing_queued = True
        if attempt is not None and attempt.phase == PHASE_QUEUED:
            attempt.stale = True

    async def _on_claimed(self, state: DriverState, payload: Mapping[str, Any]) -> None:
        message = payload.get("message")
        attempt = state.attempt
        if attempt is None or not _same_queued(message, attempt):
            return
        identifier = _message_id(message)
        if identifier and not attempt.message_id:
            attempt.message_id = identifier
        attempt.phase = PHASE_CLAIMED
        state.phase = PHASE_CLAIMED

    async def _on_discarded(self, state: DriverState, payload: Mapping[str, Any]) -> None:
        message = payload.get("message")
        attempt = state.attempt
        if attempt is None or not _same_queued(message, attempt):
            return
        attempt.cancelled = True
        state.phase = PHASE_CANCELLED

    async def _on_goal_changed(self, payload: Mapping[str, Any]) -> None:
        session_id = _changed_session(payload)
        if session_id is None:
            return
        state = self._states.get(session_id)
        if state is None:
            return
        state.needs_checkpoint = True
        state.fold_dirty = True
        goal, activation = self.current_goal(state)
        drivable = self.drivable(goal, activation)
        was_drivable = state.last_drivable
        state.last_drivable = drivable
        if (
            not drivable
            and was_drivable
            and state.attempt is not None
            and _is_busy(state.agent)
            and not state.stopping
        ):
            self._interrupt(state)
        self.request_drive(state)

    def _on_record(self, state: DriverState, record_type: str, data: Any, seq: Any = None) -> None:
        if not isinstance(data, Mapping):
            return
        if record_type == USER_MESSAGE:
            state.fold_dirty = True
            attempt = state.attempt
            if attempt is not None and _same_queued(data, attempt):
                identifier = _message_id(data)
                if identifier and not attempt.message_id:
                    attempt.message_id = identifier
                attempt.phase = PHASE_ADMITTED
                state.phase = PHASE_ADMITTED
            return
        if record_type != TURN_END:
            return
        reason = data.get("reason")
        reason = reason if isinstance(reason, Mapping) else {}
        kind = reason.get("kind")
        if kind == "max-tokens":
            self._disarm(state)
            return
        if kind == "aborted":
            inner = reason.get("reason")
            inner_kind = inner.get("kind") if isinstance(inner, Mapping) else None
            if inner_kind == "disposed":
                self._disarm(state)
                return
            attempt = state.attempt
            if attempt is not None and attempt.phase in (PHASE_CLAIMED, PHASE_ADMITTED):
                attempt.cancelled = True
                state.phase = PHASE_CANCELLED
                return
            self._disarm(state)

    # -- goal state helpers ------------------------------------------------
    def current_goal(self, state: DriverState) -> tuple[GoalSnapshot | None, str]:
        """The session goal and activation, re-folded after a dirty record."""
        goals = self._goals
        if goals is None:
            return None, "disarmed"
        session = state.agent.session
        try:
            if state.fold_dirty:
                goals.invalidate(session)
                state.fold_dirty = False
            state.goal_state = goals.state(session)
            activation = goals.activation(session)
        except Exception:  # an unreadable goal is no goal
            logger.exception("goal round driver: could not read the goal state")
            return None, "disarmed"
        goal = getattr(state.goal_state, "goal", None)
        return goal, activation

    def rounds_started(self, state: DriverState) -> int:
        value = getattr(state.goal_state, "rounds_started", 0)
        return value if isinstance(value, int) else 0

    # -- mutations ---------------------------------------------------------
    async def block(self, state: DriverState, ref: GoalRef, reason: BlockedReason) -> None:
        goals = self._goals
        if goals is None:
            return
        try:
            await goals.block(state.agent.session, ref, reason)
        except Exception:  # a failed block disarms, never raises
            logger.exception("goal round driver: could not block the goal")
            self._disarm(state)
            return
        state.fold_dirty = True

    async def pause_goal(self, state: DriverState, goal: GoalSnapshot) -> None:
        goals = self._goals
        if goals is None or goal.phase != "active":
            return
        try:
            await goals.pause(state.agent.session, goal.ref)
        except Exception:  # a failed pause disarms, never raises
            logger.exception("goal round driver: could not pause the goal")
            self._disarm(state)
            return
        state.fold_dirty = True

    async def _block_rejected(self, state: DriverState) -> None:
        goal, activation = self.current_goal(state)
        if not self.drivable(goal, activation) or goal is None:
            return
        await self.block(
            state,
            goal.ref,
            BlockedReason(
                code=PROMPT_REJECTED_CODE,
                message="Goal round was rejected before entering its step.",
            ),
        )

    def _interrupt(self, state: DriverState) -> None:
        agent = state.agent

        async def cancel() -> None:
            try:
                await agent.interrupt(clear_inbox=True)
            except Exception:  # cancelling is best effort
                logger.exception("goal round driver: could not interrupt the agent")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop, nothing to cancel
            return
        loop.create_task(cancel())

    def _disarm(self, state: DriverState) -> None:
        goals = self._goals
        if goals is None:
            return
        try:
            goals.disarm(state.agent.session)
        except Exception:  # a failed disarm must not break the bus
            logger.exception("goal round driver: could not disarm the goal")

    def _retire_attempt(self, state: DriverState, source: Mapping[str, Any]) -> None:
        attempt = state.attempt
        if attempt is not None and attempt.round == source.get("round"):
            attempt.stale = True
        state.attempt = None
        state.phase = PHASE_IDLE

    def _drop_pending(self, state: DriverState, attempt: RoundAttempt) -> None:
        if not attempt.message_id:
            return
        inbox = getattr(state.agent, "inbox", None)
        remove = getattr(inbox, "remove", None)
        if not callable(remove):
            return
        try:
            remove(attempt.message_id)
        except Exception:  # a failed removal must not break the settle
            logger.exception("goal round driver: could not drop a queued round")

    def _shutdown(self, state: DriverState) -> None:
        state.stopping = True
        attempt = state.attempt
        if attempt is not None:
            attempt.stale = True
        state.requested = False
        if state.run is not None and not state.run.done():
            state.run.cancel()
        state.run = None
        self._disarm(state)
        for dispose in state.disposers:
            _safe_dispose(dispose)
        state.disposers.clear()


def _handler(method: Any, state: DriverState) -> Callable[[Any], Any]:
    async def handle(payload: Mapping[str, Any]) -> None:
        await method(state, payload)

    return handle


def _is_round_zero(message: Any) -> bool:
    source = _source_of(message)
    return source.get("kind") == GOAL_SOURCE_KIND and source.get("round") == 0


def _safe_dispose(dispose: Any) -> None:
    if not callable(dispose):
        return
    try:
        dispose()
    except Exception:  # disposal is best effort
        logger.debug("goal round driver: a disposer failed", exc_info=True)
