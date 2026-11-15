"""Session metadata: titles and archive flags live in the log itself.

Both are plain records, so they survive forks, resumes and replays exactly like
every other fact about a session, and the last record wins -- which makes
``/rename`` and ``/archive`` a single append with no rewrite.

``session/title`` and ``session/title-llm-request`` are part of DSH's
vocabulary. ``session/archived`` is not, so it is written with envelope
``ignorable: true``: DSH-compatible readers skip it instead of rejecting the
log, and a log written here stays readable there.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from typing import Any

from deeppyro.core.wire import Message
from deeppyro.llm.messages import message_text

TITLE_EVENT = "session/title"
ARCHIVE_EVENT = "session/archived"
TITLE_LLM_REQUEST_EVENT = "session/title-llm-request"
MAX_TITLE_CHARS = 24
MAX_STORED_TITLE_CHARS = 120
PREVIEW_CHARS = 80
EMPTY_TITLE = "(untitled)"
SOURCE_USER = "user"
SOURCE_LLM = "llm"
TITLE_PROVIDER_FIRST_PROMPT = "session-title-first-prompt-llm"

#: A leading ``/command`` describes an action, not the session: drop it so the
#: title talks about the request behind the command.
_COMMAND = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]+\s+")
_SENTENCE_ENDS = "\u3002\uff0e\uff01\uff1f!?\n\r"


def collapse(text: object, *, limit: int) -> str:
    """Collapse whitespace and truncate to ``limit`` characters."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + "\u2026"


def strip_command(text: str) -> str:
    """Drop one leading ``/command`` token, keeping the rest of the sentence."""
    match = _COMMAND.match(text)
    return text[match.end() :] if match else text


def first_sentence(text: str) -> str:
    """Everything before the first sentence terminator.

    A ``.`` only ends a sentence at a word boundary, so ``v0.1.0`` survives.
    """
    for index, char in enumerate(text):
        if char in _SENTENCE_ENDS:
            return text[:index]
        if char == "." and (index + 1 == len(text) or text[index + 1].isspace()):
            return text[:index]
    return text


def derive_title(text: object, *, limit: int = MAX_TITLE_CHARS) -> str | None:
    """A short readable title for one user message, or None when it is empty."""
    raw = str(text or "").strip()
    if not raw:
        return None
    title = collapse(first_sentence(strip_command(raw)), limit=limit)
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


def first_user_message(records: Iterable[Any]) -> tuple[int, str] | None:
    """``(seq, text)`` of the first user message that carries text."""
    for record in records:
        if getattr(record, "type", None) != "user/message":
            continue
        try:
            message = Message.model_validate(getattr(record, "data", None))
        except Exception:  # noqa: BLE001 - a damaged record must not hide the session
            message = None
        if message is None:
            continue
        text = message_text(message).strip()
        if text:
            return int(getattr(record, "seq", 0) or 0), text
    return None


def first_user_text(records: Iterable[Any]) -> str | None:
    """The first thing the person said in this session, if it had text."""
    found = first_user_message(records)
    return found[1] if found is not None else None


def existing_title(records: Iterable[Any]) -> str | None:
    """Newest non-empty title recorded for a session, if any."""
    data = _last_payload(records, TITLE_EVENT)
    if data is None:
        return None
    title = collapse(data.get("title"), limit=MAX_STORED_TITLE_CHARS)
    return title or None


def title_source(records: Iterable[Any]) -> str | None:
    """``kind`` of the newest title record (``user``, ``llm``, a plugin name)."""
    data = _last_payload(records, TITLE_EVENT)
    if data is None:
        return None
    source = data.get("source")
    if isinstance(source, dict):
        return str(source.get("kind") or "") or None
    return str(source) if source else None


def _source_value(source: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(source, dict):
        return dict(source)
    return {"kind": str(source or SOURCE_USER)}


def record_title(
    session: Any,
    title: object,
    *,
    source: str | dict[str, Any] = SOURCE_USER,
) -> str:
    """Append one ``session/title`` record and return the stored value."""
    stored = collapse(title, limit=MAX_STORED_TITLE_CHARS)
    if not stored:
        raise ValueError("title must not be empty")
    session.append(TITLE_EVENT, {"title": stored, "source": _source_value(source)})
    return stored


def is_archived(records: Iterable[Any]) -> bool:
    """True when the newest ``session/archived`` record says so."""
    data = _last_payload(records, ARCHIVE_EVENT)
    if data is None:
        return False
    return bool(data.get("archived"))


def record_archive(
    session: Any,
    archived: bool = True,
    *,
    source: str | dict[str, Any] = SOURCE_USER,
) -> bool:
    """Append one ``session/archived`` record and return the new state."""
    state = bool(archived)
    session.append(
        ARCHIVE_EVENT,
        {"archived": state, "source": _source_value(source), "at": int(time.time())},
        ignorable=True,
    )
    return state
