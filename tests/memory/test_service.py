"""Tests for MemoryService: the thin scripting/agent-facing facade."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.memory.errors import (
    MemoryAmbiguousMatchError,
    MemoryEntryNotFoundError,
    MemoryValidationError,
)
from deeppyro.memory.service import MemoryService
from deeppyro.memory.store import MemoryStore
from tests.memory.helpers import FakeClock, make_store


def _service(tmp_path: Path, *, clock: FakeClock | None = None) -> MemoryService:
    return MemoryService(store=make_store(tmp_path, clock=clock))


def test_remember_returns_receipt_and_updates_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path, clock=FakeClock())

    receipt = service.remember("user", "likes tea", "critical")

    assert receipt.action == "add"
    assert receipt.target == "user"
    assert receipt.revision == 1
    assert receipt.entry is not None
    assert receipt.entry.importance == "critical"
    entries = service.entries("user")
    assert [entry.id for entry in entries] == [receipt.id]
    snapshot = service.snapshot("user")
    assert snapshot.text == "likes tea"
    assert snapshot.count == 1
    assert service.revision == 1


def test_resolve_matches_unique_substring(tmp_path: Path) -> None:
    service = _service(tmp_path)
    receipt = service.remember("memory", "runs tests with uv and pytest")

    assert service.resolve("memory", "uv and pytest").id == receipt.id


def test_resolve_ambiguous_substring_raises(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.remember("user", "likes coffee")
    service.remember("user", "drinks coffee daily")

    with pytest.raises(MemoryAmbiguousMatchError, match="2 memory entries"):
        service.resolve("user", "coffee")


def test_resolve_unknown_text_raises(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.remember("user", "likes coffee")

    with pytest.raises(MemoryEntryNotFoundError, match="no memory entry"):
        service.resolve("user", "matcha")


def test_resolve_is_target_scoped(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.remember("user", "likes alpha")
    service.remember("memory", "beta convention")

    with pytest.raises(MemoryEntryNotFoundError):
        service.resolve("memory", "alpha")
    with pytest.raises(MemoryEntryNotFoundError):
        service.resolve("user", "beta")


def test_replace_preserves_or_sets_importance(tmp_path: Path) -> None:
    clock = FakeClock()
    service = _service(tmp_path, clock=clock)
    receipt = service.remember("memory", "uses uv", "critical")
    clock.advance()

    kept = service.replace("memory", receipt.id, "uses uv and pytest")
    assert kept.entry is not None
    assert kept.entry.importance == "critical"

    changed = service.replace("memory", receipt.id, "uses uv and ruff", "low")
    assert changed.entry is not None
    assert changed.entry.importance == "low"
    assert changed.entry.id == receipt.id
    assert service.snapshot("memory").text == "uses uv and ruff"


def test_replace_unknown_entry_raises(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(MemoryEntryNotFoundError, match="no memory entry"):
        service.replace("user", "missing", "x")


def test_forget_returns_removed_entry_and_clears_target(tmp_path: Path) -> None:
    service = _service(tmp_path)
    receipt = service.remember("user", "likes tea")

    removed = service.forget("user", receipt.id)

    assert removed.action == "remove"
    assert removed.entry is not None
    assert removed.entry.content == "likes tea"
    assert service.entries("user") == []
    assert service.snapshot("user").text == ""


def test_forget_unknown_entry_raises(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(MemoryEntryNotFoundError, match="no memory entry"):
        service.forget("memory", "missing")


def test_revision_is_shared_across_service_instances(tmp_path: Path) -> None:
    first = _service(tmp_path)
    second = MemoryService(store=MemoryStore(first.store.root))

    assert first.remember("user", "one").revision == 1
    assert second.remember("user", "two").revision == 2
    assert first.revision == 2
    assert first.remember("user", "three").revision == 3


def test_validation_errors_are_surfaced(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(MemoryValidationError, match="unknown memory target"):
        service.entries("other")  # type: ignore[arg-type]
    with pytest.raises(MemoryValidationError, match="non-empty"):
        service.remember("user", "   ")
    with pytest.raises(MemoryValidationError, match="unknown importance"):
        service.remember("user", "x", "urgent")  # type: ignore[arg-type]


def test_status_aggregates_both_targets(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.remember("user", "abc")
    service.remember("memory", "de")

    status = service.status()

    assert status.revision == 2
    assert status.entries == {"user": 1, "memory": 1}
    assert status.bytes == {"user": 3, "memory": 2}
    assert status.data_path.endswith("memories.json")
