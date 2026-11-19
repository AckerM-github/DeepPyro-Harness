"""Bounded query and metadata writes for native schema 1 sessions.

This module is the read-write counterpart of deeppyro.session.native_index.
It lists, filters, sorts and searches native session logs without attaching to
them, and it writes the two metadata facts the schema 1 vocabulary supports:

* rename -- a core session.title event (source "user" or "llm");
* archive -- the plugin-namespace marker "plugin.session.archived".

The archive marker lives in the plugin event namespace because the schema 1
vocabulary has no archive event kind; it is folded by
native_session_archived in deeppyro.session.native_index, so copies and forks
carry it along and no sidecar file is needed.  An archive record is a fact like
any other: the newest marker wins, and an unarchive marker is another event.

Search is bounded: at most max_sessions rows are inspected and at most
max_events events are decoded per query.  A damaged log is skipped instead of
failing the whole listing, matching the browser in native_index.

The module imports no wire layer and no TUI symbols; the TUI can consume
NativeSessionQueryLike and the dataclasses defined here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from deeppyro.core.ids import now_ms
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.native_index import (
    ARCHIVE_EVENT,
    NativeSessionInfo,
    list_native_sessions,
    read_native_transcript,
    resolve_native_session_path,
)
from deeppyro.session.schema import (
    PLUGIN_EVENT_RE,
    TITLE_SOURCES,
    Event,
    EventKind,
    NativeCodec,
    NativeSession,
    SessionLog,
)

__all__ = [
    "NATIVE_QUERY_CORRUPT",
    "NATIVE_QUERY_INVALID_FILTER",
    "NATIVE_QUERY_INVALID_TITLE",
    "NATIVE_QUERY_LOCKED",
    "NATIVE_QUERY_NOT_FOUND",
    "NativeEventMatch",
    "NativeQueryError",
    "NativeSessionFilter",
    "NativeSessionQuery",
    "NativeSessionQueryLike",
    "NativeSessionSearchHit",
    "NativeSessionSort",
    "archive_native_session",
    "get_native_session",
    "is_native_session_archived",
    "query_native_sessions",
    "read_native_events",
    "rename_native_session",
    "search_native_sessions",
]

#: Error codes raised by this module.
NATIVE_QUERY_NOT_FOUND = "NATIVE_QUERY_NOT_FOUND"
NATIVE_QUERY_CORRUPT = "NATIVE_QUERY_CORRUPT"
NATIVE_QUERY_INVALID_FILTER = "NATIVE_QUERY_INVALID_FILTER"
NATIVE_QUERY_INVALID_TITLE = "NATIVE_QUERY_INVALID_TITLE"
NATIVE_QUERY_LOCKED = "NATIVE_QUERY_LOCKED"

_MATCH_PREVIEW = 160
_MAX_TITLE_CHARS = 120
_MAX_EVENT_MATCHES = 20
_KNOWN_KINDS = frozenset(kind.value for kind in EventKind)
_NATIVE_ROLES = frozenset({"user", "assistant", "tool", "system", "developer"})
_MESSAGE_KINDS = frozenset(
    {
        EventKind.USER_MESSAGE.value,
        EventKind.SYSTEM_MESSAGE.value,
        EventKind.DEVELOPER_MESSAGE.value,
        EventKind.ASSISTANT_MESSAGE.value,
        EventKind.MESSAGE_REPLACED.value,
        EventKind.COMPACTION_DONE.value,
    }
)
_ROLE_BY_KIND = {
    EventKind.USER_MESSAGE.value: "user",
    EventKind.SYSTEM_MESSAGE.value: "system",
    EventKind.DEVELOPER_MESSAGE.value: "developer",
    EventKind.ASSISTANT_MESSAGE.value: "assistant",
    EventKind.TOOL_CALL.value: "tool",
    EventKind.TOOL_RESULT.value: "tool",
}
_FIELD_WEIGHTS = {"id": 5, "title": 5, "preview": 2, "cwd": 2}


class NativeQueryError(ValueError):
    """Raised when a native query or metadata update cannot be satisfied."""

    def __init__(self, code: str, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = {"code": code, "message": message}
        self.cause = cause

    def __reduce__(self) -> tuple[type[NativeQueryError], tuple[str, str]]:
        return (type(self), (self.code, str(self)))


class NativeSessionSort(StrEnum):
    """Ordering used by NativeSessionQuery.list_sessions."""

    UPDATED = "updated"
    CREATED = "created"
    TITLE = "title"
    TURNS = "turns"


@dataclass(frozen=True, slots=True)
class NativeSessionFilter:
    """Row predicate for native session lists and searches.

    Every populated field must match; text and cwd are case-insensitive
    substrings.  Archived sessions are excluded unless archived is set
    explicitly.
    """

    text: str | None = None
    cwd: str | None = None
    parent_id: str | None = None
    origin: str | None = None
    archived: bool | None = None
    min_turns: int = 0
    include_unreadable: bool = True

    def __post_init__(self) -> None:
        for name in ("text", "cwd", "parent_id", "origin"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise _invalid(f"filter field {name!r} must be a non-empty string")
        if self.archived is not None and not isinstance(self.archived, bool):
            raise _invalid("filter field 'archived' must be a bool or None")
        if isinstance(self.min_turns, bool) or not isinstance(self.min_turns, int) or self.min_turns < 0:
            raise _invalid("filter field 'min_turns' must be a non-negative integer")
        if not isinstance(self.include_unreadable, bool):
            raise _invalid("filter field 'include_unreadable' must be a bool")

    def matches(self, info: NativeSessionInfo) -> bool:
        """Return whether info satisfies this predicate."""

        if not self.include_unreadable and not info.readable:
            return False
        if self.archived is not None and info.archived != self.archived:
            return False
        if self.origin is not None and info.origin != self.origin:
            return False
        if self.parent_id is not None and info.parent_id != self.parent_id:
            return False
        if info.turns < self.min_turns:
            return False
        if self.cwd is not None and self.cwd.casefold() not in info.cwd.casefold():
            return False
        return self.text is None or self.text.casefold() in _haystack(info)


@dataclass(frozen=True, slots=True)
class NativeEventMatch:
    """One native event matched by a query, reduced to display facts."""

    seq: int
    kind: str
    role: str | None = None
    preview: str = ""
    created_at: int = 0


@dataclass(frozen=True, slots=True)
class NativeSessionSearchHit:
    """A session whose row or events matched a search needle."""

    info: NativeSessionInfo
    matched: tuple[str, ...] = ()
    events: tuple[NativeEventMatch, ...] = ()
    score: int = 0


@runtime_checkable
class NativeSessionQueryLike(Protocol):
    """Read surface the TUI may depend on without importing v4 or wire symbols."""

    def list_sessions(
        self,
        *,
        filter: NativeSessionFilter | None = None,
        sort: NativeSessionSort = NativeSessionSort.UPDATED,
        limit: int = 50,
        offset: int = 0,
    ) -> list[NativeSessionInfo]: ...

    def get(self, session_id: str) -> NativeSessionInfo: ...

    def search(
        self,
        text: str,
        *,
        filter: NativeSessionFilter | None = None,
        role: str | None = None,
        limit: int = 20,
    ) -> list[NativeSessionSearchHit]: ...

    def read_events(
        self,
        session_id: str,
        *,
        kinds: Iterable[EventKind | str] | EventKind | str | None = None,
        role: str | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> tuple[NativeEventMatch, ...]: ...


class NativeSessionQuery:
    """Bounded native session listing, filtering, search and metadata writes."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_sessions: int = 200,
        max_events: int = 5000,
    ) -> None:
        if isinstance(max_sessions, bool) or not isinstance(max_sessions, int) or max_sessions <= 0:
            raise _invalid("max_sessions must be a positive integer")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
            raise _invalid("max_events must be a positive integer")
        self.root = Path(root) if root is not None else None
        self.max_sessions = max_sessions
        self.max_events = max_events

    def list_sessions(
        self,
        *,
        filter: NativeSessionFilter | None = None,
        sort: NativeSessionSort = NativeSessionSort.UPDATED,
        limit: int = 50,
        offset: int = 0,
    ) -> list[NativeSessionInfo]:
        """Return native rows filtered, sorted and sliced in memory."""

        _validate_limit(limit)
        _validate_offset(offset)
        if not isinstance(sort, NativeSessionSort):
            raise _invalid(f"sort must be a NativeSessionSort, got {sort!r}")
        return _sorted_sessions(self._rows(filter), sort)[offset : offset + limit]

    def get(self, session_id: str) -> NativeSessionInfo:
        """Return one row, including archived sessions, or raise NOT_FOUND."""

        _validate_session_id(session_id)
        for info in list_native_sessions(self.root, include_archived=True):
            if info.id == session_id:
                return info
        path = _session_path(session_id, self.root)
        try:
            return read_native_transcript(session_id, self.root).info
        except FileNotFoundError as exc:
            raise _not_found(session_id, cause=exc) from exc
        except Exception as exc:
            raise NativeQueryError(
                NATIVE_QUERY_CORRUPT,
                f"native session {session_id} cannot be read from {path}: {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc

    def search(
        self,
        text: str,
        *,
        filter: NativeSessionFilter | None = None,
        role: str | None = None,
        limit: int = 20,
    ) -> list[NativeSessionSearchHit]:
        """Search rows and events for text within the configured bounds."""

        needle = _normalize_search_text(text)
        role_name = _normalize_role(role)
        _validate_limit(limit)
        hits: list[NativeSessionSearchHit] = []
        budget = self.max_events
        for info in self._rows(filter):
            if len(hits) >= limit:
                break
            matched = _match_fields(info, needle) if role_name is None else ()
            events: list[NativeEventMatch] = []
            if budget > 0:
                log = _try_read(info.path)
                if log is not None:
                    for event in log.events:
                        if budget <= 0 or len(events) >= _MAX_EVENT_MATCHES:
                            break
                        budget -= 1
                        match = _event_match(event, needle=needle, role=role_name)
                        if match is not None:
                            events.append(match)
            if not matched and not events:
                continue
            score = sum(_FIELD_WEIGHTS.get(field, 1) for field in matched) + len(events)
            hits.append(NativeSessionSearchHit(info=info, matched=matched, events=tuple(events), score=score))
        hits.sort(key=lambda hit: (-hit.score, -hit.info.updated_at, hit.info.id))
        return hits[:limit]

    def read_events(
        self,
        session_id: str,
        *,
        kinds: Iterable[EventKind | str] | EventKind | str | None = None,
        role: str | None = None,
        after_seq: int = 0,
        limit: int = 100,
    ) -> tuple[NativeEventMatch, ...]:
        """Return matching events of one session in sequence order."""

        _validate_session_id(session_id)
        kind_filter = _normalize_kinds(kinds)
        role_name = _normalize_role(role)
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise _invalid("after_seq must be a non-negative integer")
        _validate_limit(limit)
        path = _session_path(session_id, self.root)
        try:
            log = NativeCodec.read(path)
        except Exception as exc:
            raise NativeQueryError(
                NATIVE_QUERY_CORRUPT,
                f"native session {session_id} cannot be read from {path}: {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc
        matches: list[NativeEventMatch] = []
        for event in log.events:
            if event.seq <= after_seq:
                continue
            if kind_filter is not None and event.type not in kind_filter:
                continue
            match = _event_match(event, needle=None, role=role_name)
            if match is None:
                continue
            matches.append(match)
            if len(matches) >= limit:
                break
        return tuple(matches)

    def _rows(self, session_filter: NativeSessionFilter | None) -> list[NativeSessionInfo]:
        infos = list_native_sessions(self.root, include_archived=True)
        if session_filter is None:
            infos = [info for info in infos if not info.archived]
        else:
            infos = [info for info in infos if session_filter.matches(info)]
        return infos[: self.max_sessions]


def query_native_sessions(
    *,
    filter: NativeSessionFilter | None = None,
    sort: NativeSessionSort = NativeSessionSort.UPDATED,
    limit: int = 50,
    offset: int = 0,
    root: Path | None = None,
) -> list[NativeSessionInfo]:
    """Convenience wrapper around NativeSessionQuery.list_sessions."""

    return NativeSessionQuery(root).list_sessions(filter=filter, sort=sort, limit=limit, offset=offset)


def search_native_sessions(
    text: str,
    *,
    filter: NativeSessionFilter | None = None,
    role: str | None = None,
    limit: int = 20,
    root: Path | None = None,
) -> list[NativeSessionSearchHit]:
    """Convenience wrapper around NativeSessionQuery.search."""

    return NativeSessionQuery(root).search(text, filter=filter, role=role, limit=limit)


def get_native_session(session_id: str, *, root: Path | None = None) -> NativeSessionInfo:
    """Convenience wrapper around NativeSessionQuery.get."""

    return NativeSessionQuery(root).get(session_id)


def read_native_events(
    session_id: str,
    *,
    kinds: Iterable[EventKind | str] | EventKind | str | None = None,
    role: str | None = None,
    after_seq: int = 0,
    limit: int = 100,
    root: Path | None = None,
) -> tuple[NativeEventMatch, ...]:
    """Convenience wrapper around NativeSessionQuery.read_events."""

    return NativeSessionQuery(root).read_events(
        session_id,
        kinds=kinds,
        role=role,
        after_seq=after_seq,
        limit=limit,
    )


def rename_native_session(
    session_id: str,
    title: str,
    *,
    source: str = "user",
    root: Path | None = None,
) -> NativeSessionInfo:
    """Append a session.title event and return the refreshed row."""

    _validate_session_id(session_id)
    normalized = _normalize_title(title)
    if source not in TITLE_SOURCES:
        raise _invalid(f"title source must be one of {sorted(TITLE_SOURCES)}, got {source!r}")
    path = _session_path(session_id, root)
    _append_native_event(
        path, EventKind.SESSION_TITLE, {"title": normalized, "source": source}, session_id=session_id
    )
    return _refresh_info(session_id, root)


def archive_native_session(
    session_id: str,
    *,
    archived: bool = True,
    source: str = "user",
    root: Path | None = None,
) -> NativeSessionInfo:
    """Append the archive marker event (or an unarchive marker) and refresh."""

    _validate_session_id(session_id)
    if not isinstance(archived, bool):
        raise _invalid("archived must be a bool")
    if not isinstance(source, str) or not source.strip():
        raise _invalid("source must be a non-empty string")
    path = _session_path(session_id, root)
    data = {"archived": archived, "source": source.strip(), "at": now_ms()}
    _append_native_event(path, ARCHIVE_EVENT, data, session_id=session_id)
    return _refresh_info(session_id, root)


def is_native_session_archived(session_id: str, *, root: Path | None = None) -> bool:
    """Return the folded archive state of one native session."""

    return get_native_session(session_id, root=root).archived


def _invalid(message: str) -> NativeQueryError:
    return NativeQueryError(NATIVE_QUERY_INVALID_FILTER, message)


def _not_found(session_id: str, *, cause: BaseException | None = None) -> NativeQueryError:
    return NativeQueryError(NATIVE_QUERY_NOT_FOUND, f"native session not found: {session_id}", cause=cause)


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise _invalid("session id must be a non-empty string")


def _validate_limit(value: int, *, name: str = "limit") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"{name} must be a non-negative integer")


def _validate_offset(value: int) -> None:
    _validate_limit(value, name="offset")


def _normalize_role(role: str | None) -> str | None:
    if role is None:
        return None
    if not isinstance(role, str) or role not in _NATIVE_ROLES:
        raise _invalid(f"role must be one of {sorted(_NATIVE_ROLES)}, got {role!r}")
    return role


def _normalize_search_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise _invalid("search text must be a non-empty string")
    return " ".join(text.split()).casefold()


def normalize_native_title(title: str) -> str:
    """Normalize a session title, raising NativeQueryError when it cannot be used."""

    return _normalize_title(title)


def _normalize_title(title: str) -> str:
    if not isinstance(title, str):
        raise NativeQueryError(NATIVE_QUERY_INVALID_TITLE, "title must be a string")
    collapsed = " ".join(title.split())
    if not collapsed:
        raise NativeQueryError(NATIVE_QUERY_INVALID_TITLE, "title must not be empty")
    if len(collapsed) > _MAX_TITLE_CHARS:
        raise NativeQueryError(NATIVE_QUERY_INVALID_TITLE, f"title exceeds {_MAX_TITLE_CHARS} characters")
    return collapsed


def _normalize_kinds(kinds: Iterable[EventKind | str] | EventKind | str | None) -> frozenset[str] | None:
    if kinds is None:
        return None
    if isinstance(kinds, (EventKind, str)):
        values: Iterable[EventKind | str] = (kinds,)
    else:
        values = kinds
    normalized: set[str] = set()
    for kind in values:
        if isinstance(kind, EventKind):
            normalized.add(kind.value)
            continue
        if not isinstance(kind, str):
            raise _invalid(f"event kind must be a string, got {type(kind).__name__}")
        if kind not in _KNOWN_KINDS and PLUGIN_EVENT_RE.fullmatch(kind) is None:
            raise _invalid(f"unknown native event kind: {kind!r}")
        normalized.add(kind)
    if not normalized:
        raise _invalid("kinds must name at least one event kind")
    return frozenset(normalized)


def _haystack(info: NativeSessionInfo) -> str:
    return " ".join((info.id, info.title, info.preview or "", info.cwd)).casefold()


def _match_fields(info: NativeSessionInfo, needle: str) -> tuple[str, ...]:
    matched: list[str] = []
    if needle in info.id.casefold():
        matched.append("id")
    if needle in info.title.casefold():
        matched.append("title")
    if needle in (info.preview or "").casefold():
        matched.append("preview")
    if needle in info.cwd.casefold():
        matched.append("cwd")
    return tuple(matched)


def _sorted_sessions(infos: list[NativeSessionInfo], sort: NativeSessionSort) -> list[NativeSessionInfo]:
    if sort is NativeSessionSort.CREATED:
        return sorted(infos, key=lambda info: (info.created_at, info.id), reverse=True)
    if sort is NativeSessionSort.TITLE:
        return sorted(infos, key=lambda info: (info.title.casefold(), info.id))
    if sort is NativeSessionSort.TURNS:
        return sorted(infos, key=lambda info: (info.turns, info.id), reverse=True)
    return sorted(infos, key=lambda info: (info.updated_at, info.id), reverse=True)


def _try_read(path: Path) -> SessionLog | None:
    try:
        return NativeCodec.read(path)
    except Exception:  # noqa: BLE001
        return None


def _event_role(event: Event) -> str | None:
    role = _ROLE_BY_KIND.get(event.type)
    if role is not None:
        return role
    if event.type in (EventKind.MESSAGE_REPLACED.value, EventKind.COMPACTION_DONE.value):
        message = event.data.get("message")
        if isinstance(message, Mapping):
            raw_role = message.get("role")
            if isinstance(raw_role, str) and raw_role in _NATIVE_ROLES:
                return raw_role
    return None


def _event_match(event: Event, *, needle: str | None, role: str | None) -> NativeEventMatch | None:
    if role is not None and _event_role(event) != role:
        return None
    text = _event_text(event)
    if needle is not None and needle not in text.casefold():
        return None
    return NativeEventMatch(
        seq=event.seq,
        kind=event.type,
        role=_event_role(event),
        preview=_collapse(text, _MATCH_PREVIEW),
        created_at=event.ts,
    )


def _event_text(event: Event) -> str:
    data = event.data if isinstance(event.data, Mapping) else {}
    if event.type in _MESSAGE_KINDS:
        return _raw_message_text(data.get("message"))
    if event.type == EventKind.TOOL_CALL.value:
        parts = [part for key in ("name", "arguments") if isinstance((part := data.get(key)), str)]
        return " ".join(parts)
    if event.type == EventKind.SESSION_TITLE.value:
        title = data.get("title")
        return title if isinstance(title, str) else ""
    return _collect_text(data)


def _raw_message_text(raw: Any) -> str:
    if not isinstance(raw, Mapping):
        return ""
    content = raw.get("content")
    if not isinstance(content, list):
        text = raw.get("text")
        return text if isinstance(text, str) else ""
    chunks: list[str] = []
    for part in content[:64]:
        if not isinstance(part, Mapping):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks)


def _collect_text(value: Any, *, depth: int = 0) -> str:
    if depth > 3:
        return ""
    if isinstance(value, str):
        return value
    chunks: list[str] = []
    if isinstance(value, Mapping):
        for item in list(value.values())[:32]:
            chunks.append(_collect_text(item, depth=depth + 1))
    elif isinstance(value, list):
        for item in value[:32]:
            chunks.append(_collect_text(item, depth=depth + 1))
    return " ".join(part for part in chunks if part)


def _collapse(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 1:
        return "…"[:limit]
    return collapsed[: limit - 1] + "…"


def _session_path(session_id: str, root: Path | None) -> Path:
    try:
        return resolve_native_session_path(session_id, root)
    except FileNotFoundError as exc:
        raise _not_found(session_id, cause=exc) from exc


def _refresh_info(session_id: str, root: Path | None) -> NativeSessionInfo:
    try:
        return read_native_transcript(session_id, root).info
    except FileNotFoundError as exc:
        raise _not_found(session_id, cause=exc) from exc
    except Exception as exc:
        raise NativeQueryError(
            NATIVE_QUERY_CORRUPT,
            f"native session {session_id} cannot be read back: {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc


def _append_native_event(
    path: Path,
    kind: EventKind | str,
    data: Mapping[str, Any],
    *,
    session_id: str,
) -> Event:
    session: NativeSession | None = None
    try:
        try:
            session = NativeSession.open(path)
        except FileNotFoundError as exc:
            raise _not_found(session_id, cause=exc) from exc
        except SessionLockedError as exc:
            raise NativeQueryError(
                NATIVE_QUERY_LOCKED,
                f"native session {session_id} is locked: {exc}",
                cause=exc,
            ) from exc
        except Exception as exc:
            raise NativeQueryError(
                NATIVE_QUERY_CORRUPT,
                f"native session {session_id} cannot be opened: {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc
        try:
            return session.append(kind, data)
        except Exception as exc:
            label = kind.value if isinstance(kind, EventKind) else kind
            raise NativeQueryError(
                NATIVE_QUERY_CORRUPT,
                f"native session {session_id} cannot append {label}: {type(exc).__name__}: {exc}",
                cause=exc,
            ) from exc
    finally:
        if session is not None:
            session.close()
