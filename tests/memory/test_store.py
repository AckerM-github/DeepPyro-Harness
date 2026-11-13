"""Tests for MemoryStore: persistence, projections, capacity and corruption."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.memory.errors import (
    MemoryAmbiguousMatchError,
    MemoryCapacityError,
    MemoryCorruptError,
    MemoryEntryNotFoundError,
    MemoryValidationError,
)
from deeppyro.memory.store import MemoryStore
from deeppyro.memory.types import CAPACITY_LIMITS
from tests.memory.helpers import FakeClock, make_store

STAMP = "2026-01-02T03:04:05.000Z"


def _write_state(store: MemoryStore, payload: object) -> Path:
    store.root.mkdir(parents=True, exist_ok=True)
    store.data_path.write_text(json.dumps(payload), encoding="utf-8")
    return store.data_path


def test_empty_store_starts_at_revision_zero(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    assert store.revision == 0
    assert store.entries("user") == ()
    assert store.entries("memory") == ()
    snapshot = store.snapshot("user")
    assert snapshot.text == ""
    assert snapshot.bytes == 0
    assert snapshot.limit == 4096
    assert snapshot.count == 0
    assert store.data_path == tmp_path / "memory" / "memories.json"
    assert not store.data_path.exists()
    assert store.projection_path("user") == tmp_path / "memory" / "USER.md"
    assert store.projection_path("memory") == tmp_path / "memory" / "MEMORY.md"


def test_default_root_is_deeppyro_home_memory(isolated_home: Path) -> None:
    store = MemoryStore()

    assert store.root == isolated_home / "memory"
    assert store.data_path == isolated_home / "memory" / "memories.json"


def test_remember_persists_entry_projection_and_json(tmp_path: Path) -> None:
    clock = FakeClock()
    store = make_store(tmp_path, clock=clock)

    receipt = store.remember("user", "  likes tea  ")

    assert receipt.action == "add"
    assert receipt.target == "user"
    assert receipt.revision == 1
    entry = receipt.entry
    assert entry is not None
    assert len(entry.id) == 32
    assert entry.content == "likes tea"
    assert entry.importance == "normal"
    assert entry.created_at == STAMP
    assert entry.updated_at == STAMP
    assert store.revision == 1
    assert store.entries("user") == (entry,)
    assert (store.root / "USER.md").read_text(encoding="utf-8") == "likes tea"
    assert (store.root / "MEMORY.md").read_text(encoding="utf-8") == ""
    payload = json.loads(store.data_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["revision"] == 1
    assert payload["entries"] == [entry.to_dict()]

    reloaded = MemoryStore(store.root)
    assert reloaded.entries("user") == (entry,)
    assert reloaded.snapshot("user").text == "likes tea"


def test_entries_are_isolated_by_target(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    user_receipt = store.remember("user", "likes tea")
    memory_receipt = store.remember("memory", "runs tests with uv")

    assert store.entries("user")[0].id == user_receipt.id
    assert store.entries("memory")[0].id == memory_receipt.id
    assert store.snapshot("user").text == "likes tea"
    assert store.snapshot("memory").text == "runs tests with uv"
    assert store.status().total == 2


def test_projection_joins_entries_with_one_blank_line(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    store.remember("user", "first")
    store.remember("user", "second")
    store.remember("user", "third")

    expected = "first\n\nsecond\n\nthird"
    assert store.snapshot("user").text == expected
    assert (store.root / "USER.md").read_text(encoding="utf-8") == expected
    assert (store.root / "USER.md").read_bytes() == expected.encode("utf-8")
    assert not expected.endswith("\n")
    assert (store.root / "MEMORY.md").read_text(encoding="utf-8") == ""


def test_replace_keeps_id_position_and_creation_time(tmp_path: Path) -> None:
    clock = FakeClock()
    store = make_store(tmp_path, clock=clock)
    receipt = store.remember("user", "likes tea")
    store.remember("memory", "uses uv")
    clock.advance()

    updated = store.replace("user", receipt.id, "  likes coffee  ", "critical")

    assert updated.action == "replace"
    assert updated.revision == 3
    entry = updated.entry
    assert entry is not None
    assert entry.id == receipt.id
    assert entry.content == "likes coffee"
    assert entry.importance == "critical"
    assert entry.created_at == STAMP
    assert entry.updated_at == "2026-01-02T03:04:06.000Z"
    assert [item.id for item in store.entries("user")] == [receipt.id]
    assert store.snapshot("user").text == "likes coffee"


def test_replace_without_importance_keeps_previous_value(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    receipt = store.remember("memory", "uses uv", "critical")

    updated = store.replace("memory", receipt.id, "uses uv and ruff")

    assert updated.entry is not None
    assert updated.entry.importance == "critical"
    assert store.snapshot("memory").text == "uses uv and ruff"


def test_forget_removes_entry_and_clears_projection(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    receipt = store.remember("user", "likes tea")

    removed = store.forget("user", receipt.id)

    assert removed.action == "remove"
    assert removed.id == receipt.id
    assert removed.entry is not None
    assert removed.entry.content == "likes tea"
    assert store.entries("user") == ()
    assert store.snapshot("user").text == ""
    assert (store.root / "USER.md").read_text(encoding="utf-8") == ""
    payload = json.loads(store.data_path.read_text(encoding="utf-8"))
    assert payload["entries"] == []


def test_revision_is_monotonic_across_instances(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.remember("user", "one")
    other = MemoryStore(store.root)

    assert other.revision == 1
    receipt = other.remember("memory", "two")

    assert receipt.revision == 2
    assert store.revision == 2
    third = store.remember("user", "three")
    assert third.revision == 3


def test_writes_leave_no_temporary_files(tmp_path: Path) -> None:
    clock = FakeClock()
    store = make_store(tmp_path, clock=clock)
    receipt = store.remember("user", "one")
    store.remember("memory", "two")
    store.replace("user", receipt.id, "three")
    store.forget("memory", store.entries("memory")[0].id)

    assert list(store.root.glob("*.tmp")) == []
    assert sorted(path.name for path in store.root.iterdir()) == [
        "MEMORY.md",
        "USER.md",
        "memories.json",
    ]


def test_content_is_stripped_but_internal_newlines_kept(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    receipt = store.remember("user", "  line one\nline two  ")

    assert receipt.entry is not None
    assert receipt.entry.content == "line one\nline two"
    assert store.snapshot("user").text == "line one\nline two"


def test_empty_content_rejected_without_writing(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(MemoryValidationError, match="non-empty"):
        store.remember("user", "   \n ")

    assert store.revision == 0
    assert not store.data_path.exists()


def test_content_must_be_a_string(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(MemoryValidationError, match="content must be a string"):
        store.check_content(123)  # type: ignore[arg-type]


def test_unknown_target_and_importance_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    with pytest.raises(MemoryValidationError, match="unknown memory target"):
        store.remember("other", "x")  # type: ignore[arg-type]
    with pytest.raises(MemoryValidationError, match="unknown importance"):
        store.remember("user", "x", "urgent")  # type: ignore[arg-type]


def test_blank_store_file_is_corrupt(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    store.data_path.write_text("   \n", encoding="utf-8")

    with pytest.raises(MemoryCorruptError, match="is empty"):
        store.entries("user")


def test_invalid_json_is_corrupt(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    store.data_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(MemoryCorruptError, match="not valid JSON"):
        store.entries("user")


def test_unsupported_version_is_corrupt(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    _write_state(store, {"version": 2, "revision": 0, "entries": []})

    with pytest.raises(MemoryCorruptError, match="unsupported memory store version 2"):
        store.entries("user")


def test_entry_without_id_is_corrupt(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    _write_state(
        store,
        {
            "version": 1,
            "revision": 1,
            "entries": [
                {
                    "content": "likes tea",
                    "target": "user",
                    "importance": "normal",
                    "created_at": STAMP,
                    "updated_at": STAMP,
                }
            ],
        },
    )

    with pytest.raises(MemoryCorruptError, match="missing a string id"):
        store.entries("user")


def test_duplicate_entry_ids_are_corrupt(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entry = {
        "id": "abc",
        "content": "likes tea",
        "target": "user",
        "importance": "normal",
        "created_at": STAMP,
        "updated_at": STAMP,
    }
    _write_state(store, {"version": 1, "revision": 2, "entries": [entry, dict(entry)]})

    with pytest.raises(MemoryCorruptError, match="duplicate entry id"):
        store.entries("user")


def test_user_capacity_limit_accepts_exact_fit_then_rejects(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    store.remember("user", "x" * CAPACITY_LIMITS["user"])

    assert store.snapshot("user").bytes == 4096
    with pytest.raises(MemoryCapacityError, match=r"USER\.md: 2 entries would need 4099 bytes"):
        store.remember("user", "y")
    with pytest.raises(MemoryCapacityError, match="limit is 4096 bytes"):
        store.remember("user", "y")
    assert store.revision == 1
    assert len(store.entries("user")) == 1
    assert (store.root / "USER.md").read_text(encoding="utf-8") == "x" * 4096


def test_memory_capacity_limit_accepts_exact_fit_then_rejects(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    store.remember("memory", "m" * CAPACITY_LIMITS["memory"])

    assert store.snapshot("memory").bytes == 8192
    with pytest.raises(MemoryCapacityError, match=r"MEMORY\.md: 2 entries would need 8195 bytes"):
        store.remember("memory", "z")
    with pytest.raises(MemoryCapacityError, match="limit is 8192 bytes"):
        store.remember("memory", "z")
    assert store.revision == 1


def test_failed_capacity_write_keeps_previous_projection(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.remember("user", "short")
    store.remember("user", "x" * 4089)

    with pytest.raises(MemoryCapacityError):
        store.remember("user", "y")

    assert (store.root / "USER.md").read_text(encoding="utf-8") == "short\n\n" + "x" * 4089
    assert store.revision == 2


def test_replace_can_shrink_back_under_capacity(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    receipt = store.remember("user", "x" * 4096)

    updated = store.replace("user", receipt.id, "short")

    assert updated.revision == 2
    assert store.snapshot("user").bytes == 5
    assert store.snapshot("user").text == "short"


def test_status_reports_counts_sizes_limits_and_paths(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.remember("user", "abc")
    store.remember("memory", "defg")

    status = store.status()

    assert status.revision == 2
    assert status.total == 2
    assert status.entries == {"user": 1, "memory": 1}
    assert status.bytes == {"user": 3, "memory": 4}
    assert status.limits == dict(CAPACITY_LIMITS)
    assert status.data_path == str(store.data_path)
    assert status.projections == {
        "user": str(store.root / "USER.md"),
        "memory": str(store.root / "MEMORY.md"),
    }


def test_resolve_unique_substring_and_errors(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = store.remember("user", "likes coffee")
    store.remember("user", "likes tea")

    assert store.resolve("user", "coffee").id == first.id
    with pytest.raises(MemoryEntryNotFoundError, match="no memory entry"):
        store.resolve("user", "matcha")
    with pytest.raises(MemoryAmbiguousMatchError, match="2 memory entries"):
        store.resolve("user", "likes")
    with pytest.raises(MemoryValidationError, match="old_text must be a non-empty string"):
        store.resolve("user", "  ")
