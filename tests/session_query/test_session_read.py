"""Session reads and model-visible surfaces mirror the session package."""

from __future__ import annotations

import pathlib

import pytest

from deeppyro.session import SessionStore, read_session_file, write_new_session
from deeppyro.session_query import (
    INVALID_SURFACE,
    SESSION_NOT_FOUND,
    SessionQuery,
    SessionQueryError,
)
from tests.session.support import event, header, session_files, surface, user_message
from tests.session_query.support import tmp_store


def test_read_session_matches_reader(query: SessionQuery) -> None:
    for path in session_files():
        session = query.read_session(path.stem.split(".")[0])
        expected = read_session_file(path)
        assert session.header == expected.header
        assert session.records == expected.records
        assert session.truncated == expected.truncated
        assert session.trimmed == expected.trimmed


def test_read_surface_matches_derive(query: SessionQuery) -> None:
    store = query.store
    for path in session_files():
        session_id = path.stem.split(".")[0]
        loaded = store.open(session_id)
        assert query.read_surface(session_id) == loaded.derive_messages()


def test_read_session_returns_a_detached_record_list(query: SessionQuery) -> None:
    store: SessionStore = query.store
    session_id = store.list()[0].id
    first = query.read_session(session_id)
    first.records.clear()
    second = query.read_session(session_id)
    assert second.records


def test_unknown_session_read_is_not_found(query: SessionQuery) -> None:
    with pytest.raises(SessionQueryError) as excinfo:
        query.read_session("session-missing")
    assert excinfo.value.code == SESSION_NOT_FOUND
    with pytest.raises(SessionQueryError) as excinfo:
        query.read_surface("session-missing")
    assert excinfo.value.code == SESSION_NOT_FOUND


def test_invalid_surface_maps_to_invalid_surface(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    path = store.root / "session-broken-surface.jsonl"
    events = [
        event("turn/start", 0, {"turn": 1}),
        surface(
            "user/message",
            1,
            user_message(),
            op={"op": "replace", "startSeq": 0, "endSeq": 0},
        ),
    ]
    write_new_session(path, header("session-broken-surface"), events)
    query = SessionQuery(store)
    # Raw reads still work; the replacement targets a non-surface event, which
    # is only detectable by folding the surface.
    assert query.read_session("session-broken-surface").records[0].seq == 0
    with pytest.raises(SessionQueryError) as excinfo:
        query.read_surface("session-broken-surface")
    assert excinfo.value.code == INVALID_SURFACE
    assert excinfo.value.__cause__ is not None


def test_session_read_preserves_trimmed_flag(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    path = store.root / "session-trimmed.jsonl"
    events = [
        event("turn/start", 0, {"turn": 1}),
        event("turn/end", 5, {"turn": 1, "status": "completed"}),
    ]
    write_new_session(path, header("session-trimmed"), events)
    query = SessionQuery(store)
    read = query.read_session("session-trimmed")
    assert read.trimmed is True
    assert [record.seq for record in read.records] == [0, 5]
