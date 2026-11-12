"""Session store: create, open (jsonl / zstd), enumerate, and latest.

The store owns a root directory.  New sessions are written in the flat layout
used by this port; open/list also understand DSH's nested
<projectKey>/<sessionId>/session.v4.jsonl[.zstd] layout so real DSH session
directories can be read without conversion.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from deeppyro.config import session_dir
from deeppyro.core.errors import SessionError
from deeppyro.core.ids import new_session_id, now_ms
from deeppyro.core.wire import LogRecord, SessionHeader
from deeppyro.session import writer
from deeppyro.session.layout import (
    LOG_BASENAME,
    LOG_SUFFIXES,
    discover_session_files,
    encode_segment,
    flat_session_path,
)
from deeppyro.session.reader import read_header, read_session_file
from deeppyro.session.session import Session


@dataclass(slots=True)
class SessionInfo:
    """Cheap summary of one persisted session, newest first in SessionStore.list()."""

    id: str
    path: pathlib.Path
    cwd: str
    createdAt: int
    title: str | None
    turns: int
    recordCount: int
    agentPreset: str | None


class SessionStore:
    """Filesystem-backed collection of session v4 logs."""

    def __init__(self, root: pathlib.Path | None = None) -> None:
        self.root = pathlib.Path(root) if root is not None else session_dir()

    def create(
        self,
        cwd: pathlib.Path,
        *,
        agent_preset: str | None = None,
        session_id: str | None = None,
    ) -> Session:
        """Create an empty session log and return the open Session."""
        cwd_path = pathlib.Path(cwd)
        if not cwd_path.is_absolute():
            raise SessionError(f"session cwd must be an absolute path: {str(cwd_path)!r}")
        sid = new_session_id() if session_id is None else session_id
        if not isinstance(sid, str) or not sid:
            raise SessionError("session id must be a non-empty string")
        path = flat_session_path(self.root, sid)
        if path.exists():
            raise SessionError(f"session {sid!r} already exists at {path}")
        header = SessionHeader(id=sid, createdAt=now_ms(), cwd=str(cwd_path), agentPreset=agent_preset)
        writer.write_new_session(path, header, [])
        return Session(path=path, header=header, records=[])

    def open(self, session_id: str) -> Session:
        """Open one session by id, accepting plain and zstd logs."""
        if not isinstance(session_id, str) or not session_id:
            raise SessionError("session id must be a non-empty string")
        path = self._locate(session_id)
        data = read_session_file(path)
        if data.header.id != session_id:
            raise SessionError(
                f"session log {path} holds session {data.header.id!r}, expected {session_id!r}"
            )
        return Session(
            path=path,
            header=data.header,
            records=data.records,
            truncated=data.truncated,
            trimmed=data.trimmed,
            zstd=path.name.endswith(".zstd"),
        )

    def list(self) -> list[SessionInfo]:
        """All readable sessions, newest first by createdAt."""
        infos: dict[str, SessionInfo] = {}
        for path in discover_session_files(self.root):
            try:
                data = read_session_file(path)
            except SessionError:
                continue
            if data.header.id in infos:
                continue
            infos[data.header.id] = _build_info(path, data.header, data.records)
        return sorted(infos.values(), key=lambda info: (info.createdAt, info.id), reverse=True)

    def latest(self) -> SessionInfo | None:
        """Most recently created session, if any."""
        sessions = self.list()
        return sessions[0] if sessions else None

    # -- internals ---------------------------------------------------------

    def _locate(self, session_id: str) -> pathlib.Path:
        base = self.root
        names = [encode_segment(session_id)]
        if session_id not in names:
            names.append(session_id)
        for name in names:
            for suffix in LOG_SUFFIXES:
                candidate = base / f"{name}{suffix}"
                if candidate.is_file():
                    return candidate
        if base.is_dir():
            for project in sorted(base.iterdir()):
                if not project.is_dir():
                    continue
                for name in names:
                    directory = project / name
                    if not directory.is_dir():
                        continue
                    for suffix in LOG_SUFFIXES:
                        candidate = directory / f"{LOG_BASENAME}{suffix}"
                        if candidate.is_file():
                            return candidate
                for suffix in LOG_SUFFIXES:
                    candidate = project / f"{LOG_BASENAME}{suffix}"
                    if candidate.is_file():
                        try:
                            if read_header(candidate).id == session_id:
                                return candidate
                        except SessionError:
                            continue
        for candidate in discover_session_files(base):
            try:
                header = read_header(candidate)
            except SessionError:
                continue
            if header.id == session_id:
                return candidate
        raise SessionError(f"session {session_id!r} not found under {base}")

    def _create_with_seed(self, header: SessionHeader, events: Sequence[Mapping[str, Any]]) -> Session:
        """Materialize a fully validated seeded session (used by fork)."""
        path = flat_session_path(self.root, header.id)
        if path.exists():
            raise SessionError(f"session {header.id!r} already exists at {path}")
        writer.write_new_session(path, header, events)
        data = read_session_file(path)
        return Session(
            path=path,
            header=data.header,
            records=data.records,
            truncated=data.truncated,
            trimmed=data.trimmed,
        )


def _build_info(path: pathlib.Path, header: SessionHeader, records: Sequence[LogRecord]) -> SessionInfo:
    title: str | None = None
    for record in reversed(records):
        if record.type != "session/title":
            continue
        candidate = record.data.get("title")
        if isinstance(candidate, str) and candidate:
            title = candidate
            break
    turns = sum(1 for record in records if record.type == "turn/start")
    return SessionInfo(
        id=header.id,
        path=path,
        cwd=header.cwd,
        createdAt=header.createdAt,
        title=title,
        turns=turns,
        recordCount=len(records),
        agentPreset=header.agentPreset,
    )
