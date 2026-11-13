"""Types shared by the working-directory service and its tool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Append one durable record and return its sequence number (when available).
RecordFn = Callable[[str, dict[str, Any]], Any]
#: Queue one user-context notice for the next step; failures are non-fatal.
NoticeFn = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class WorkingDirectoryState:
    """Committed working-directory projection for one session.

    The recovered flag is reserved for the future DSH recovery path.  P1 never
    recovers a vanished directory, so it is normally false.
    """

    cwd: Path
    recovered: bool = False


__all__ = ["NoticeFn", "RecordFn", "WorkingDirectoryState"]
