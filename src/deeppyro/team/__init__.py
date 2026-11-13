"""Agent team service: subagent members, messaging and the shared task board."""

from __future__ import annotations

from deeppyro.team.errors import TeamError
from deeppyro.team.manager import TeamService
from deeppyro.team.types import (
    LIVE_PHASES,
    MAX_MEMBERS,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_RUNNING,
    PHASE_SPAWNING,
    SETTLED_PHASES,
    TASK_ACTIONS,
    TASK_STATUSES,
    MemberInfo,
    TaskInfo,
)

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
    "TeamError",
    "TeamService",
]
