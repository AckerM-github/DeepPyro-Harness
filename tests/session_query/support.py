"""Shared helpers for session_query tests."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from deeppyro.core.wire import SessionHeader
from deeppyro.session import Session, SessionStore, encode_header_line, write_new_session
from deeppyro.session_query import SessionQuery
from tests.session.support import FIXTURES, event, header, surface, user_message


def require_fixtures() -> pathlib.Path:
    """Skip when privacy-trimmed real fixtures are absent."""
    import pytest

    if not FIXTURES.is_dir():
        pytest.skip("privacy: real-session fixtures are not committed")
    return FIXTURES


def tmp_store(tmp_path: pathlib.Path) -> SessionStore:
    """A store rooted in the test's temporary directory."""
    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return SessionStore(root=root)


def query_for(store: SessionStore, **kwargs: Any) -> SessionQuery:
    return SessionQuery(store, **kwargs)


def new_session(
    store: SessionStore,
    events: list[dict[str, Any]],
    *,
    session_id: str = "session-query-test",
    cwd: str = "C:/work",
) -> Session:
    """Create a session and append raw events with automatic seq/time."""
    session = store.create(pathlib.Path(cwd), session_id=session_id)
    for raw in events:
        envelope = {key: value for key, value in raw.items() if key not in ("type", "seq", "time", "data")}
        session.append(raw["type"], raw["data"], **envelope)
    return session


def write_corrupt_log(path: pathlib.Path, session_id: str = "session-corrupt") -> pathlib.Path:
    """Write a log whose header is valid and whose second line is not JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = header(session_id)
    payload = encode_header_line(hdr) + b'{"type":"turn/start","seq":0}\n'
    path.write_bytes(payload)
    return path


def write_zstd_log(
    path: pathlib.Path,
    *,
    session_id: str = "session-zstd",
    title: str | None = "compressed title",
    header_overrides: dict[str, Any] | None = None,
) -> pathlib.Path:
    """Write a zstd log with a header frame and a second frame holding a title."""
    overrides = dict(header_overrides or {})
    hdr: SessionHeader = header(session_id, **overrides)
    events: list[dict[str, Any]] = []
    if title is not None:
        events.append(
            event("session/title", 0, {"title": title, "messageSeqs": [], "source": {"kind": "provider"}})
        )
    write_new_session(path, hdr, events, zstd=True)
    return path


def raw_header_line(tmp_path: pathlib.Path) -> bytes:
    """Canonical bytes of one valid header line (for hand-written logs)."""
    return encode_header_line(header())


def dumps_line(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


__all__ = [
    "dumps_line",
    "event",
    "new_session",
    "query_for",
    "raw_header_line",
    "require_fixtures",
    "surface",
    "tmp_store",
    "user_message",
    "write_corrupt_log",
    "write_zstd_log",
]
