"""Native session control: the service over the schema 1 session layer.

One object that the native plug-ins and the TUI turn driver share for session
lifecycle: create, open, read, list, rename, archive and fork. It is a thin,
typed facade over the modules that already own each operation, so there is
exactly one implementation of the log format, the fold, the query filters and
the fork copy:

* create/open/read -> deeppyro.session.schema (NativeSession, NativeCodec)
* list/latest/transcript/look -> deeppyro.session.native_index
* rename/archive/lookup -> deeppyro.session.native_query
* fork -> deeppyro.session.native_fork

No UI, no wire model and no v4 session code is involved. Reads never take the
session lock. open() and fork() return a NativeSession the caller owns and must
close; a session locked by another process raises SessionLockedError untouched.
The default session directory is the same one the rest of the runtime uses
(deeppyro.paths.sessions_dir), so the control plane can be pointed at a temp
root in tests without patching globals.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from deeppyro.api.types import DEFAULT_PRESET_ID
from deeppyro.core.ids import new_session_id
from deeppyro.session import schema as _schema
from deeppyro.session.native_fork import fork_native_session
from deeppyro.session.native_index import (
    NativeSessionInfo,
    NativeTranscript,
    list_native_sessions,
    read_native_log,
    read_native_transcript,
    resolve_native_session_path,
)
from deeppyro.session.native_query import (
    NATIVE_QUERY_NOT_FOUND,
    NativeQueryError,
    archive_native_session,
    get_native_session,
    rename_native_session,
)
from deeppyro.session.schema import ORIGINS, SESSION_ID_RE, NativeSession, SessionLog

__all__ = ["NativeSessionControl", "NewSession"]


@dataclass(frozen=True, slots=True)
class NewSession:
    """Everything one native session needs at creation time.

    cwd is resolved to an absolute path by the schema. The origin must be one of
    the schema origins (user/headless/subagent/fork); parent_id and
    delegation_depth describe child sessions created by team/subagent callers.
    Pass session_id/created_at for reproducible tests, otherwise they default to
    a fresh uuid and the current clock.
    """

    cwd: Path
    preset: str = DEFAULT_PRESET_ID
    origin: str = "user"
    parent_id: str | None = None
    delegation_depth: int = 0
    session_id: str | None = None
    created_at: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cwd, (str, Path)):
            raise ValueError("session cwd must be a path")
        if self.origin not in ORIGINS:
            raise ValueError(f"session origin must be one of {sorted(ORIGINS)}, got {self.origin!r}")
        if type(self.delegation_depth) is not int or self.delegation_depth < 0:
            raise ValueError("delegation_depth must be a non-negative int")
        if self.parent_id is not None and not isinstance(self.parent_id, str):
            raise ValueError("parent_id must be a session id string or None")
        if self.session_id is not None and SESSION_ID_RE.fullmatch(self.session_id) is None:
            raise ValueError(f"invalid session id: {self.session_id!r}")
        if self.created_at is not None and (type(self.created_at) is not int or self.created_at < 0):
            raise ValueError("created_at must be a non-negative int")


class NativeSessionControl:
    """Typed session lifecycle facade over the native session modules."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else None

    @property
    def root(self) -> Path | None:
        """Directory sessions live in; None means the runtime default."""
        return self._root

    # -------------------------------------------------------------- lifecycle

    def create(self, spec: NewSession, *, deferred: bool = False) -> NativeSession:
        """Create one session and return it open (caller closes).

        With deferred=True the file and lock are only created by the first
        user/turn event, so an abandoned draft leaves nothing on disk.
        Raises FileExistsError when the target id already exists.
        """
        session_id = spec.session_id or new_session_id()
        path = _schema.sessions_path_for(session_id, self._root)
        return NativeSession.create(
            path,
            cwd=spec.cwd,
            preset=spec.preset,
            session_id=session_id,
            parent_id=spec.parent_id,
            origin=spec.origin,
            delegation_depth=spec.delegation_depth,
            created_at=spec.created_at,
            deferred=deferred,
        )

    def open(self, session_id: str) -> NativeSession:
        """Open an existing session for writing; the caller owns the lock.

        Raises NativeQueryError(NATIVE_QUERY_NOT_FOUND) for an unknown id and
        lets SessionLockedError through when another writer holds the lock.
        """
        info = self.look(session_id)
        return NativeSession.open(info.path)

    def fork(self, session_id: str, *, new_id: str | None = None) -> NativeSession:
        """Copy a session into a new fork and return it open (caller closes)."""
        return fork_native_session(session_id, root=self._root, new_id=new_id)

    # ------------------------------------------------------------------ reads

    def exists(self, session_id: str) -> bool:
        """Whether a native session with this id is readable."""
        try:
            self.look(session_id)
        except NativeQueryError as exc:
            if exc.code == NATIVE_QUERY_NOT_FOUND:
                return False
            raise
        return True

    def read(self, session_id: str) -> SessionLog:
        """Parse one session log without taking its write lock."""
        try:
            return read_native_log(session_id, self._root)
        except FileNotFoundError as exc:
            raise _not_found(session_id, cause=exc) from exc

    def look(self, session_id: str) -> NativeSessionInfo:
        """One session's browser row (raises NativeQueryError for unknown ids)."""
        return get_native_session(session_id, root=self._root)

    def list(
        self,
        *,
        include_archived: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[NativeSessionInfo]:
        """List readable sessions newest first, optionally including archived ones."""
        if type(limit) is not int and limit is not None:
            raise ValueError("limit must be an int or None")
        if type(offset) is not int:
            raise ValueError("offset must be an int")
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("offset and limit must be non-negative")
        sessions: Sequence[NativeSessionInfo] = list_native_sessions(
            self._root, include_archived=include_archived
        )
        start = offset
        end = None if limit is None else start + limit
        return list(sessions[start:end])

    def latest(self, *, include_archived: bool = False) -> NativeSessionInfo | None:
        """The newest readable session, or None when the directory is empty."""
        sessions = self.list(include_archived=include_archived, limit=1)
        return sessions[0] if sessions else None

    def transcript(self, session_id: str) -> NativeTranscript:
        """Fold one session into bounded display entries (read-only)."""
        try:
            return read_native_transcript(session_id, self._root)
        except FileNotFoundError as exc:
            raise _not_found(session_id, cause=exc) from exc

    # ---------------------------------------------------------------- writers

    def rename(self, session_id: str, title: str, *, source: str = "user") -> str:
        """Append a session.title fact and return the normalised title."""
        return rename_native_session(session_id, title, source=source, root=self._root).title

    def archive(self, session_id: str, *, archived: bool = True) -> bool:
        """Append an archive marker and return the resulting archived state."""
        return archive_native_session(session_id, archived=archived, root=self._root).archived

    def path_for(self, session_id: str) -> Path:
        """Resolve one session's log path without reading it."""
        try:
            return resolve_native_session_path(session_id, self._root)
        except FileNotFoundError as exc:
            raise _not_found(session_id, cause=exc) from exc


def _not_found(session_id: str, *, cause: BaseException | None = None) -> NativeQueryError:
    return NativeQueryError(
        NATIVE_QUERY_NOT_FOUND,
        f"native session not found: {session_id}",
        cause=cause,
    )
