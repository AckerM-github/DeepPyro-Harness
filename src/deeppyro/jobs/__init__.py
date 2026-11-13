"""Background jobs: lifecycle registry and job control tools.

Import JobsService from here; the kernel jobs plugin in deeppyro.builtin.jobs
provides it under the service name jobs and registers the job_output,
job_kill and job_list tools.
"""

from __future__ import annotations

from .manager import JobHandle, JobsService
from .types import JOB_STATUSES, TERMINAL_STATUSES, JobResult, JobStatus

__all__ = [
    "JOB_STATUSES",
    "TERMINAL_STATUSES",
    "JobHandle",
    "JobResult",
    "JobStatus",
    "JobsService",
]
