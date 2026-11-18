"""Goal domain types: snapshots, state, views and the error codes.

Mirrors the upstream contract frozen in ``docs/spec/goal.md`` (sections 2-4).
Every persisted change is a full snapshot; the log is the only authority and
``fold`` rebuilds the same state from it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from deeppyro.core.errors import ToolError

#: Durable record type; the value is frozen by the upstream record vocabulary.
GOAL_EVENT = "goal/change"
#: Bus event published after a successful commit (payload shape lands in M3b).
GOAL_BUS_EVENT = "goal/changed"

GOAL_CHANGE_KIND = "goal/change"
GOAL_CHANGE_VERSION = 1
SNAPSHOT_OPERATIONS = ("create", "edit", "pause", "resume", "complete", "block")
PHASES = ("active", "paused", "blocked", "complete")
DEFAULT_MAX_GOAL_ROUNDS = 256

Phase = Literal["active", "paused", "blocked", "complete"]
Activation = Literal["armed", "disarmed"]

# -- error codes (docs/spec/goal.md section 3) -------------------------
GOAL_INVALID_OBJECTIVE = "GOAL_INVALID_OBJECTIVE"
GOAL_INVALID_MAX_ROUNDS = "GOAL_INVALID_MAX_ROUNDS"
GOAL_INVALID_BLOCK_REASON = "GOAL_INVALID_BLOCK_REASON"
GOAL_ALREADY_EXISTS = "GOAL_ALREADY_EXISTS"
GOAL_INVALID_EDIT = "GOAL_INVALID_EDIT"
GOAL_INVALID_TRANSITION = "GOAL_INVALID_TRANSITION"
GOAL_NOT_FOUND = "GOAL_NOT_FOUND"
GOAL_STALE_REVISION = "GOAL_STALE_REVISION"
GOAL_AGENT_NOT_LIVE = "GOAL_AGENT_NOT_LIVE"
#: deeppyro-only: the goal state could not be read from the session log.
GOAL_REPLAY_FAILED = "GOAL_REPLAY_FAILED"


class GoalError(ToolError):
    """A goal operation refused by the contract, carrying its upstream code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BlockedReason:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class GoalRef:
    """A revision-checked reference, used for every mutating verb."""

    id: str
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class GoalSnapshot:
    id: str
    revision: int
    objective: str
    phase: Phase
    max_goal_rounds: int
    blocked_reason: BlockedReason | None = None

    @property
    def ref(self) -> GoalRef:
        return GoalRef(self.id, self.revision)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "revision": self.revision,
            "objective": self.objective,
            "phase": self.phase,
            "maxGoalRounds": self.max_goal_rounds,
        }
        if self.blocked_reason is not None:
            data["blockedReason"] = self.blocked_reason.to_dict()
        return data

    def with_phase(
        self,
        phase: Phase,
        *,
        revision: int,
        blocked_reason: BlockedReason | None = None,
    ) -> GoalSnapshot:
        return replace(
            self,
            phase=phase,
            revision=revision,
            blocked_reason=blocked_reason,
        )


@dataclass(frozen=True, slots=True)
class GoalState:
    """The folded state of one session: at most one goal plus its counters."""

    goal: GoalSnapshot | None = None
    rounds_started: int = 0
    created_at: int = 0
    updated_at: int = 0
    #: revision of the last accepted change (a clear tombstone counts too).
    last_revision: int = 0
    seen_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class GoalView:
    """What tools, commands and the TUI see."""

    goal: GoalSnapshot
    rounds_started: int
    created_at: int
    updated_at: int
    activation: Activation

    @property
    def ref(self) -> GoalRef:
        return self.goal.ref

    def to_dict(self) -> dict[str, Any]:
        data = self.goal.to_dict()
        data.update(
            {
                "roundsStarted": self.rounds_started,
                "createdAt": self.created_at,
                "updatedAt": self.updated_at,
                "activation": self.activation,
            }
        )
        return data


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
    "DEFAULT_MAX_GOAL_ROUNDS",
    "GOAL_AGENT_NOT_LIVE",
    "GOAL_ALREADY_EXISTS",
    "GOAL_BUS_EVENT",
    "GOAL_CHANGE_KIND",
    "GOAL_CHANGE_VERSION",
    "GOAL_EVENT",
    "GOAL_INVALID_BLOCK_REASON",
    "GOAL_INVALID_EDIT",
    "GOAL_INVALID_MAX_ROUNDS",
    "GOAL_INVALID_OBJECTIVE",
    "GOAL_INVALID_TRANSITION",
    "GOAL_NOT_FOUND",
    "GOAL_REPLAY_FAILED",
    "GOAL_STALE_REVISION",
    "PHASES",
    "SNAPSHOT_OPERATIONS",
    "Activation",
    "BlockedReason",
    "ClearedChange",
    "GoalChange",
    "GoalError",
    "GoalRef",
    "GoalSnapshot",
    "GoalState",
    "GoalView",
    "Phase",
    "SnapshotChange",
    "view",
]
