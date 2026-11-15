"""Fold ``goal/change`` records into state, with the DSH contract enforced.

The log is the durable truth: every accepted mutation appends a complete
snapshot (or a clear tombstone), so the state is a pure fold over records.
Anything that does not satisfy ``docs/spec/goal.md`` section 2 is a hard
replay failure rather than a silently wrong goal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from deeppyro.goal.types import (
    DEFAULT_MAX_GOAL_ROUNDS,
    GOAL_CHANGE_KIND,
    GOAL_CHANGE_VERSION,
    GOAL_EVENT,
    PHASES,
    SNAPSHOT_OPERATIONS,
    Activation,
    BlockedReason,
    GoalError,
    GoalRef,
    GoalSnapshot,
    GoalState,
    GoalView,
)

_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SNAPSHOT_KEYS = frozenset(
    {"kind", "version", "operation", "goal", "roundsStarted", "createdAt", "updatedAt"}
)
_CLEARED_KEYS = frozenset({"kind", "version", "operation", "cleared", "clearedAt"})
_GOAL_KEYS = frozenset({"id", "revision", "objective", "phase", "maxGoalRounds"})
_GOAL_BLOCKED_KEYS = _GOAL_KEYS | {"blockedReason"}
_REASON_KEYS = frozenset({"code", "message"})
_REF_KEYS = frozenset({"id", "revision"})

#: Record types that can move ``roundsStarted`` forward.
USER_MESSAGE = "user/message"


class GoalReplayError(Exception):
    """The session log contains a goal record the contract rejects."""


@dataclass(frozen=True, slots=True)
class SnapshotChange:
    operation: str
    goal: GoalSnapshot
    rounds_started: int
    created_at: int
    updated_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": GOAL_CHANGE_KIND,
            "version": GOAL_CHANGE_VERSION,
            "operation": self.operation,
            "goal": self.goal.to_dict(),
            "roundsStarted": self.rounds_started,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ClearedChange:
    cleared: GoalRef
    cleared_at: int
    operation: Literal["clear"] = "clear"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": GOAL_CHANGE_KIND,
            "version": GOAL_CHANGE_VERSION,
            "operation": "clear",
            "cleared": self.cleared.to_dict(),
            "clearedAt": self.cleared_at,
        }


GoalChange = SnapshotChange | ClearedChange


# -- primitive validators ------------------------------------------------
def _fail(where: str, message: str) -> GoalReplayError:
    return GoalReplayError(f"{where}: {message}")


def _keys(value: Mapping[str, Any], expected: frozenset[str], where: str) -> None:
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)}")
        if extra:
            parts.append(f"unexpected {sorted(extra)}")
        raise _fail(where, "keys must be exactly the contract set (" + ", ".join(parts) + ")")


def _safe_int(value: Any, where: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(where, "must be an integer")
    if positive and value <= 0:
        raise _fail(where, "must be positive")
    if not positive and value < 0:
        raise _fail(where, "must not be negative")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _fail(where, "must be a non-empty string without surrounding whitespace")
    return value


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(where, "must be an object")
    return value


def _decode_reason(value: Any, where: str) -> BlockedReason:
    reason = _mapping(value, where)
    _keys(reason, _REASON_KEYS, where)
    code = _text(reason["code"], f"{where}.code")
    if not _CODE_PATTERN.match(code):
        raise _fail(f"{where}.code", "must be lower-kebab-case")
    return BlockedReason(code=code, message=_text(reason["message"], f"{where}.message"))


def _decode_ref(value: Any, where: str) -> GoalRef:
    ref = _mapping(value, where)
    _keys(ref, _REF_KEYS, where)
    return GoalRef(
        id=_text(ref["id"], f"{where}.id"),
        revision=_safe_int(ref["revision"], f"{where}.revision", positive=True),
    )


def _decode_goal(value: Any, where: str) -> GoalSnapshot:
    goal = _mapping(value, where)
    phase = goal.get("phase")
    if phase not in PHASES:
        raise _fail(f"{where}.phase", f"must be one of {list(PHASES)}")
    expected = _GOAL_BLOCKED_KEYS if phase == "blocked" else _GOAL_KEYS
    _keys(goal, expected, where)
    reason = _decode_reason(goal["blockedReason"], f"{where}.blockedReason") if phase == "blocked" else None
    return GoalSnapshot(
        id=_text(goal["id"], f"{where}.id"),
        revision=_safe_int(goal["revision"], f"{where}.revision", positive=True),
        objective=_text(goal["objective"], f"{where}.objective"),
        phase=phase,
        max_goal_rounds=_safe_int(goal["maxGoalRounds"], f"{where}.maxGoalRounds", positive=True),
        blocked_reason=reason,
    )


def decode_change(data: Mapping[str, Any], *, where: str = "goal/change") -> GoalChange:
    """Decode one record body, raising :class:`GoalReplayError` when invalid."""
    body = _mapping(data, where)
    if body.get("kind") != GOAL_CHANGE_KIND:
        raise _fail(where, f"kind must be {GOAL_CHANGE_KIND!r}")
    if body.get("version") != GOAL_CHANGE_VERSION:
        raise _fail(where, f"version must be {GOAL_CHANGE_VERSION}")
    operation = body.get("operation")
    if operation == "clear":
        _keys(body, _CLEARED_KEYS, where)
        return ClearedChange(
            cleared=_decode_ref(body["cleared"], f"{where}.cleared"),
            cleared_at=_safe_int(body["clearedAt"], f"{where}.clearedAt"),
        )
    if operation not in SNAPSHOT_OPERATIONS:
        raise _fail(f"{where}.operation", f"must be one of {list(SNAPSHOT_OPERATIONS)} or 'clear'")
    _keys(body, _SNAPSHOT_KEYS, where)
    change = SnapshotChange(
        operation=operation,
        goal=_decode_goal(body["goal"], f"{where}.goal"),
        rounds_started=_safe_int(body["roundsStarted"], f"{where}.roundsStarted"),
        created_at=_safe_int(body["createdAt"], f"{where}.createdAt"),
        updated_at=_safe_int(body["updatedAt"], f"{where}.updatedAt"),
    )
    if change.updated_at < change.created_at:
        raise _fail(where, "updatedAt must not precede createdAt")
    return change


# -- fold ----------------------------------------------------------------
def _transition_ok(operation: str, current: GoalSnapshot | None) -> None:
    if operation == "create":
        return
    if current is None:
        raise GoalError("no goal is set for this session", "GOAL_NOT_FOUND")
    phase = current.phase
    allowed = {
        "edit": PHASES,
        "pause": ("active",),
        "resume": ("active", "paused", "blocked"),
        "complete": ("active", "paused", "blocked"),
        "block": ("active",),
    }[operation]
    if phase not in allowed:
        expected = " or ".join(f'"{name}"' for name in allowed)
        raise GoalError(
            f'cannot {operation} goal "{current.id}" from phase "{phase}"; expected {expected}',
            "GOAL_INVALID_TRANSITION",
        )


def apply_change(state: GoalState, change: GoalChange, *, where: str = "goal/change") -> GoalState:
    """Return the state after one change, validating every invariant."""
    if isinstance(change, ClearedChange):
        current = state.goal
        if current is None:
            raise GoalError("no goal is set for this session", "GOAL_NOT_FOUND")
        expected = state.last_revision + 1
        if change.cleared.id != current.id:
            raise _fail(where, f"clear names goal {change.cleared.id!r}, current is {current.id!r}")
        if change.cleared.revision != expected:
            raise _fail(where, f"clear revision must be {expected}")
        if change.cleared_at < state.updated_at:
            raise _fail(where, "clearedAt must not precede updatedAt")
        return replace(
            state,
            goal=None,
            rounds_started=0,
            created_at=0,
            updated_at=0,
            last_revision=change.cleared.revision,
            seen_ids=state.seen_ids | {current.id},
        )

    goal = change.goal
    if change.operation == "create":
        if goal.revision != 1:
            raise _fail(where, "create must start at revision 1")
        if goal.phase != "active":
            raise _fail(where, 'create must start in phase "active"')
        if change.rounds_started != 0:
            raise _fail(where, "create must start with roundsStarted 0")
        if goal.id in state.seen_ids:
            raise _fail(where, f"goal id {goal.id!r} was already used in this session")
        if state.goal is not None and state.goal.phase != "complete":
            raise _fail(where, f"cannot create while goal {state.goal.id!r} is {state.goal.phase}")
        return GoalState(
            goal=goal,
            rounds_started=0,
            created_at=change.created_at,
            updated_at=change.updated_at,
            last_revision=goal.revision,
            seen_ids=state.seen_ids | {goal.id},
        )

    _transition_ok(change.operation, state.goal)
    current = state.goal
    assert current is not None  # for type checkers; _transition_ok guarantees it
    expected_revision = current.revision + 1
    if goal.revision != expected_revision:
        raise _fail(where, f"revision must advance to {expected_revision}")
    if change.created_at != state.created_at:
        raise _fail(where, "createdAt is immutable")
    if change.rounds_started != state.rounds_started:
        raise _fail(where, "roundsStarted only moves through goal-round messages")
    if change.updated_at < state.updated_at:
        raise _fail(where, "updatedAt must not go backwards")
    if goal.objective == "":
        raise _fail(where, "objective must not be empty")
    if change.operation == "edit":
        if goal.phase != current.phase:
            raise _fail(where, "edit cannot change the phase")
        if (goal.blocked_reason is None) != (current.blocked_reason is None):
            raise _fail(where, "edit cannot change blockedReason")
    if change.operation != "block" and goal.blocked_reason is not None:
        raise _fail(where, f"only block carries blockedReason (operation {change.operation!r})")
    return replace(
        state,
        goal=goal,
        updated_at=change.updated_at,
        last_revision=goal.revision,
    )


def _round_source(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    source = data.get("source")
    if not isinstance(source, Mapping) or source.get("kind") != "goal":
        return None
    return source


def _admit_round(state: GoalState, source: Mapping[str, Any]) -> GoalState:
    goal = state.goal
    if goal is None or goal.phase != "active":
        return state
    if source.get("goalId") != goal.id or source.get("revision") != goal.revision:
        return state
    round_number = source.get("round")
    if isinstance(round_number, bool) or not isinstance(round_number, int):
        return state
    if round_number != state.rounds_started + 1 or round_number > goal.max_goal_rounds:
        return state
    return replace(state, rounds_started=round_number)


def _legacy_snapshot(
    data: Mapping[str, Any],
    *,
    at: int,
    default_max_rounds: int,
    where: str,
) -> GoalState | None:
    value = data.get("goal")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return GoalState(
        goal=GoalSnapshot(
            id="legacy",
            revision=1,
            objective=value.strip(),
            phase="active",
            max_goal_rounds=default_max_rounds,
        ),
        rounds_started=0,
        created_at=at,
        updated_at=at,
        last_revision=1,
        seen_ids=frozenset({"legacy"}),
    )


def fold(
    records: Iterable[Any],
    *,
    default_max_rounds: int = DEFAULT_MAX_GOAL_ROUNDS,
) -> GoalState:
    """Rebuild the goal state from durable records.

    ``records`` may contain any record type; only ``goal/change`` and
    goal-source ``user/message`` records affect the state.
    """
    state = GoalState()
    for record in records:
        record_type = str(getattr(record, "type", "") or "")
        data = getattr(record, "data", None)
        if not isinstance(data, Mapping):
            continue
        when = getattr(record, "time", 0) or 0
        if record_type == GOAL_EVENT:
            if "kind" not in data:
                legacy = _legacy_snapshot(
                    data,
                    at=int(when) if isinstance(when, int) else 0,
                    default_max_rounds=default_max_rounds,
                    where=GOAL_EVENT,
                )
                if legacy is not None:
                    state = legacy
                else:
                    state = replace(
                        state,
                        goal=None,
                        rounds_started=0,
                        created_at=0,
                        updated_at=0,
                    )
                continue
            try:
                change = decode_change(data)
                state = apply_change(state, change)
            except GoalError as exc:
                raise GoalReplayError(f"goal replay failed: {exc}") from exc
            except GoalReplayError as exc:
                raise GoalReplayError(f"goal replay failed: {exc}") from exc
            continue
        if record_type == USER_MESSAGE:
            source = _round_source(data)
            if source is not None:
                state = _admit_round(state, source)
    return state


def view(state: GoalState, activation: Activation) -> GoalView | None:
    """Project a folded state for tools, commands and the TUI."""
    if state.goal is None:
        return None
    return GoalView(
        goal=state.goal,
        rounds_started=state.rounds_started,
        created_at=state.created_at,
        updated_at=state.updated_at,
        activation=activation,
    )


__all__ = [
    "USER_MESSAGE",
    "ClearedChange",
    "GoalChange",
    "GoalReplayError",
    "SnapshotChange",
    "apply_change",
    "decode_change",
    "fold",
    "view",
]
