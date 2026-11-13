"""Public data types for the agent team service."""

from __future__ import annotations

from dataclasses import dataclass

#: A member that still occupies one of the limited team slots.
LIVE_PHASES = ("spawning", "running", "idle")

PHASE_SPAWNING = "spawning"
PHASE_RUNNING = "running"
PHASE_IDLE = "idle"
PHASE_COMPLETED = "completed"
PHASE_FAILED = "failed"
PHASE_CANCELLED = "cancelled"

#: Phases in which wait() returns a snapshot.
SETTLED_PHASES = (PHASE_IDLE, PHASE_COMPLETED, PHASE_FAILED, PHASE_CANCELLED)

TASK_STATUSES = ("pending", "in_progress", "completed")
TASK_ACTIONS = ("claim", "start", "complete", "reopen", "edit")

#: Maximum number of simultaneously live team members.
MAX_MEMBERS = 8


@dataclass(frozen=True, slots=True)
class MemberInfo:
    """Snapshot of one team member (a child agent owned by the lead)."""

    id: str
    name: str
    description: str = ""
    provider: str = ""
    model: str = ""
    preset: str = ""
    phase: str = PHASE_SPAWNING
    session_id: str = ""
    last_reply: str = ""
    error: str | None = None
    created_at: int = 0


@dataclass(frozen=True, slots=True)
class TaskInfo:
    """Snapshot of one item on the shared team task board."""

    id: str
    revision: int = 1
    subject: str = ""
    description: str = ""
    status: str = "pending"
    blocked_by: tuple[str, ...] = ()
    write_scopes: tuple[str, ...] = ()
    owner: str = ""
    created_at: int = 0


__all__ = [
    "LIVE_PHASES",
    "MAX_MEMBERS",
    "PHASE_CANCELLED",
    "PHASE_COMPLETED",
    "PHASE_FAILED",
    "PHASE_IDLE",
    "PHASE_RUNNING",
    "PHASE_SPAWNING",
    "SETTLED_PHASES",
    "TASK_ACTIONS",
    "TASK_STATUSES",
    "MemberInfo",
    "TaskInfo",
]
