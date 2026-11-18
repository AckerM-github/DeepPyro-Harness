"""Fork a native schema 1 session into a new session id.

The fork copies the event log (or a through_seq prefix of it) and reparents the
header: the child gets a fresh id, its created_at is the fork instant, parent_id
points at the source, origin is "fork" and delegation_depth is incremented.
Event order, sequence numbers, types and payloads are preserved; only the
envelope timestamp is rebased by a constant offset so timestamps stay relative
and keep satisfying the append-order checks.

The copy is crash safe: the new log is written to a temporary sibling file,
flushed and fsynced, and only then moved into place with os.replace, mirroring
NativeSession.create.  The returned session is already open (and locked); the
caller owns it and must close it.

A prefix fork is a consistent prefix, never a fabricated boundary: it copies
every event with seq <= through_seq and stops, so callers must pass a bound that
ends at a safe point (a completed turn or the source tail).  This module never
synthesizes turn or step closers and never rewrites a payload, because the
schema 1 turn reason vocabulary has no fork reason; a mid-turn copy would put
torn facts in the event log.  The copied count is the forked session's event
count; :func:`forked_event_count` reads it back.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from deeppyro.core.ids import new_session_id, now_ms
from deeppyro.paths import sessions_dir
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.native_index import resolve_native_session_path
from deeppyro.session.schema import (
    SESSION_ID_RE,
    Event,
    NativeCodec,
    NativeSession,
    SessionHeader,
)

__all__ = [
    "NATIVE_FORK_CORRUPT",
    "NATIVE_FORK_EXISTS",
    "NATIVE_FORK_INVALID",
    "NATIVE_FORK_LOCKED",
    "NATIVE_FORK_NOT_FOUND",
    "NativeForkError",
    "fork_native_session",
    "forked_event_count",
]

#: Error codes raised by this module.
NATIVE_FORK_NOT_FOUND = "NATIVE_FORK_NOT_FOUND"
NATIVE_FORK_CORRUPT = "NATIVE_FORK_CORRUPT"
NATIVE_FORK_EXISTS = "NATIVE_FORK_EXISTS"
NATIVE_FORK_INVALID = "NATIVE_FORK_INVALID"
NATIVE_FORK_LOCKED = "NATIVE_FORK_LOCKED"


class NativeForkError(ValueError):
    """Raised when a native session cannot be forked."""

    def __init__(self, code: str, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = {"code": code, "message": message}
        self.cause = cause

    def __reduce__(self) -> tuple[type[NativeForkError], tuple[str, str]]:
        return (type(self), (self.code, str(self)))


def fork_native_session(
    session_id: str,
    *,
    root: Path | None = None,
    new_id: str | None = None,
    created_at: int | None = None,
    through_seq: int | None = None,
) -> NativeSession:
    """Copy a native session into a new session and return it open.

    The source is read completely before anything is written, so a damaged
    source leaves no partial fork behind.  An existing target id is never
    overwritten.  With through_seq set, only events whose seq is at most that
    bound are copied (a consistent prefix); None copies the complete log.
    """

    if not isinstance(session_id, str) or not session_id.strip():
        raise _invalid("session id must be a non-empty string")
    bound = _through_seq(through_seq)
    source_path = _source_path(session_id, root)
    try:
        source = NativeCodec.read(source_path)
    except Exception as exc:
        raise NativeForkError(
            NATIVE_FORK_CORRUPT,
            f"native session {session_id} cannot be read from {source_path}: {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc
    target_id = _target_id(new_id)
    stamp = _created_at(created_at)
    base = Path(root) if root is not None else Path(sessions_dir())
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{target_id}.jsonl"
    if target.exists():
        raise NativeForkError(NATIVE_FORK_EXISTS, f"native session already exists: {target_id}")
    header = SessionHeader(
        id=target_id,
        created_at=stamp,
        cwd=source.header.cwd,
        preset=source.header.preset,
        parent_id=source.header.id,
        origin="fork",
        delegation_depth=source.header.delegation_depth + 1,
    )
    delta = stamp - source.header.created_at
    events = tuple(replace(event, ts=event.ts + delta) for event in _prefix_events(source.events, bound))
    _write_log(target, header, events)
    try:
        return NativeSession.open(target)
    except SessionLockedError as exc:
        raise NativeForkError(
            NATIVE_FORK_LOCKED,
            f"forked native session {target_id} is locked: {exc}",
            cause=exc,
        ) from exc
    except Exception as exc:
        raise NativeForkError(
            NATIVE_FORK_CORRUPT,
            f"forked native session {target_id} cannot be reopened: {type(exc).__name__}: {exc}",
            cause=exc,
        ) from exc


def forked_event_count(session: NativeSession) -> int:
    """How many events a fork copied, read from the returned session.

    A full fork copies the whole source log; a through_seq prefix fork copies
    exactly the events up to the bound.  Call this before appending anything to
    the fork, which would inflate the count.
    """

    return len(session.events)


def _invalid(message: str) -> NativeForkError:
    return NativeForkError(NATIVE_FORK_INVALID, message)


def _through_seq(through_seq: int | None) -> int | None:
    if through_seq is None:
        return None
    if isinstance(through_seq, bool) or not isinstance(through_seq, int) or through_seq < 0:
        raise _invalid("through_seq must be a non-negative integer")
    return through_seq


def _prefix_events(events: tuple[Event, ...], bound: int | None) -> tuple[Event, ...]:
    if bound is None:
        return events
    return tuple(event for event in events if event.seq <= bound)


def _source_path(session_id: str, root: Path | None) -> Path:
    try:
        return resolve_native_session_path(session_id, root)
    except FileNotFoundError as exc:
        raise NativeForkError(
            NATIVE_FORK_NOT_FOUND,
            f"native session not found: {session_id}",
            cause=exc,
        ) from exc


def _target_id(new_id: str | None) -> str:
    target_id = new_id if new_id is not None else new_session_id()
    if not isinstance(target_id, str) or SESSION_ID_RE.fullmatch(target_id) is None:
        raise _invalid(f"invalid native session id: {target_id!r}")
    return target_id


def _created_at(created_at: int | None) -> int:
    if created_at is None:
        return now_ms()
    if isinstance(created_at, bool) or not isinstance(created_at, int) or created_at < 0:
        raise _invalid("created_at must be a non-negative integer")
    return created_at


def _write_log(target: Path, header: SessionHeader, events: tuple[Event, ...]) -> None:
    temp = target.parent / f".{target.name}.tmp"
    lines = [NativeCodec.encode_header(header)]
    lines.extend(NativeCodec.encode_event(event) for event in events)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            raise NativeForkError(NATIVE_FORK_EXISTS, f"native session already exists: {target.stem}")
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    _fsync_directory(target.parent)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)
