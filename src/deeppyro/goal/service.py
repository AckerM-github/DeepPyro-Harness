"""One goal per session: snapshot records, revision CAS and activation.

Contract: ``docs/spec/goal.md``. Every mutating verb appends a complete
snapshot (or a clear tombstone) through :meth:`Session.append`, and the
in-memory state is exactly the fold of those records. Activation is process
local in the DSH sense: it is never persisted, it starts ``disarmed`` and a
fresh process (or a replayed session) therefore never auto-arms.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from deeppyro.core.errors import ToolError
from deeppyro.goal import replay as goal_replay
from deeppyro.goal.replay import ClearedChange, GoalReplayError, SnapshotChange
from deeppyro.goal.types import (
    DEFAULT_MAX_GOAL_ROUNDS,
    GOAL_ALREADY_EXISTS,
    GOAL_BUS_EVENT,
    GOAL_EVENT,
    GOAL_INVALID_BLOCK_REASON,
    GOAL_INVALID_EDIT,
    GOAL_INVALID_MAX_ROUNDS,
    GOAL_INVALID_OBJECTIVE,
    GOAL_INVALID_TRANSITION,
    GOAL_NOT_FOUND,
    GOAL_STALE_REVISION,
    Activation,
    BlockedReason,
    GoalError,
    GoalRef,
    GoalSnapshot,
    GoalState,
    GoalView,
)

logger = logging.getLogger(__name__)

RecordFn = Callable[[str, dict[str, Any]], Any]
_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_ID_PREFIX = "goal-"


def _session_id(session: Any) -> str:
    if isinstance(session, str):
        if not session:
            raise ToolError("goal: session id must not be empty")
        return session
    if isinstance(session, Mapping):
        value = session.get("session_id") or session.get("sessionId")
    else:
        value = getattr(session, "session_id", None)
        if value is None:
            value = getattr(session, "sessionId", None)
    if not value:
        raise ToolError("goal: session has no session_id")
    return str(value)


def _records_of(session: Any) -> list[Any]:
    records = getattr(session, "records", None)
    if records is None:
        records = getattr(session, "log", None)
    return list(records or ())


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class GoalService:
    """Per-session goal state; usable without a plugin context."""

    provide: ClassVar[str] = "goal"
    inject: ClassVar[list[str]] = ["sessions"]

    def __init__(self, config: Any = None) -> None:
        self._states: dict[str, GoalState] = {}
        self._activations: dict[str, Activation] = {}
        self._records: dict[str, RecordFn] = {}
        self._emit: Callable[[str, dict[str, Any]], Any] | None = None
        self._loader: Callable[[str], list[Any]] | None = None
        self._pending: set[asyncio.Task[Any]] = set()
        self._clock: Callable[[], int] = lambda: int(time.time() * 1000)
        self._max_chars = self._read_int(config, ("maxChars", "max_chars"), 0)
        self._default_max_rounds = self._read_int(
            config, ("maxGoalRounds", "max_goal_rounds"), DEFAULT_MAX_GOAL_ROUNDS
        )

    @staticmethod
    def _read_int(config: Any, keys: tuple[str, ...], fallback: int) -> int:
        value: Any = None
        for key in keys:
            if isinstance(config, Mapping) and key in config:
                value = config[key]
                break
            if config is not None and hasattr(config, key):
                value = getattr(config, key)
                break
        parsed = _positive_int(value)
        return parsed if parsed is not None else fallback

    # -- host wiring ---------------------------------------------------
    def configure(
        self,
        *,
        emit: Callable[[str, dict[str, Any]], Any] | None = None,
        record: RecordFn | None = None,
        load: Callable[[str], list[Any]] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """Late binding, so the service works before and after the plugin boots."""
        if emit is not None:
            self._emit = emit
        if record is not None:
            self._records[""] = record
        if load is not None:
            self._loader = load
        if clock is not None:
            self._clock = clock

    def bind(self, session: Any, *, record: RecordFn | None = None) -> None:
        session_id = _session_id(session)
        if record is not None:
            self._records[session_id] = record
        elif hasattr(session, "append"):
            self._records[session_id] = session.append

    def unbind(self, session: Any) -> None:
        session_id = _session_id(session)
        self._states.pop(session_id, None)
        self._activations.pop(session_id, None)
        self._records.pop(session_id, None)

    def invalidate(self, session: Any) -> None:
        """Drop the cached fold so the next read replays the log again."""
        self._states.pop(_session_id(session), None)

    # -- reads ---------------------------------------------------------
    def state(self, session: Any) -> GoalState:
        session_id = _session_id(session)
        cached = self._states.get(session_id)
        if cached is not None:
            return cached
        records = self._load_records(session)
        try:
            folded = goal_replay.fold(records, default_max_rounds=self._default_max_rounds)
        except GoalReplayError as exc:
            raise GoalError(str(exc), "GOAL_REPLAY_FAILED") from exc
        self._states[session_id] = folded
        return folded

    def _load_records(self, session: Any) -> list[Any]:
        if isinstance(session, str):
            if self._loader is None:
                return []
            return list(self._loader(session) or ())
        return _records_of(session)

    def view(self, session: Any) -> GoalView | None:
        return goal_replay.view(self.state(session), self.activation(session))

    def goal(self, session: Any) -> GoalSnapshot | None:
        return self.state(session).goal

    def objective(self, session: Any) -> str | None:
        goal = self.state(session).goal
        return goal.objective if goal is not None else None

    def get(self, session: Any) -> str | None:
        """Backwards-compatible string view of the current objective."""
        return self.objective(session)

    def context_text(self, session: Any) -> str:
        goal = self.state(session).goal
        if goal is None:
            return ""
        rounds = self.state(session).rounds_started
        if goal.phase == "active":
            return f"Current goal: {goal.objective}."
        return f"Current goal ({goal.phase}, {rounds}/{goal.max_goal_rounds} rounds): {goal.objective}."

    # -- activation ----------------------------------------------------
    def activation(self, session: Any) -> Activation:
        return self._activations.get(_session_id(session), "disarmed")

    def set_activation(self, session: Any, activation: Activation) -> Activation:
        if activation not in ("armed", "disarmed"):
            raise ValueError(f"unknown activation {activation!r}")
        return self._set_activation(_session_id(session), activation)

    def arm(self, session: Any) -> Activation:
        return self.set_activation(session, "armed")

    def disarm(self, session: Any) -> Activation:
        return self.set_activation(session, "disarmed")

    def _set_activation(self, session_id: str, activation: Activation) -> Activation:
        previous = self._activations.get(session_id, "disarmed")
        self._activations[session_id] = activation
        # M3a/M3b publish ``goal/activation-changed`` here when it really changed.
        return previous

    # -- verbs ---------------------------------------------------------
    async def create(
        self,
        session: Any,
        objective: str,
        *,
        max_goal_rounds: int | None = None,
    ) -> GoalView:
        session_id = self._session_id_for(session)
        state = self.state(session)
        if state.goal is not None and state.goal.phase != "complete":
            raise GoalError(
                f'goal "{state.goal.id}" already exists with phase "{state.goal.phase}"',
                GOAL_ALREADY_EXISTS,
            )
        text = self._objective(objective)
        rounds = self._max_rounds(max_goal_rounds)
        now = self._now()
        goal = GoalSnapshot(
            id=f"{_ID_PREFIX}{uuid.uuid4()}",
            revision=1,
            objective=text,
            phase="active",
            max_goal_rounds=rounds,
        )
        change = SnapshotChange("create", goal, 0, now, now)
        view = self._commit(session, session_id, change, activation="armed")
        assert view is not None
        return view

    async def edit(
        self,
        session: Any,
        ref: GoalRef,
        *,
        objective: str | None = None,
        max_goal_rounds: int | None = None,
    ) -> GoalView:
        session_id = self._session_id_for(session)
        state = self.state(session)
        current = self._require(session, state, ref)
        if objective is None and max_goal_rounds is None:
            raise GoalError("edit must change the objective or maxGoalRounds", GOAL_INVALID_EDIT)
        text = current.objective if objective is None else self._objective(objective)
        rounds = current.max_goal_rounds if max_goal_rounds is None else self._max_rounds(max_goal_rounds)
        goal = GoalSnapshot(
            id=current.id,
            revision=current.revision + 1,
            objective=text,
            phase=current.phase,
            max_goal_rounds=rounds,
            blocked_reason=current.blocked_reason,
        )
        change = SnapshotChange("edit", goal, state.rounds_started, state.created_at, self._next_time(state))
        view = self._commit(session, session_id, change, activation=None)
        assert view is not None
        return view

    async def pause(self, session: Any, ref: GoalRef) -> GoalView:
        return await self._transition(session, ref, "pause", "paused", activation="disarmed")

    async def resume(self, session: Any, ref: GoalRef) -> GoalView:
        state = self.state(session)
        current = self._require(session, state, ref)
        if current.phase == "active" and self.activation(session) == "armed":
            raise GoalError(f'goal "{current.id}" is already active and armed', GOAL_INVALID_TRANSITION)
        if current.phase not in ("active", "paused", "blocked"):
            self._refuse("resume", current)
        if state.rounds_started >= current.max_goal_rounds:
            raise GoalError(
                f"exhausted {state.rounds_started} goal rounds; increase maxGoalRounds before resuming",
                GOAL_INVALID_TRANSITION,
            )
        goal = current.with_phase("active", revision=current.revision + 1, blocked_reason=None)
        change = SnapshotChange(
            "resume", goal, state.rounds_started, state.created_at, self._next_time(state)
        )
        view = self._commit(session, self._session_id_for(session), change, activation="armed")
        assert view is not None
        return view

    async def complete(self, session: Any, ref: GoalRef) -> GoalView:
        return await self._transition(session, ref, "complete", "complete", activation="disarmed")

    async def block(
        self,
        session: Any,
        ref: GoalRef,
        reason: str | BlockedReason,
        *,
        code: str = "model-reported",
    ) -> GoalView:
        blocked = reason if isinstance(reason, BlockedReason) else self._reason(code, reason)
        return await self._transition(session, ref, "block", "blocked", activation="disarmed", reason=blocked)

    async def clear(self, session: Any, ref: GoalRef | None = None) -> None:
        session_id = self._session_id_for(session)
        state = self.state(session)
        current = state.goal
        if current is None:
            raise GoalError("no goal is set for this session", GOAL_NOT_FOUND)
        if ref is not None:
            self._check_ref(current, state, ref)
        cleared_at = self._next_time(state)
        change = ClearedChange(
            cleared=GoalRef(current.id, state.last_revision + 1),
            cleared_at=cleared_at,
        )
        self._commit(session, session_id, change, activation="disarmed")

    async def _transition(
        self,
        session: Any,
        ref: GoalRef,
        operation: str,
        phase: Any,
        *,
        activation: Activation | None,
        reason: BlockedReason | None = None,
    ) -> GoalView:
        session_id = self._session_id_for(session)
        state = self.state(session)
        current = self._require(session, state, ref)
        allowed = {
            "pause": ("active",),
            "complete": ("active", "paused", "blocked"),
            "block": ("active",),
        }[operation]
        if current.phase not in allowed:
            self._refuse(operation, current)
        goal = current.with_phase(phase, revision=current.revision + 1, blocked_reason=reason)
        change = SnapshotChange(
            operation, goal, state.rounds_started, state.created_at, self._next_time(state)
        )
        view = self._commit(session, session_id, change, activation=activation)
        assert view is not None
        return view

    # -- backwards-compatible helpers ---------------------------------
    async def set(self, session: Any, text: str) -> str:
        """Deprecated: overwrite or create the goal, returning the objective."""
        state = self.state(session)
        current = state.goal
        if current is None or current.phase == "complete":
            view = await self.create(session, text)
        else:
            view = await self.edit(session, current.ref, objective=text)
        return view.goal.objective

    # -- internals -----------------------------------------------------
    def _session_id_for(self, session: Any) -> str:
        self.bind(session)
        return _session_id(session)

    def _commit(
        self,
        session: Any,
        session_id: str,
        change: SnapshotChange | ClearedChange,
        *,
        activation: Activation | None,
    ) -> GoalView | None:
        record = self._record_for(session_id)
        record(GOAL_EVENT, change.to_dict())
        current = self._states.get(session_id)
        if current is None:
            current = self.state(session)
        try:
            updated = goal_replay.apply_change(current, change)
        except (GoalError, GoalReplayError) as exc:  # pragma: no cover - guard rail
            raise GoalError(str(exc), "GOAL_REPLAY_FAILED") from exc
        self._states[session_id] = updated
        if activation is not None:
            self._set_activation(session_id, activation)
        self._publish(session_id, updated)
        return goal_replay.view(updated, self.activation(session_id))

    def _require(self, session: Any, state: GoalState, ref: GoalRef) -> GoalSnapshot:
        current = state.goal
        if current is None:
            raise GoalError("no goal is set for this session", GOAL_NOT_FOUND)
        self._check_ref(current, state, ref)
        return current

    @staticmethod
    def _check_ref(current: GoalSnapshot, state: GoalState, ref: GoalRef) -> None:
        if ref.id != current.id or ref.revision != current.revision:
            raise GoalError(
                f'goal "{ref.id}" revision {ref.revision} is stale; '
                f'current is "{current.id}" revision {current.revision}',
                GOAL_STALE_REVISION,
            )
        if state.last_revision != current.revision:  # pragma: no cover - defensive
            raise GoalError("goal state is inconsistent", "GOAL_REPLAY_FAILED")

    @staticmethod
    def _refuse(operation: str, current: GoalSnapshot) -> None:
        allowed = {
            "pause": ("active",),
            "resume": ("active", "paused", "blocked"),
            "complete": ("active", "paused", "blocked"),
            "block": ("active",),
        }[operation]
        expected = " or ".join(f'"{name}"' for name in allowed)
        raise GoalError(
            f'cannot {operation} goal "{current.id}" from phase "{current.phase}"; expected {expected}',
            GOAL_INVALID_TRANSITION,
        )

    def _objective(self, objective: Any) -> str:
        text = " ".join(str(objective).split()) if objective is not None else ""
        if not text:
            raise GoalError("objective must not be empty", GOAL_INVALID_OBJECTIVE)
        if self._max_chars and len(text) > self._max_chars:
            raise GoalError(
                f"objective must be at most {self._max_chars} characters",
                GOAL_INVALID_OBJECTIVE,
            )
        return text

    def _max_rounds(self, value: Any) -> int:
        if value is None:
            return self._default_max_rounds
        parsed = _positive_int(value)
        if parsed is None:
            raise GoalError("maxGoalRounds must be a positive integer", GOAL_INVALID_MAX_ROUNDS)
        return parsed

    @staticmethod
    def _reason(code: str, message: Any) -> BlockedReason:
        text = " ".join(str(message or "").split())
        if not text:
            raise GoalError("blockedReason.message must not be empty", GOAL_INVALID_BLOCK_REASON)
        candidate = str(code or "").strip()
        if not _CODE_PATTERN.match(candidate):
            raise GoalError("blockedReason.code must be lower-kebab-case", GOAL_INVALID_BLOCK_REASON)
        return BlockedReason(code=candidate, message=text)

    def _now(self) -> int:
        try:
            value = self._clock()
        except Exception:  # noqa: BLE001 - a broken clock must not block a goal
            value = int(time.time() * 1000)
        return int(value)

    def _next_time(self, state: GoalState) -> int:
        return max(self._now(), state.updated_at)

    def _record_for(self, session_id: str) -> RecordFn:
        record = self._records.get(session_id) or self._records.get("")
        if record is None:
            raise ToolError("goal: the service is not bound to this session")
        return record

    def _publish(self, session_id: str, state: GoalState) -> None:
        """Announce the change; M3b replaces the payload with the DSH shape."""
        objective = state.goal.objective if state.goal is not None else None
        self._emit_event(GOAL_BUS_EVENT, {"session": session_id, "goal": objective})

    def _emit_event(self, event: str, payload: dict[str, Any]) -> None:
        emitter = self._emit
        if emitter is None:
            return
        try:
            pending = emitter(event, payload)
        except Exception:
            logger.warning("failed to publish %s", event, exc_info=True)
            return
        if not inspect.isawaitable(pending):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(pending)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


__all__ = ["GOAL_BUS_EVENT", "GOAL_EVENT", "GoalService", "GoalState"]
