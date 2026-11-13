"""Cheap summary listing must not decode whole session logs."""

from __future__ import annotations

import pathlib

import pytest

from deeppyro.core.errors import SessionError
from deeppyro.session import Session, SessionStore, read_header
from deeppyro.session_query import (
    CORRUPT_SESSION,
    SESSION_NOT_FOUND,
    SessionQuery,
    SessionQueryError,
)
from tests.session_query.support import (
    require_fixtures,
    tmp_store,
    write_corrupt_log,
    write_zstd_log,
)


def test_list_summaries_matches_store_list(query: SessionQuery, fixture_store: SessionStore) -> None:
    summaries = query.list_summaries()
    infos = fixture_store.list()
    assert len(summaries) == len(infos)
    assert [summary.id for summary in summaries] == [info.id for info in infos]
    for summary, info in zip(summaries, infos, strict=True):
        assert summary.path == info.path
        assert summary.cwd == info.cwd
        assert summary.createdAt == info.createdAt
        assert summary.title == info.title
        assert summary.agent_preset == info.agentPreset
        assert summary.parent == read_header(info.path).parentSession


def test_list_summaries_covers_every_fixture(query: SessionQuery, manifest: dict) -> None:
    ids = {summary.id for summary in query.list_summaries()}
    assert {entry["id"] for entry in manifest["sessions"]} <= ids


def test_list_summaries_does_not_decode_whole_log(
    query: SessionQuery, monkeypatch: pytest.MonkeyPatch
) -> None:
    import deeppyro.session.reader as reader_module
    import deeppyro.session.store as store_module
    import deeppyro.session_query.query as query_module

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("list_summaries must not read whole session logs")

    monkeypatch.setattr(reader_module, "read_session_file", boom)
    monkeypatch.setattr(store_module, "read_session_file", boom)
    assert "read_session_file" not in vars(query_module)
    assert len(query.list_summaries()) == len(list(require_fixtures().joinpath("sessions").glob("*.jsonl")))


def test_list_summaries_exposes_fork_metadata(query: SessionQuery, manifest: dict) -> None:
    sessions = {entry["id"]: entry for entry in manifest["sessions"]}
    forked = next(entry for entry in sessions.values() if entry.get("parentSession"))
    summary = next(item for item in query.list_summaries() if item.id == forked["id"])
    assert summary.parent == forked["parentSession"]
    assert summary.is_seeded == bool(forked.get("isSeeded", False))
    assert summary.delegation_depth == int(forked.get("delegationDepth", 0))
    assert summary.origin == forked.get("origin")


def test_zstd_summary_and_open(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    path = write_zstd_log(
        store.root / "session-zstd.jsonl.zstd",
        title="compressed title",
        header_overrides={
            "parentSession": "session-parent",
            "origin": "subagent",
            "isSeeded": True,
            "delegationDepth": 1,
        },
    )
    query = SessionQuery(store)
    summaries = query.list_summaries()
    assert [summary.id for summary in summaries] == ["session-zstd"]
    summary = summaries[0]
    assert summary.path == path
    assert summary.title == "compressed title"
    assert summary.parent == "session-parent"
    assert summary.origin == "subagent"
    assert summary.is_seeded is True
    assert summary.delegation_depth == 1
    assert query.get("session-zstd").records[0].type == "session/title"


def test_unknown_session_is_not_found(query: SessionQuery) -> None:
    with pytest.raises(SessionQueryError) as excinfo:
        query.get("session-does-not-exist")
    assert excinfo.value.code == SESSION_NOT_FOUND
    assert isinstance(excinfo.value, SessionError)


def test_corrupt_session_is_reported(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    path = write_corrupt_log(store.root / "session-corrupt.jsonl")
    query = SessionQuery(store)
    with pytest.raises(SessionQueryError) as excinfo:
        query.get(path.stem)
    assert excinfo.value.code == CORRUPT_SESSION
    assert excinfo.value.__cause__ is not None
    # A corrupt body is cheap to list (the header is valid); opening reports it.
    summaries = query.list_summaries()
    assert [summary.id for summary in summaries] == ["session-corrupt"]
    assert summaries[0].title is None


def test_live_provider_takes_precedence(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    persisted = store.create(tmp_path / "work", session_id="session-live")
    live = Session(path=persisted.path, header=persisted.header, records=[])
    query = SessionQuery(
        store, live_provider=lambda session_id: live if session_id == "session-live" else None
    )
    assert query.get("session-live") is live
    assert query.read_session("session-live").records == []
    fallback = SessionQuery(store, live_provider=lambda session_id: None)
    assert fallback.read_session("session-live").records == persisted.records


def test_list_summaries_sorted_newest_first(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    for index, created in enumerate((1_700_000_000_000, 1_700_000_500_000, 1_700_000_100_000)):
        store.create(tmp_path / "work", session_id=f"session-{index}")
        # Rewrite the header to control createdAt deterministically.
        path = store.root / f"session-{index}.jsonl"
        header_line = path.read_text(encoding="utf-8").splitlines()[0]
        adjusted = header_line.replace(
            '"createdAt":' + header_line.split('"createdAt":')[1].split(",")[0], f'"createdAt":{created}'
        )
        path.write_text(adjusted + "\n", encoding="utf-8")
    query = SessionQuery(store)
    assert [summary.id for summary in query.list_summaries()] == ["session-1", "session-2", "session-0"]
