"""Shared vocabulary for the background jobs service.

The service tracks one handle per background job. Output is accumulated as
plain text; a job settles exactly once with a terminal status. The durable
session log stores the lifecycle as job/registered and job/settled records, so
the activity projection and the TUI stay pure readers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

JobStatus = Literal["running", "completed", "failed", "cancelled"]

JOB_STATUSES: tuple[str, ...] = ("running", "completed", "failed", "cancelled")

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class JobResult:
    """Immutable snapshot returned by waiting on a job.

    timed_out is True when the wait deadline expired before settlement; the
    job itself keeps running in that case.
    """

    id: str
    status: str
    exit_code: int | None = None
    output: str = ""
    started_at: int = 0
    finished_at: int | None = None
    timed_out: bool = False
    cause: str = ""
    detail: str = ""


__all__ = ["JOB_STATUSES", "TERMINAL_STATUSES", "JobResult", "JobStatus"]
