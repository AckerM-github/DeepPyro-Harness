"""SQLite FTS5 long-term memory backend: provenance, ranking, migration, errors."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from deeppyro.memory.backend import (
    MemoryProvenance,
    MemoryRecord,
    MemorySearchHit,
    hash_content,
    validate_tags,
)
from deeppyro.memory.errors import (
    MemoryBackendCorruptError,
    MemoryBackendUnavailableError,
    MemoryRecordNotFoundError,
    MemoryValidationError,
)
from deeppyro.memory.sqlite import (
    DB_FILENAME,
    SCHEMA_VERSION,
    SqliteMemoryBackend,
    build_fts_expression,
    default_memory_db_path,
)
from tests.memory.helpers import FIXED_NOW, FakeClock


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def backend(tmp_path: Path, clock: FakeClock) -> SqliteMemoryBackend:
    instance = SqliteMemoryBackend(tmp_path / "long-term.sqlite3", clock=clock)
    yield instance
    instance.close()


def test_write_records_provenance(backend: SqliteMemoryBackend, clock: FakeClock) -> None:
    record = backend.write(
        "Prefer ruff format over black",
        source_session_id="session-abc",
        source_message_id="msg_0123456789abcdef",
        run_id="run_0123456789abcdef",
        importance="critical",
        tags=("style", "python"),
    )

    assert record.provenance.content_sha256 == hash_content(record.content)
    assert record.provenance.created_at == "2026-01-02T03:04:05.000Z"
    assert record.provenance.importance == "critical"
    assert record.provenance.source_session_id == "session-abc"
    assert record.provenance.source_message_id == "msg_0123456789abcdef"
    assert record.provenance.run_id == "run_0123456789abcdef"
    assert record.provenance.tags == ("style", "python")
    assert record.provenance.created_at.startswith("2026-01-02T03:04:05")
    assert clock.moment == FIXED_NOW


def test_provenance_json_round_trip() -> None:
    record = MemoryRecord(
        id="mem_0123456789abcdef",
        content="capture the lesson",
        provenance=MemoryProvenance(
            content_sha256=hash_content("capture the lesson"),
            created_at="2026-01-02T03:04:05.000Z",
            importance="low",
            source_session_id="session-abc",
            tags=("lesson",),
        ),
    )
    payload = json.loads(json.dumps(record.to_dict()))
    again = MemoryRecord.from_dict(payload)
    assert again == record
    assert again.to_dict() == record.to_dict()

    with pytest.raises(MemoryValidationError):
        MemoryRecord.from_dict({**payload, "extra": 1})
    with pytest.raises(MemoryValidationError):
        MemoryProvenance.from_dict({**payload["provenance"], "extra": 1})


def test_provenance_rejects_bad_sha() -> None:
    with pytest.raises(MemoryValidationError):
        MemoryProvenance(content_sha256="not-a-hash", created_at="2026-01-02T03:04:05.000Z")


def test_search_is_ranked_and_limited(backend: SqliteMemoryBackend) -> None:
    first = backend.write("budget is enforced by the supervisor budget gate")
    backend.write("the supervisor aggregates worker results")
    third = backend.write("supervisor supervisor supervisor budget")

    hits = backend.search("supervisor", limit=2)
    assert len(hits) == 2
    assert all(isinstance(hit, MemorySearchHit) for hit in hits)
    assert [hit.rank for hit in hits] == [1, 2]
    assert hits[0].record.id == third.id
    assert hits[0].score >= hits[1].score

    again = backend.search("supervisor", limit=2)
    assert [(hit.rank, hit.record.id) for hit in again] == [(hit.rank, hit.record.id) for hit in hits]
    assert first.id not in {hit.record.id for hit in hits}


def test_search_matches_tags_text(backend: SqliteMemoryBackend) -> None:
    record = backend.write("always run the offline replay", tags=("ci", "native"))
    hits = backend.search("native")
    assert [hit.record.id for hit in hits] == [record.id]


def test_search_filters_by_tags(backend: SqliteMemoryBackend) -> None:
    style = backend.write("prefer ruff for linting", tags=("style",))
    backend.write("prefer pytest for testing", tags=("test",))

    hits = backend.search("prefer", tags=("style",))
    assert [hit.record.id for hit in hits] == [style.id]
    assert backend.search("prefer", tags=("style", "missing")) == ()


def test_search_escapes_fts_operators(backend: SqliteMemoryBackend) -> None:
    record = backend.write("quoted words are safe")
    # Each term is quoted, so FTS5 operators typed by a user stay text and all
    # terms are ANDed: "OR" is a required term, not an operator.
    assert [hit.record.id for hit in backend.search("quoted words")] == [record.id]
    assert build_fts_expression('a "b"') == '"a" """b"""'
    assert backend.search('quoted "words" OR') == ()


def test_forget_removes_record_and_index(backend: SqliteMemoryBackend) -> None:
    record = backend.write("forget this soon")
    removed = backend.forget(record.id)
    assert removed == record
    assert backend.count() == 0
    assert backend.search("forget") == ()
    with pytest.raises(MemoryRecordNotFoundError):
        backend.forget(record.id)


def test_status_reports_fts_and_size(backend: SqliteMemoryBackend) -> None:
    backend.write("status has one row")
    status = backend.status()
    assert status.count == 1
    assert status.fts is True
    assert status.schema_version == SCHEMA_VERSION
    assert status.path == str(backend.path)
    assert status.size_bytes > 0


def test_open_is_idempotent_and_persistent(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    first = SqliteMemoryBackend(path)
    record = first.write("survives a reopen")
    first.close()

    second = SqliteMemoryBackend(path)
    try:
        assert second.status().schema_version == SCHEMA_VERSION
        assert second.count() == 1
        assert [hit.record.id for hit in second.search("survives")] == [record.id]
    finally:
        second.close()


def test_default_path_respects_deeppyro_home(isolated_home: Path) -> None:
    assert default_memory_db_path() == isolated_home / DB_FILENAME
    backend = SqliteMemoryBackend()
    try:
        assert backend.path == isolated_home / DB_FILENAME
        assert backend.path.parent == isolated_home
        backend.write("the default path lives under DEEPPYRO_HOME")
        assert backend.search("DEEPPYRO_HOME")
    finally:
        backend.close()


def test_corrupt_database_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.sqlite3"
    path.write_bytes(b"this is not a sqlite database" * 4)
    with pytest.raises(MemoryBackendCorruptError):
        SqliteMemoryBackend(path)


def test_newer_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    backend = SqliteMemoryBackend(path)
    backend.close()
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(MemoryBackendCorruptError):
        SqliteMemoryBackend(path)


def test_closed_backend_refuses_work(tmp_path: Path) -> None:
    backend = SqliteMemoryBackend(tmp_path / "closed.sqlite3")
    backend.close()
    backend.close()
    with pytest.raises(MemoryBackendUnavailableError):
        backend.write("nope")
    with pytest.raises(MemoryBackendUnavailableError):
        backend.search("nope")


def test_validation_errors(backend: SqliteMemoryBackend) -> None:
    with pytest.raises(MemoryValidationError):
        backend.write("   ")
    with pytest.raises(MemoryValidationError):
        backend.write("content", importance="urgent")
    with pytest.raises(MemoryValidationError):
        backend.write("content", tags="style")
    with pytest.raises(MemoryValidationError):
        backend.write("content", source_session_id="")
    with pytest.raises(MemoryValidationError):
        backend.search("query", limit=0)
    with pytest.raises(MemoryValidationError):
        backend.search("   ")
    with pytest.raises(MemoryValidationError):
        backend.forget("mem_nope")
    with pytest.raises(MemoryValidationError):
        validate_tags(("ok", 'bad"tag'))
    with pytest.raises(MemoryValidationError):
        validate_tags(("ok", "bad\\tag"))
    assert validate_tags((" a ", "a", "b")) == ("a", "b")
