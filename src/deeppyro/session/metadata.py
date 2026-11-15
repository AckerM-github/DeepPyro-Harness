"""Session metadata: titles and archive flags live in the log itself.

Both are plain records, so they survive forks, resumes and replays exactly like
every other fact about a session, and the last record wins -- which makes
``/rename`` and ``/archive`` a single append with no rewrite.

``session/title`` is part of DSH's vocabulary. ``session/archived`` is not, so
it is written with envelope ``ignorable: true``: DSH-compatible readers skip it
instead of rejecting the log, and a log written here stays readable there.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

TITLE_EVENT = "session/title"
ARCHIVE_EVENT = "session/archived"
MAX_TITLE_CHARS = 60
PREVIEW_CHARS = 80
EMPTY_TITLE = "(untitled)"
SOURCE_USER = "user"


def collapse(text: object, *, limit: int) -> str:
    """Collapse whitespace and truncate to ``limit`` characters."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + "\u2026"


def derive_title(text: object, *, limit: int = MAX_TITLE_CHARS) -> str | None:
    """A short readable title for one user message, or None when it is empty."""
    title = collapse(text, limit=limit)
    return title or None


def preview_text(text: object, *, limit: int = PREVIEW_CHARS) -> str | None:
    """A one-line preview of the first message in a session."""
    preview = collapse(text, limit=limit)
    return preview or None


def _last_payload(records: Iterable[Any], event: str) -> dict[str, Any] | None:
    for record in reversed(list(records)):
        if getattr(record, "type", None) != event:
            continue
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            return data
    return None


def existing_title(records: Iterable[Any]) -> str | None:
    """Newest non-empty title recorded for a session, if any."""
    data = _last_payload(records, TITLE_EVENT)
    if data is None:
        return None
    title = collapse(data.get("title"), limit=MAX_TITLE_CHARS)
    return title or None


def record_title(session: Any, title: object, *, source: str = SOURCE_USER) -> str:
    """Append one ``session/title`` record and return the stored value."""
    stored = derive_title(title)
    if stored is None:
        raise ValueError("title must not be empty")
    session.append(TITLE_EVENT, {"title": stored, "source": source})
    return stored


def is_archived(records: Iterable[Any]) -> bool:
    """True when the newest ``session/archived`` record says so."""
    data = _last_payload(records, ARCHIVE_EVENT)
    if data is None:
        return False
    return bool(data.get("archived"))


def record_archive(session: Any, archived: bool = True, *, source: str = SOURCE_USER) -> bool:
    """Append one ``session/archived`` record and return the new state."""
    state = bool(archived)
    session.append(
        ARCHIVE_EVENT,
        {"archived": state, "source": source, "at": int(time.time())},
        ignorable=True,
    )
    return state
