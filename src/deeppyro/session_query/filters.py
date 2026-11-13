"""Event filters and literal text extraction for session queries.

DSH's event filters are literal, case-insensitive scans, not full-text search:
arrays are AND-ed, each array is OR-ed, ranges include both endpoints, an empty
type list matches nothing, and malformed values fail with
`SESSION_QUERY_INVALID_FILTER`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from deeppyro.core.wire import LogRecord
from deeppyro.session import KNOWN_EVENT_TYPES, SURFACE_EVENTS, event_message
from deeppyro.session_query.types import INVALID_FILTER, SessionQueryError

#: Event types this reader knows; the same registry the session log validates against.
KNOWN_EVENT_TYPES = frozenset(KNOWN_EVENT_TYPES)


def _invalid(message: str) -> SessionQueryError:
    return SessionQueryError(INVALID_FILTER, message)


@dataclass(frozen=True, slots=True)
class EventFilter:
    """Conjunctive filter over persisted events.

    Fields:
        seq_from: inclusive lower bound on `seq`; `None` disables the bound.
        seq_to: inclusive upper bound on `seq`.
        types: event types to accept (OR inside the list); an empty tuple matches
            nothing; `None` disables the clause.
        surface_only: `True` keeps only model-visible surface events (records
            whose type is in `SURFACE_EVENTS`), `False` keeps only non-surface
            records, `None` disables the clause.
        text: literal, case-insensitive substring; runs of whitespace collapse to
            one space on both sides before matching.  The empty string matches
            everything.
    """

    seq_from: int | None = None
    seq_to: int | None = None
    types: tuple[str, ...] | None = None
    surface_only: bool | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        for name in ("seq_from", "seq_to"):
            value = getattr(self, name)
            if value is not None and not _is_seq(value):
                raise _invalid(f"{name} must be a non-negative integer")
        if self.seq_from is not None and self.seq_to is not None and self.seq_from > self.seq_to:
            raise _invalid("seq_from must be <= seq_to")
        if self.types is not None:
            if isinstance(self.types, str) or not isinstance(self.types, Iterable):
                raise _invalid("types must be a collection of event type strings")
            normalized = tuple(self.types)
            for value in normalized:
                if not isinstance(value, str) or value not in KNOWN_EVENT_TYPES:
                    raise _invalid(f"unknown event type in filter: {value!r}")
            object.__setattr__(self, "types", normalized)
        if self.surface_only is not None and not isinstance(self.surface_only, bool):
            raise _invalid("surface_only must be a boolean")
        if self.text is not None and not isinstance(self.text, str):
            raise _invalid("text must be a string")


def _is_seq(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def normalize_text(value: str) -> str:
    """Collapse whitespace and lowercase for literal, case-insensitive matching."""
    return " ".join(value.split()).lower()


def event_matches(record: LogRecord, event_filter: EventFilter) -> bool:
    """Whether *record* satisfies every clause of *event_filter*."""
    if event_filter.seq_from is not None and record.seq < event_filter.seq_from:
        return False
    if event_filter.seq_to is not None and record.seq > event_filter.seq_to:
        return False
    if event_filter.types is not None and record.type not in event_filter.types:
        return False
    if event_filter.surface_only is not None and event_filter.surface_only != (record.type in SURFACE_EVENTS):
        return False
    if event_filter.text is not None and event_filter.text:
        needle = normalize_text(event_filter.text)
        if needle and needle not in normalize_text(event_text(record)):
            return False
    return True


def event_text(record: LogRecord) -> str:
    """Extract the literal text DSH indexes for one event type.

    Message-bearing events use the model-visible text plus reasoning; tool calls
    contribute name and raw arguments; titles contribute the title; other events
    fall back to every string value in their data payload (keys are never
    indexed), which keeps ad-hoc records searchable without pretending to be
    full-text search.
    """
    if record.type in SURFACE_EVENTS:
        message = event_message(record)
        if message is None:
            return ""
        parts = [message.text(), message.reasoning()]
        return "\n".join(part for part in parts if part)
    if record.type == "tool/call":
        name = record.data.get("name")
        arguments = record.data.get("arguments")
        return f"{name if isinstance(name, str) else ''} {arguments if isinstance(arguments, str) else ''}".strip()
    if record.type == "session/title":
        title = record.data.get("title")
        return title if isinstance(title, str) else ""
    if record.type == "assistant/attempt":
        return "\n".join(_collect_keyed_strings(record.data, {"text", "message"}))
    return "\n".join(_collect_strings(record.data))


def _collect_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int | float):
        return []
    if isinstance(value, list | tuple):
        result: list[str] = []
        for item in value:
            result.extend(_collect_strings(item))
        return result
    if isinstance(value, dict):
        result = []
        for item in value.values():
            result.extend(_collect_strings(item))
        return result
    return []


def _collect_keyed_strings(value: Any, keys: set[str]) -> list[str]:
    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            if key in keys and isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict | list | tuple):
                result.extend(_collect_keyed_strings(item, keys))
        return result
    if isinstance(value, list | tuple):
        result = []
        for item in value:
            result.extend(_collect_keyed_strings(item, keys))
        return result
    return []


def extract_text(record: LogRecord) -> str:
    """Public alias for :func:`event_text` (used by docs and tests)."""
    return event_text(record)


__all__ = [
    "KNOWN_EVENT_TYPES",
    "EventFilter",
    "event_matches",
    "event_text",
    "extract_text",
    "normalize_text",
]
