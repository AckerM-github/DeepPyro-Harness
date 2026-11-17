"""Task and handoff records for multi-agent work.

A Task is the durable unit of delegated work: a stable id, an objective, a status
that follows a validated state machine, an optional owner, a budget and an
optional parent task. A Handoff is the explicit baton pass between two owners:
who handed off, to whom, a summary, JSON state and the items that are still open.

Both records are plain frozen dataclasses with strict JSON round-trips so they can
be written to session logs or coordinator fixtures without inventing a second
schema. Timestamps are ISO-8601 UTC strings, matching the long-term memory
provenance style.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from deeppyro.core.errors import DeeppyroError

#: Task ids are stable random hex, one namespace per task.
TASK_ID_RE = re.compile(r"^task_[0-9a-f]{16}$")

#: Default per-task step budget when a caller does not provide one.
DEFAULT_MAX_STEPS = 32

#: Hard cap on a task's step budget, so a typo cannot explode a run.
MAX_STEPS_CEILING = 4096


class TaskValidationError(DeeppyroError):
    """A task or handoff record is structurally invalid."""


class TaskStateError(TaskValidationError):
    """A requested task status transition is not allowed."""


class TaskStatus(StrEnum):
    """Lifecycle states of one task.

    PENDING and BLOCKED are not started, IN_PROGRESS is owned and running, and
    COMPLETED, FAILED and CANCELLED are terminal.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Transitions accepted by Task.transition; terminal states have no successors.
TRANSITIONS: dict[TaskStatus, tuple[TaskStatus, ...]] = {
    TaskStatus.PENDING: (TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED),
    TaskStatus.IN_PROGRESS: (
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.BLOCKED: (TaskStatus.IN_PROGRESS, TaskStatus.FAILED, TaskStatus.CANCELLED),
    TaskStatus.COMPLETED: (),
    TaskStatus.FAILED: (),
    TaskStatus.CANCELLED: (),
}

#: Statuses from which no further transition is possible.
TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED})


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def format_timestamp(moment: datetime) -> str:
    """Render one datetime as ISO-8601 UTC with milliseconds and a trailing Z."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: object, subject: str) -> str:
    """Validate one ISO-8601 timestamp string and return it unchanged."""
    if not isinstance(value, str) or not value:
        raise TaskValidationError(f"{subject} must be a non-empty ISO-8601 string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskValidationError(f"{subject} is not ISO-8601: {value!r}") from exc
    return value


def new_task_id() -> str:
    """Return a fresh task id."""
    return "task_" + uuid.uuid4().hex[:16]


def validate_task_id(value: object, subject: str = "task id") -> str:
    """Validate one task id string."""
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
        raise TaskValidationError(f"{subject} must match 'task_<16 hex>'")
    return value


def _optional_text(value: object, subject: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError(f"{subject} must be a non-empty string or null")
    return value


def _required_text(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskValidationError(f"{subject} must be a non-empty string")
    return value


def _positive_int(value: object, subject: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TaskValidationError(f"{subject} must be a positive integer")
    return value


def _optional_positive_int(value: object, subject: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, subject)


def _positive_number(value: object, subject: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise TaskValidationError(f"{subject} must be a positive number")
    return float(value)


def _optional_positive_number(value: object, subject: str) -> float | None:
    if value is None:
        return None
    return _positive_number(value, subject)


def _reject_unknown(raw: dict[str, object], allowed: set[str], subject: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise TaskValidationError(f"{subject} has unknown field {unknown[0]!r}")


@dataclass(frozen=True, slots=True)
class TaskBudget:
    """Hard limits for one task or worker: steps, tokens and wall-clock seconds."""

    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int | None = None
    max_seconds: float | None = None

    def __post_init__(self) -> None:
        steps = _positive_int(self.max_steps, "budget max_steps")
        if steps > MAX_STEPS_CEILING:
            raise TaskValidationError(f"budget max_steps may not exceed {MAX_STEPS_CEILING}")
        _optional_positive_int(self.max_tokens, "budget max_tokens")
        _optional_positive_number(self.max_seconds, "budget max_seconds")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping, omitting unset limits."""
        data: dict[str, object] = {"max_steps": self.max_steps}
        if self.max_tokens is not None:
            data["max_tokens"] = self.max_tokens
        if self.max_seconds is not None:
            data["max_seconds"] = self.max_seconds
        return data

    @classmethod
    def from_dict(cls, raw: object) -> TaskBudget:
        """Parse a budget mapping, refusing unknown fields."""
        if not isinstance(raw, dict):
            raise TaskValidationError("budget must be a JSON object")
        _reject_unknown(raw, {"max_steps", "max_tokens", "max_seconds"}, "budget")
        max_steps = _positive_int(raw.get("max_steps", DEFAULT_MAX_STEPS), "budget max_steps")
        max_tokens = _optional_positive_int(raw.get("max_tokens"), "budget max_tokens")
        max_seconds = _optional_positive_number(raw.get("max_seconds"), "budget max_seconds")
        return cls(max_steps=max_steps, max_tokens=max_tokens, max_seconds=max_seconds)


@dataclass(frozen=True, slots=True)
class Task:
    """One durable task: objective, owner, status, budget, parent and timestamp."""

    id: str
    objective: str
    status: TaskStatus
    budget: TaskBudget
    created_at: str
    owner: str | None = None
    parent: str | None = None

    def __post_init__(self) -> None:
        validate_task_id(self.id)
        _required_text(self.objective, "task objective")
        if not isinstance(self.status, TaskStatus):
            raise TaskValidationError(f"task status must be a TaskStatus, got {self.status!r}")
        if not isinstance(self.budget, TaskBudget):
            raise TaskValidationError("task budget must be a TaskBudget")
        parse_timestamp(self.created_at, "task created_at")
        _optional_text(self.owner, "task owner")
        if self.parent is not None:
            validate_task_id(self.parent, "task parent")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping."""
        return {
            "id": self.id,
            "objective": self.objective,
            "status": self.status.value,
            "owner": self.owner,
            "budget": self.budget.to_dict(),
            "created_at": self.created_at,
            "parent": self.parent,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Task:
        """Parse a task mapping, refusing unknown fields and bad statuses."""
        if not isinstance(raw, dict):
            raise TaskValidationError("task must be a JSON object")
        allowed = {"id", "objective", "status", "owner", "budget", "created_at", "parent"}
        _reject_unknown(raw, allowed, "task")
        for name in ("id", "objective", "status", "budget", "created_at"):
            if name not in raw:
                raise TaskValidationError(f"task lacks required field {name!r}")
        return cls(
            id=validate_task_id(raw["id"]),
            objective=_required_text(raw["objective"], "task objective"),
            status=parse_status(raw["status"]),
            budget=TaskBudget.from_dict(raw["budget"]),
            created_at=parse_timestamp(raw["created_at"], "task created_at"),
            owner=_optional_text(raw.get("owner"), "task owner"),
            parent=None if raw.get("parent") is None else validate_task_id(raw["parent"], "task parent"),
        )

    @property
    def is_terminal(self) -> bool:
        """Whether the task reached a terminal status."""
        return self.status in TERMINAL_STATUSES

    def allowed_transitions(self) -> tuple[TaskStatus, ...]:
        """Return the statuses this task may transition to right now."""
        return TRANSITIONS[self.status]

    def transition(self, status: TaskStatus) -> Task:
        """Return a copy moved to status, or raise TaskStateError if illegal."""
        if not isinstance(status, TaskStatus):
            raise TaskValidationError(f"task status must be a TaskStatus, got {status!r}")
        if status not in TRANSITIONS[self.status]:
            allowed = ", ".join(item.value for item in TRANSITIONS[self.status]) or "none"
            raise TaskStateError(
                f"task {self.id} cannot transition from {self.status.value} to {status.value} "
                f"(allowed: {allowed})"
            )
        return replace(self, status=status)

    def assign(self, owner: str | None) -> Task:
        """Return a copy owned by owner (or unowned when None)."""
        checked = _optional_text(owner, "task owner")
        return replace(self, owner=checked)

    def to_json(self) -> str:
        """Serialize the task to one JSON document."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> Task:
        """Parse one JSON document produced by to_json."""
        return cls.from_dict(json.loads(payload))


def parse_status(value: object) -> TaskStatus:
    """Parse one task status from a string value."""
    if isinstance(value, TaskStatus):
        return value
    if isinstance(value, str):
        try:
            return TaskStatus(value)
        except ValueError as exc:
            raise TaskStateError(f"unknown task status {value!r}") from exc
    raise TaskStateError(f"task status must be a string, got {value!r}")


def new_task(
    objective: str,
    *,
    owner: str | None = None,
    budget: TaskBudget | None = None,
    parent: str | None = None,
    status: TaskStatus = TaskStatus.PENDING,
    created_at: str | None = None,
    task_id: str | None = None,
) -> Task:
    """Create a validated task; a fresh id and current UTC timestamp fill the gaps."""
    timestamp = (
        format_timestamp(utc_now()) if created_at is None else parse_timestamp(created_at, "task created_at")
    )
    return Task(
        id=new_task_id() if task_id is None else validate_task_id(task_id),
        objective=objective,
        status=status,
        budget=budget if budget is not None else TaskBudget(),
        created_at=timestamp,
        owner=owner,
        parent=parent,
    )


@dataclass(frozen=True, slots=True)
class Handoff:
    """An explicit baton pass: from one owner to another, with state and open items."""

    from_owner: str
    to_owner: str
    summary: str
    state: dict[str, object]
    open_items: tuple[str, ...]
    created_at: str
    task_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.from_owner, "handoff from_owner")
        _required_text(self.to_owner, "handoff to_owner")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise TaskValidationError("handoff summary must be a non-empty string")
        if not isinstance(self.state, dict):
            raise TaskValidationError("handoff state must be a JSON object")
        try:
            json.dumps(self.state, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise TaskValidationError(f"handoff state is not JSON-serializable: {exc}") from exc
        if not isinstance(self.open_items, tuple):
            raise TaskValidationError("handoff open_items must be a tuple of strings")
        for item in self.open_items:
            if not isinstance(item, str) or not item.strip():
                raise TaskValidationError("handoff open_items entries must be non-empty strings")
        parse_timestamp(self.created_at, "handoff created_at")
        if self.task_id is not None:
            validate_task_id(self.task_id)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping using the public from/to keys."""
        return {
            "from": self.from_owner,
            "to": self.to_owner,
            "summary": self.summary,
            "state": self.state,
            "open_items": list(self.open_items),
            "created_at": self.created_at,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Handoff:
        """Parse a handoff mapping, refusing unknown fields."""
        if not isinstance(raw, dict):
            raise TaskValidationError("handoff must be a JSON object")
        allowed = {"from", "to", "summary", "state", "open_items", "created_at", "task_id"}
        _reject_unknown(raw, allowed, "handoff")
        for name in ("from", "to", "summary", "state", "open_items", "created_at"):
            if name not in raw:
                raise TaskValidationError(f"handoff lacks required field {name!r}")
        open_items = raw["open_items"]
        if not isinstance(open_items, list):
            raise TaskValidationError("handoff open_items must be a JSON array")
        state = raw["state"]
        if not isinstance(state, dict):
            raise TaskValidationError("handoff state must be a JSON object")
        return cls(
            from_owner=_required_text(raw["from"], "handoff from_owner"),
            to_owner=_required_text(raw["to"], "handoff to_owner"),
            summary=_required_text(raw["summary"], "handoff summary"),
            state=dict(state),
            open_items=tuple(str(item) for item in open_items),
            created_at=parse_timestamp(raw["created_at"], "handoff created_at"),
            task_id=None if raw.get("task_id") is None else validate_task_id(raw["task_id"]),
        )

    def to_json(self) -> str:
        """Serialize the handoff to one JSON document."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> Handoff:
        """Parse one JSON document produced by to_json."""
        return cls.from_dict(json.loads(payload))


def new_handoff(
    from_owner: str,
    to_owner: str,
    summary: str,
    *,
    state: dict[str, object] | None = None,
    open_items: tuple[str, ...] = (),
    task_id: str | None = None,
    created_at: str | None = None,
) -> Handoff:
    """Create a validated handoff, defaulting state to empty and time to now."""
    timestamp = (
        format_timestamp(utc_now())
        if created_at is None
        else parse_timestamp(created_at, "handoff created_at")
    )
    return Handoff(
        from_owner=from_owner,
        to_owner=to_owner,
        summary=summary,
        state=dict(state) if state is not None else {},
        open_items=open_items,
        created_at=timestamp,
        task_id=task_id,
    )


__all__ = [
    "DEFAULT_MAX_STEPS",
    "MAX_STEPS_CEILING",
    "TASK_ID_RE",
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "Handoff",
    "Task",
    "TaskBudget",
    "TaskStateError",
    "TaskStatus",
    "TaskValidationError",
    "format_timestamp",
    "new_handoff",
    "new_task",
    "new_task_id",
    "parse_status",
    "parse_timestamp",
    "utc_now",
    "validate_task_id",
]
