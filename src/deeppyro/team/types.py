"""Public data types for the agent team service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from deeppyro.subagent.types import SubagentUsage


class TeamPhase(StrEnum):
    """Lifecycle phase of one team member.

    Native team facts store the phase as one of these values; the PHASE_* string
    constants below remain the v4 recorder vocabulary (same wire strings).
    """

    SPAWNING = "spawning"
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
class MemberRun:
    """One measured member turn and the provider usage it consumed.

    The v4 team folds these runs out of the lead session log (record type
    team/usage, written as an ignorable fact), so per-run usage survives a cold
    resume. A failed or cancelled turn still carries the partial usage that
    reached the provider before it stopped. The native team path keeps the same
    in-memory shape for the live process but never writes usage to a session:
    tokens and latency belong to traces and the UsageSink only.
    """

    run_id: str
    member_id: str
    usage: SubagentUsage
    ok: bool = True
    cancelled: bool = False
    error: str = ""
    created_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "memberId": self.member_id,
            "usage": self.usage.to_dict(),
            "ok": self.ok,
            "cancelled": self.cancelled,
            "error": self.error,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> MemberRun:
        if not isinstance(raw, dict):
            raise ValueError("member run must be a mapping")
        usage = raw.get("usage")
        return cls(
            run_id=_run_text(raw.get("runId") or raw.get("run_id")),
            member_id=_run_text(raw.get("memberId") or raw.get("member_id")),
            usage=SubagentUsage.from_dict(usage) if isinstance(usage, dict) else SubagentUsage(),
            ok=bool(raw.get("ok", True)),
            cancelled=bool(raw.get("cancelled", False)),
            error=_run_text(raw.get("error")),
            created_at=_run_int(raw.get("createdAt") or raw.get("created_at")),
        )


def _run_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _run_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


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

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the snake_case payload stored in plugin.team.task facts."""
        return {
            "id": self.id,
            "revision": self.revision,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "blocked_by": list(self.blocked_by),
            "write_scopes": list(self.write_scopes),
            "owner": self.owner,
            "created_at": self.created_at,
        }


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
    "MemberRun",
    "TaskInfo",
    "TeamPhase",
]
