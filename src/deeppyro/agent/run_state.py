"""Run state persisted by the native loop checkpoints (M2 task 3)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from deeppyro.agent.budget import BudgetLimits, BudgetUsage
from deeppyro.core.messages import MESSAGE_ID_RE, MISSING_MESSAGE_ID_RE
from deeppyro.session.schema import (
    CHECKPOINT_STATUSES,
    RUN_ID_RE,
    STEP_ID_RE,
    TRACE_ID_RE,
    TURN_ID_RE,
)


class RunStatus(StrEnum):
    """Lifecycle status of one native run."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _require_identifier(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"run state field {name!r} is not a valid identifier")
    return value


def _require_message_id(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"run state field {name!r} must be a string")
    if MESSAGE_ID_RE.fullmatch(value) is None and MISSING_MESSAGE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"run state field {name!r} is not a valid message id")
    return value


@dataclass(frozen=True, slots=True)
class ToolCallCheckpoint:
    """One dispatched tool call as recorded in a run checkpoint (schema 10.4)."""

    call_id: str
    name: str
    state: str
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id:
            raise ValueError("tool call checkpoint field 'call_id' must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("tool call checkpoint field 'name' must be a non-empty string")
        if self.state not in {"pending", "running", "completed", "failed"}:
            raise ValueError(f"tool call checkpoint field 'state' has invalid value {self.state!r}")
        if self.idempotency_key is not None and (
            not isinstance(self.idempotency_key, str)
            or re.fullmatch(r"idem:[0-9a-f]{64}", self.idempotency_key) is None
        ):
            raise ValueError("tool call checkpoint field 'idempotency_key' is invalid")

    def to_dict(self) -> dict[str, object]:
        """Serialize to the closed checkpoint tool_calls entry."""
        data: dict[str, object] = {
            "call_id": self.call_id,
            "name": self.name,
            "state": self.state,
        }
        if self.idempotency_key is not None:
            data["idempotency_key"] = self.idempotency_key
        return data


@dataclass(frozen=True, slots=True)
class RunState:
    """Resumable state of a single native run."""

    run_id: str
    trace_id: str
    session_id: str
    status: RunStatus
    last_seq: int
    usage: BudgetUsage
    budget: BudgetLimits
    started_at: int
    turn_id: str | None = None
    step_id: str | None = None
    last_message_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, RUN_ID_RE, "run_id")
        _require_identifier(self.trace_id, TRACE_ID_RE, "trace_id")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("run state field 'session_id' must be a non-empty string")
        if not isinstance(self.status, RunStatus):
            raise ValueError("run state field 'status' must be a RunStatus")
        if not isinstance(self.last_seq, int) or isinstance(self.last_seq, bool) or self.last_seq < 0:
            raise ValueError("run state field 'last_seq' must be a non-negative integer")
        if not isinstance(self.usage, BudgetUsage):
            raise ValueError("run state field 'usage' must be a BudgetUsage")
        if not isinstance(self.budget, BudgetLimits):
            raise ValueError("run state field 'budget' must be a BudgetLimits")
        if not isinstance(self.started_at, int) or isinstance(self.started_at, bool) or self.started_at < 0:
            raise ValueError("run state field 'started_at' must be a non-negative integer")
        if self.turn_id is not None:
            _require_identifier(self.turn_id, TURN_ID_RE, "turn_id")
        if self.step_id is not None:
            _require_identifier(self.step_id, STEP_ID_RE, "step_id")
        if self.last_message_id is not None:
            _require_message_id(self.last_message_id, "last_message_id")

    def checkpoint_data(
        self,
        *,
        tool_calls: Iterable[ToolCallCheckpoint] = (),
        approvals: Iterable[Mapping[str, object]] = (),
    ) -> dict[str, object]:
        """Build the run.checkpoint event data for this state."""
        data: dict[str, object] = {
            "last_seq": self.last_seq,
            "status": self.status.value,
            "budgets": self.usage.to_dict(),
            "tool_calls": [call.to_dict() for call in tool_calls],
            "approvals": [dict(approval) for approval in approvals],
        }
        if self.turn_id is not None:
            data["turn_id"] = self.turn_id
        if self.step_id is not None:
            data["step_id"] = self.step_id
        if self.last_message_id is not None:
            data["last_message_id"] = self.last_message_id
        return data

    def to_dict(self) -> dict[str, object]:
        """Serialize the full resumable state (checkpoint file payload section)."""
        return {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "last_message_id": self.last_message_id,
            "status": self.status.value,
            "last_seq": self.last_seq,
            "started_at": self.started_at,
            "usage": self.usage.to_dict(),
            "budget": self.budget.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> RunState:
        """Parse the payload written by :meth:`to_dict`."""
        if not isinstance(raw, Mapping):
            raise ValueError("run state must be an object")
        expected = {
            "run_id",
            "trace_id",
            "session_id",
            "turn_id",
            "step_id",
            "last_message_id",
            "status",
            "last_seq",
            "started_at",
            "usage",
            "budget",
        }
        unknown = set(raw) - expected
        if unknown:
            raise ValueError(f"run state has an unknown field {sorted(unknown)[0]!r}")
        missing = {
            "run_id",
            "trace_id",
            "session_id",
            "status",
            "last_seq",
            "started_at",
            "usage",
            "budget",
        } - set(raw)
        if missing:
            raise ValueError(f"run state is missing field {sorted(missing)[0]!r}")
        status_raw = raw["status"]
        if not isinstance(status_raw, str) or status_raw not in {item.value for item in RunStatus}:
            raise ValueError(f"run state field 'status' has invalid value {status_raw!r}")
        usage_raw = raw["usage"]
        budget_raw = raw["budget"]
        return cls(
            run_id=raw["run_id"],  # type: ignore[arg-type]
            trace_id=raw["trace_id"],  # type: ignore[arg-type]
            session_id=raw["session_id"],  # type: ignore[arg-type]
            status=RunStatus(status_raw),
            last_seq=raw["last_seq"],  # type: ignore[arg-type]
            usage=BudgetUsage.from_dict(usage_raw),
            budget=BudgetLimits.from_dict(budget_raw),
            started_at=raw["started_at"],  # type: ignore[arg-type]
            turn_id=raw.get("turn_id"),  # type: ignore[arg-type]
            step_id=raw.get("step_id"),  # type: ignore[arg-type]
            last_message_id=raw.get("last_message_id"),  # type: ignore[arg-type]
        )


def ensure_checkpoint_status(status: RunStatus) -> str:
    """Return the checkpoint status string for a run status, validating the schema set."""
    value = status.value
    if value not in CHECKPOINT_STATUSES:
        raise ValueError(f"run status {value!r} is not a checkpoint status")
    return value


__all__ = [
    "RunState",
    "RunStatus",
    "ToolCallCheckpoint",
    "ensure_checkpoint_status",
]
