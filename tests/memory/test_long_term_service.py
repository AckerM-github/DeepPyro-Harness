"""MemoryService long-term integration and hot-memory non-regression."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.memory.errors import MemoryBackendUnavailableError
from deeppyro.memory.service import MemoryService
from deeppyro.memory.sqlite import SqliteMemoryBackend
from tests.memory.helpers import FakeClock, make_store


def _service(tmp_path: Path, clock: FakeClock) -> MemoryService:
    store = make_store(tmp_path, clock=clock)
    backend = SqliteMemoryBackend(tmp_path / "long-term.sqlite3", clock=clock)
    return MemoryService(store=store, backend=backend)


def test_long_term_service_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeClock())
    try:
        assert service.long_term_enabled is True
        record = service.write_long_term(
            "the supervisor polls workers for convergence",
            source_session_id="session-abc",
            source_message_id="msg_0123456789abcdef",
            run_id="run_0123456789abcdef",
            tags=("team",),
        )
        hits = service.search_long_term("convergence")
        assert [hit.record.id for hit in hits] == [record.id]
        assert hits[0].record.provenance.source_session_id == "session-abc"
        assert hits[0].record.provenance.tags == ("team",)

        status = service.long_term_status()
        assert status is not None
        assert status.count == 1
        assert status.fts is True

        removed = service.forget_long_term(record.id)
        assert removed.id == record.id
        assert service.search_long_term("convergence") == ()
    finally:
        service.close()


def test_service_without_backend_keeps_hot_memory(tmp_path: Path) -> None:
    service = MemoryService(store=make_store(tmp_path, clock=FakeClock()))
    assert service.long_term_enabled is False
    assert service.long_term_status() is None
    with pytest.raises(MemoryBackendUnavailableError):
        service.write_long_term("no backend")
    with pytest.raises(MemoryBackendUnavailableError):
        service.search_long_term("no backend")
    with pytest.raises(MemoryBackendUnavailableError):
        service.forget_long_term("mem_0123456789abcdef")

    receipt = service.remember("user", "I like small diffs")
    assert service.status().total == 1
    assert service.snapshot("user").text.strip()
    assert receipt.target == "user"


def test_hot_memory_projection_unchanged_with_backend(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeClock())
    try:
        service.remember("memory", "native sessions are the source of truth")
        assert service.status().total == 1
        assert service.snapshot("memory").count == 1
        # The long-term backend is separate storage: no projection files leak there.
        assert not (tmp_path / "long-term.sqlite3-user.md").exists()
        assert service.long_term_status() is not None
        assert service.long_term_status().count == 0
    finally:
        service.close()
