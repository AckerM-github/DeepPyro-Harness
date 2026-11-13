"""Value types and error codes for the session query service.

The service is a read-only view over the durable session.v4 store: it never
appends to a session log and never mutates a persisted record.  Every failure
carries one of the DSH-compatible codes below so callers (TUI, tools, tests) can
discriminate without parsing messages.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord

#: The requested session id is not present in the store.
SESSION_NOT_FOUND = "SESSION_QUERY_SESSION_NOT_FOUND"
#: The requested event seq is not present in the session log.
EVENT_NOT_FOUND = "SESSION_QUERY_EVENT_NOT_FOUND"
#: The log exists but a line is corrupt or violates the v4 wire contract.
CORRUPT_SESSION = "SESSION_QUERY_CORRUPT_SESSION"
#: The canonical surface fold refused the log (e.g. a bad replace op).
INVALID_SURFACE = "SESSION_QUERY_INVALID_SURFACE"
#: A filter or window parameter is malformed.
INVALID_FILTER = "SESSION_QUERY_INVALID_FILTER"


class SessionQueryError(SessionError):
    """A session query failed with one of the DSH error codes.

    Subclasses :class:`deeppyro.core.errors.SessionError` so existing callers
    that catch session errors keep working, while `code` exposes the precise
    DSH-compatible reason.  The original failure (if any) is chained as the
    exception cause.
    """

    def __init__(self, code: str, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message
        if cause is not None:
            self.__cause__ = cause

    def __reduce__(self) -> tuple[object, tuple[str, str]]:  # pragma: no cover - pickling aid
        return (type(self), (self.code, self.detail))


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Cheap metadata for one persisted session (header plus tail title).

    Produced by :meth:`SessionQuery.list_summaries` without decoding the full
    log, so building a list of every session stays proportional to the number of
    sessions rather than to their total size.
    """

    id: str
    path: pathlib.Path
    cwd: str
    createdAt: int
    title: str | None
    parent: str | None
    origin: str | None
    is_seeded: bool
    delegation_depth: int
    agent_preset: str | None


@dataclass(frozen=True, slots=True)
class EventWindow:
    """One event plus a bounded window of neighbours in seq order."""

    session_id: str
    seq: int
    event: LogRecord
    before: list[LogRecord] = field(default_factory=list)
    after: list[LogRecord] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SessionTrace:
    """Ancestor chain and recursive descendants of one session.

    `ancestors` is ordered root first, immediate parent last; `descendants` is a
    pre-order walk (a session appears before its own children).  The queried
    session itself is in neither list.
    """

    ancestors: list[SessionSummary] = field(default_factory=list)
    descendants: list[SessionSummary] = field(default_factory=list)


__all__ = [
    "CORRUPT_SESSION",
    "EVENT_NOT_FOUND",
    "INVALID_FILTER",
    "INVALID_SURFACE",
    "SESSION_NOT_FOUND",
    "EventWindow",
    "SessionQueryError",
    "SessionSummary",
    "SessionTrace",
]
