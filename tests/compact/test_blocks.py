"""Tests for the append-only block store."""

from __future__ import annotations

import json
import pathlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from deeppyro.compact.blocks import (
    BLOCK_STORE_VERSION,
    BlockRecord,
    BlockStore,
    CompactError,
)


def _add(store: BlockStore, session_id: str = "session-test", **overrides):
    payload = {
        "start_ref": "m00001",
        "end_ref": "m00002",
        "start_seq": 1,
        "end_seq": 2,
        "title": "topic",
        "summary": "summary text",
    }
    payload.update(overrides)
    return store.add(session_id, **payload)


def test_add_assigns_increasing_ids_and_persists(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    first = _add(store)
    second = _add(store, start_ref="m00003", end_ref="m00004", start_seq=3, end_seq=4)
    assert (first.id, second.id) == ("b1", "b2")
    assert first.created_at > 0
    reloaded = BlockStore(tmp_path).list("session-test")
    assert [record.id for record in reloaded] == ["b1", "b2"]
    assert reloaded[0] == first
    lines = store.path("session-test").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["version"] == BLOCK_STORE_VERSION


def test_next_id_does_not_write(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    assert store.next_id("session-test") == "b1"
    _add(store, block_id="b7")
    assert store.next_id("session-test") == "b8"
    assert len(store.list("session-test")) == 1


def test_explicit_duplicate_block_id_conflicts(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    _add(store, block_id="b1")
    with pytest.raises(CompactError) as info:
        _add(store, block_id="b1")
    assert info.value.code == "COMPACT_BLOCK_CONFLICT"
    assert len(store.list("session-test")) == 1


def test_missing_session_is_empty(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    assert store.list("nope") == []
    assert store.get("nope", "b1") is None
    assert store.load("nope") == ([], 0)


def test_corrupt_lines_are_skipped_and_counted(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    _add(store)
    path = store.path("session-test")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("not json\n")
        handle.write('{"id":"b2"}\n')
    records, corrupt = store.load("session-test")
    assert [record.id for record in records] == ["b1"]
    assert corrupt == 2


def test_offsets_and_rollback_drop_appended_lines(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    _add(store)
    offsets = store.offsets("session-test")
    assert offsets[0] > 0
    _add(store, start_ref="m00003", end_ref="m00004", start_seq=3, end_seq=4)
    assert len(store.list("session-test")) == 2
    store.rollback("session-test", offsets)
    assert [record.id for record in store.list("session-test")] == ["b1"]
    assert store.offsets("session-test") == offsets


def test_rollback_ignores_missing_files(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    store.rollback("absent", (0, 0))
    assert store.list("absent") == []


def test_concurrent_adds_get_unique_ids(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    start = threading.Barrier(4)

    def worker(_index: int) -> str:
        start.wait(timeout=5)
        return _add(store).id

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(worker, range(4)))
    assert sorted(ids) == ["b1", "b2", "b3", "b4"]
    assert len(store.list("session-test")) == 4


def test_path_uses_a_single_safe_segment(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    path = store.path("session:a/b c")
    assert path.parent == tmp_path
    assert "/" not in path.name
    assert ":" not in path.name
    assert path.name.endswith(".jsonl")
    assert store.refs_path("session:a/b c").name.endswith(".refs.jsonl")


def test_block_record_validation() -> None:
    good = {
        "id": "b1",
        "sessionId": "session-test",
        "range": {"start": "m00001", "end": "m00002"},
        "startSeq": 1,
        "endSeq": 2,
        "title": "topic",
        "summary": "text",
        "createdAt": 5,
    }
    record = BlockRecord.from_dict(good)
    assert record.to_dict()["version"] == BLOCK_STORE_VERSION
    assert BlockRecord.from_dict(record.to_dict()) == record

    with pytest.raises(ValueError):
        BlockRecord.from_dict({**good, "id": "x1"})
    with pytest.raises(ValueError):
        BlockRecord.from_dict({**good, "startSeq": 5, "endSeq": 2})
    with pytest.raises(TypeError):
        BlockRecord.from_dict([("bad", "shape")])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        BlockRecord.from_dict({**good, "range": "m00001-m00002"})  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        BlockRecord.from_dict({**good, "createdAt": True})
    with pytest.raises(TypeError):
        BlockRecord(
            summary=object(),
            id="b1",
            session_id="s",
            start_ref="m1",
            end_ref="m2",
            start_seq=1,
            end_seq=2,
            title="t",
            created_at=0,
        )  # type: ignore[arg-type]


def test_only_one_line_per_record(tmp_path: pathlib.Path) -> None:
    store = BlockStore(tmp_path)
    _add(store, summary="line one\nline two")
    raw = store.path("session-test").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    assert json.loads(raw)["summary"] == "line one\nline two"
