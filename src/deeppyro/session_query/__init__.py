"""Read-only queries over durable session v4 logs.

Public surface::

    from deeppyro.session_query import (
        EventFilter,
        EventWindow,
        SessionQuery,
        SessionQueryError,
        SessionSummary,
        SessionTrace,
    )

The service never appends to a session and never mutates a persisted record.
Errors subclass :class:`deeppyro.core.errors.SessionError` and carry a
DSH-compatible `code` attribute.
"""

from __future__ import annotations

from deeppyro.session_query.filters import (
    KNOWN_EVENT_TYPES,
    EventFilter,
    event_matches,
    event_text,
    extract_text,
    normalize_text,
)
from deeppyro.session_query.query import TAIL_SCAN_BYTES, SessionQuery, scan_tail_title
from deeppyro.session_query.types import (
    CORRUPT_SESSION,
    EVENT_NOT_FOUND,
    INVALID_FILTER,
    INVALID_SURFACE,
    SESSION_NOT_FOUND,
    EventWindow,
    SessionQueryError,
    SessionSummary,
    SessionTrace,
)

__all__ = [
    "CORRUPT_SESSION",
    "EVENT_NOT_FOUND",
    "INVALID_FILTER",
    "INVALID_SURFACE",
    "KNOWN_EVENT_TYPES",
    "SESSION_NOT_FOUND",
    "TAIL_SCAN_BYTES",
    "EventFilter",
    "EventWindow",
    "SessionQuery",
    "SessionQueryError",
    "SessionSummary",
    "SessionTrace",
    "event_matches",
    "event_text",
    "extract_text",
    "normalize_text",
    "scan_tail_title",
]
