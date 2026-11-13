"""Read-only session queries over the durable session.v4 store.

The service answers the questions the TUI and future tools ask about persisted
sessions without ever appending to a log: cheap summaries, validated raw reads,
model-visible surfaces, event listings and windows, literal event filters,
session ancestry, and positional replacement traces.  It reuses the session
package's public reader, canonical surface fold, and layout helpers, so the
query view can never disagree with what the model saw.

DSH semantics preserved here:

* persisted-first; a caller-supplied `live_provider` is consulted first when a
  session is open in this process, mirroring DSH's live-first registry;
* one canonical surface fold per read (never a second, ad-hoc fold);
* literal, case-insensitive text matching, not full-text search;
* no caller authorization (trusted infrastructure, read-only APIs).
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable, Collection

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord, Message
from deeppyro.session import (
    KNOWN_EVENT_TYPES,
    LOG_SUFFIXES,
    SURFACE_EVENTS,
    Session,
    SessionRead,
    SessionStore,
    decompress_zstd_frames,
    discover_session_files,
    encode_segment,
    read_header,
)
from deeppyro.session_query.filters import EventFilter, event_matches
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

#: Bytes at each end of a log scanned for the newest `session/title` event.
#: The head window is required because DSH writes the initial title after the
#: first turn's large runtime-context messages (observed ~110 KB in real logs).
TAIL_SCAN_BYTES = 256 * 1024

_DEFAULT_LIMIT = 500


class SessionQuery:
    """Read-only query facade over one :class:`SessionStore`.

    Args:
        store: the session store to read.  The store root is used for discovery.
        read_window_max: hard cap on `read_event` neighbour counts per side.
        live_provider: optional callback returning an in-memory
            :class:`deeppyro.session.Session` for an id, or `None`.  When it
            returns a session, reads use its records and the persisted file is
            left untouched.
    """

    def __init__(
        self,
        store: SessionStore,
        *,
        read_window_max: int = 50,
        live_provider: Callable[[str], Session | None] | None = None,
    ) -> None:
        if not isinstance(read_window_max, int) or isinstance(read_window_max, bool) or read_window_max < 1:
            raise ValueError("read_window_max must be a positive integer")
        if live_provider is not None and not callable(live_provider):
            raise TypeError("live_provider must be callable")
        self.store = store
        self.read_window_max = read_window_max
        self._live_provider = live_provider

    # -- summaries ---------------------------------------------------------

    def list_summaries(self) -> list[SessionSummary]:
        """Every readable session, newest first, without decoding full logs.

        Each session costs one header read plus bounded head/tail scans for the
        newest non-empty `session/title`; the body of the log is never parsed.  Files
        that fail header validation are skipped, matching
        :meth:`SessionStore.list`, and duplicate ids keep the first discovered
        file (flat layout before nested DSH layout).
        """
        summaries: dict[str, SessionSummary] = {}
        for path in discover_session_files(self.store.root):
            try:
                header = read_header(path)
            except (SessionError, OSError):
                continue
            if header.id in summaries:
                continue
            try:
                title = scan_tail_title(path)
            except (SessionError, OSError):
                title = None
            summaries[header.id] = SessionSummary(
                id=header.id,
                path=path,
                cwd=header.cwd,
                createdAt=header.createdAt,
                title=title,
                parent=header.parentSession,
                origin=header.origin,
                is_seeded=header.isSeeded,
                delegation_depth=header.delegationDepth,
                agent_preset=header.agentPreset,
            )
        return sorted(summaries.values(), key=lambda item: (item.createdAt, item.id), reverse=True)

    # -- sessions ----------------------------------------------------------

    def get(self, session_id: str) -> Session:
        """Open one session (live first, then persisted), validating the header."""
        return self._session(session_id)

    def read_session(self, session_id: str) -> SessionRead:
        """Validated records of one session, detached from the live session list.

        The returned list is a copy: appending to it cannot change the store or
        the live session.  Record objects themselves are shared (they are
        treated as immutable by the reader).
        """
        session = self._session(session_id)
        return SessionRead(
            header=session.header,
            records=list(session.records),
            truncated=session.truncated,
            trimmed=session.trimmed,
        )

    def read_surface(self, session_id: str) -> list[Message]:
        """Current model-visible surface after the canonical fold."""
        session = self._session(session_id)
        try:
            return session.derive_messages()
        except (SessionError, ValueError) as exc:
            raise SessionQueryError(
                INVALID_SURFACE, f"session {session_id!r} has an invalid surface: {exc}", cause=exc
            ) from exc

    # -- events ------------------------------------------------------------

    def list_events(
        self,
        session_id: str,
        *,
        types: Collection[str] | None = None,
        surface: bool | None = None,
        after_seq: int = 0,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[LogRecord]:
        """Lightweight event listing in seq order.

        Args:
            types: event types to keep (OR); an empty collection matches nothing;
                `None` keeps every type.
            surface: `True` keeps only model-visible surface events, `False`
                keeps only non-surface events, `None` disables the clause.
            after_seq: exclusive lower bound; only events with `seq > after_seq`
                are returned (DSH `afterSeq` semantics).
            limit: maximum number of events, at least 1.
        """
        wanted = _normalize_types(types)
        if surface is not None and not isinstance(surface, bool):
            raise _invalid("surface must be a boolean")
        if not _is_seq(after_seq):
            raise _invalid("after_seq must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise _invalid("limit must be a positive integer")
        session = self._session(session_id)
        if wanted is not None and not wanted:
            return []
        result: list[LogRecord] = []
        for record in session.records:
            if record.seq <= after_seq:
                continue
            if wanted is not None and record.type not in wanted:
                continue
            if surface is not None and (record.type in SURFACE_EVENTS) != surface:
                continue
            result.append(record)
            if len(result) >= limit:
                break
        return result

    def read_event(
        self,
        session_id: str,
        seq: int,
        *,
        before: int = 2,
        after: int = 2,
    ) -> EventWindow:
        """One event plus bounded neighbours in seq order.

        `before` and `after` are clamped independently to
        :attr:`read_window_max`; negative values or a missing seq are errors.
        """
        if not _is_seq(seq):
            raise _invalid("seq must be a non-negative integer")
        for name, value in (("before", before), ("after", after)):
            if not _is_seq(value):
                raise _invalid(f"{name} must be a non-negative integer")
        session = self._session(session_id)
        index = _index_of_seq(session.records, seq)
        if index is None:
            raise SessionQueryError(EVENT_NOT_FOUND, f"session {session_id!r} has no event seq {seq}")
        before_count = min(before, self.read_window_max)
        after_count = min(after, self.read_window_max)
        return EventWindow(
            session_id=session_id,
            seq=seq,
            event=session.records[index],
            before=list(session.records[max(0, index - before_count) : index]),
            after=list(session.records[index + 1 : index + 1 + after_count]),
        )

    def filter_events(self, session_id: str, event_filter: EventFilter) -> list[LogRecord]:
        """Every event satisfying *event_filter*, in seq order."""
        if not isinstance(event_filter, EventFilter):
            raise _invalid("event_filter must be an EventFilter")
        session = self._session(session_id)
        return [record for record in session.records if event_matches(record, event_filter)]

    # -- tracing -----------------------------------------------------------

    def trace_session(self, session_id: str) -> SessionTrace:
        """Ancestor chain and recursive descendants of one session.

        The graph is built from headers only (one bounded read per session), so
        tracing a session does not replay any log body.  Cycles and dangling
        parent ids terminate the walk instead of looping or failing.
        """
        summaries = self.list_summaries()
        by_id = {summary.id: summary for summary in summaries}
        target = by_id.get(session_id)
        if target is None:
            raise _not_found(session_id, self.store.root)
        children: dict[str, list[SessionSummary]] = {}
        for summary in summaries:
            if summary.parent:
                children.setdefault(summary.parent, []).append(summary)
        ancestors: list[SessionSummary] = []
        seen: set[str] = {session_id}
        current = target
        while current.parent and current.parent in by_id and current.parent not in seen:
            parent = by_id[current.parent]
            ancestors.append(parent)
            seen.add(parent.id)
            current = parent
        ancestors.reverse()
        descendants: list[SessionSummary] = []
        stack = list(reversed(children.get(session_id, [])))
        visited: set[str] = {session_id}
        while stack:
            node = stack.pop()
            if node.id in visited:
                continue
            visited.add(node.id)
            descendants.append(node)
            stack.extend(reversed(children.get(node.id, [])))
        return SessionTrace(ancestors=ancestors, descendants=descendants)

    def trace_event(self, session_id: str, seq: int) -> list[LogRecord]:
        """Positional replacement chain for one event.

        The chain starts at *seq*, then follows each event that replaced the
        current one in place (`surfaceOp` replace covering its seq), ending at
        the final surviving event.  The final event's `sourceEventSeqs` are
        appended one hop deep (never recursively), so callers can see which
        events a compaction or retirement consumed without expanding the whole
        ancestry.
        """
        if not _is_seq(seq):
            raise _invalid("seq must be a non-negative integer")
        session = self._session(session_id)
        records = session.records
        by_seq = {record.seq: record for record in records}
        if seq not in by_seq:
            raise SessionQueryError(EVENT_NOT_FOUND, f"session {session_id!r} has no event seq {seq}")
        replacers = _replacement_map(records, by_seq)
        chain: list[LogRecord] = [by_seq[seq]]
        seen = {seq}
        while chain[-1].seq in replacers:
            replacement = replacers[chain[-1].seq]
            if replacement.seq in seen:
                break
            chain.append(replacement)
            seen.add(replacement.seq)
        final = chain[-1]
        sources: set[int] = set()
        for source in final.sourceEventSeqs or ():
            if source in by_seq and source not in seen:
                sources.add(source)
        result = chain + [by_seq[source] for source in sorted(sources)]
        return result

    # -- internals ---------------------------------------------------------

    def _session(self, session_id: str) -> Session:
        if not isinstance(session_id, str) or not session_id:
            raise _invalid("session_id must be a non-empty string")
        live = self._live_provider(session_id) if self._live_provider is not None else None
        if live is not None:
            return live
        try:
            return self.store.open(session_id)
        except SessionError as exc:
            if self._find_path(session_id) is None:
                raise _not_found(session_id, self.store.root) from None
            raise SessionQueryError(
                CORRUPT_SESSION, f"session {session_id!r} could not be read: {exc}", cause=exc
            ) from exc

    def _find_path(self, session_id: str) -> pathlib.Path | None:
        base = self.store.root
        names = [encode_segment(session_id)]
        if session_id not in names:
            names.append(session_id)
        for name in names:
            for suffix in LOG_SUFFIXES:
                candidate = base / f"{name}{suffix}"
                if candidate.is_file():
                    return candidate
        for path in discover_session_files(base):
            try:
                header = read_header(path)
            except (SessionError, OSError):
                continue
            if header.id == session_id:
                return path
        return None


def _last_title(lines: list[str]) -> str | None:
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict) or raw.get("type") != "session/title":
            continue
        data = raw.get("data")
        title = data.get("title") if isinstance(data, dict) else None
        if isinstance(title, str) and title:
            return title
    return None


def _read_window(path: pathlib.Path, start: int, end: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(end - start)


def scan_tail_title(path: pathlib.Path, *, max_bytes: int = TAIL_SCAN_BYTES) -> str | None:
    """Newest bounded-window `session/title` title for a log.

    DSH writes the first title just after the first user turn and the initial
    runtime context can exceed 100 KB, so a pure tail scan misses most titles.
    This reader checks the last *max_bytes* first (later titles win), then the
    first *max_bytes*.  The scan is bounded on purpose: listing summaries must
    not decode a whole log.  zstd logs have no cheap random access, so one
    decompression feeds both windows; titles updated in the middle of very long
    logs are outside the bounded windows and are only visible through
    :meth:`SessionQuery.read_session`.
    """
    path = pathlib.Path(path)
    if path.name.endswith(".zstd"):
        data = decompress_zstd_frames(path.read_bytes())
        if len(data) <= max_bytes:
            return _last_title(data.decode("utf-8", "replace").splitlines())
        tail = data[-max_bytes:].decode("utf-8", "replace").splitlines()[1:]
        return _last_title(tail) or _last_title(data[:max_bytes].decode("utf-8", "replace").splitlines()[:-1])
    size = path.stat().st_size
    if size <= max_bytes:
        return _last_title(_read_window(path, 0, size).decode("utf-8", "replace").splitlines())
    tail = _read_window(path, size - max_bytes, size).decode("utf-8", "replace").splitlines()[1:]
    title = _last_title(tail)
    if title is not None:
        return title
    head = _read_window(path, 0, max_bytes).decode("utf-8", "replace").splitlines()[:-1]
    return _last_title(head)


def _normalize_types(types: Collection[str] | None) -> frozenset[str] | None:
    if types is None:
        return None
    if isinstance(types, str) or not isinstance(types, Collection):
        raise _invalid("types must be a collection of event type strings")
    normalized = frozenset(types)
    for value in normalized:
        if not isinstance(value, str) or value not in KNOWN_EVENT_TYPES:
            raise _invalid(f"unknown event type: {value!r}")
    return normalized


def _is_seq(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _invalid(message: str) -> SessionQueryError:
    return SessionQueryError(INVALID_FILTER, message)


def _not_found(session_id: str, root: pathlib.Path) -> SessionQueryError:
    return SessionQueryError(SESSION_NOT_FOUND, f"session {session_id!r} not found under {root}")


def _index_of_seq(records: list[LogRecord], seq: int) -> int | None:
    for index, record in enumerate(records):
        if record.seq == seq:
            return index
        if record.seq > seq:
            return None
    return None


def _replacement_map(records: list[LogRecord], by_seq: dict[int, LogRecord]) -> dict[int, LogRecord]:
    """seq -> the event that replaced it in place (last writer wins)."""
    last_seq = records[-1].seq if records else -1
    replacers: dict[int, LogRecord] = {}
    for record in records:
        replacement = record.replacement()
        if replacement is None:
            continue
        upper = min(replacement.endSeq, last_seq)
        for target in range(max(0, replacement.startSeq), upper + 1):
            if target in by_seq:
                replacers[target] = record
    return replacers


__all__ = ["TAIL_SCAN_BYTES", "SessionQuery", "scan_tail_title"]
