"""Public runtime memory service used by plugins, tools and the host."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from deeppyro.memory.backend import (
    DEFAULT_SEARCH_LIMIT,
    MemoryBackend,
    MemoryBackendStatus,
    MemoryRecord,
    MemorySearchHit,
)
from deeppyro.memory.errors import MemoryBackendUnavailableError
from deeppyro.memory.store import MemoryStore
from deeppyro.memory.types import (
    DEFAULT_IMPORTANCE,
    MemoryEntry,
    MemoryImportance,
    MemoryReceipt,
    MemorySnapshot,
    MemoryStatus,
    MemoryTarget,
)


class MemoryService:
    """Thin, stateless facade over one MemoryStore plus an optional long-term backend.

    The hot store keeps no cache: every call reads memories.json, so two services
    or two processes that share the file see each other's writes and the revision
    always reflects disk state. When a long-term backend is attached, the same
    facade also exposes append-only, provenance-carrying memory (write, ranked
    search and forget) without changing the hot-memory behavior or projections.
    """

    def __init__(self, *, store: MemoryStore, backend: MemoryBackend | None = None) -> None:
        self.store = store
        self.backend = backend

    @property
    def revision(self) -> int:
        return self.store.revision

    def entries(self, target: MemoryTarget) -> list[MemoryEntry]:
        return list(self.store.entries(target))

    def snapshot(self, target: MemoryTarget) -> MemorySnapshot:
        return self.store.snapshot(target)

    def resolve(self, target: MemoryTarget, old_text: str) -> MemoryEntry:
        return self.store.resolve(target, old_text)

    def remember(
        self,
        target: MemoryTarget,
        content: str,
        importance: MemoryImportance = DEFAULT_IMPORTANCE,
    ) -> MemoryReceipt:
        return self.store.remember(target, content, importance)

    def replace(
        self,
        target: MemoryTarget,
        entry_id: str,
        content: str,
        importance: MemoryImportance | None = None,
    ) -> MemoryReceipt:
        return self.store.replace(target, entry_id, content, importance)

    def forget(self, target: MemoryTarget, entry_id: str) -> MemoryReceipt:
        return self.store.forget(target, entry_id)

    def status(self) -> MemoryStatus:
        return self.store.status()

    @property
    def long_term_enabled(self) -> bool:
        """Whether a long-term memory backend is attached."""
        return self.backend is not None

    def write_long_term(
        self,
        content: str,
        *,
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        run_id: str | None = None,
        importance: str = DEFAULT_IMPORTANCE,
        tags: Sequence[str] = (),
        created_at: str | None = None,
    ) -> MemoryRecord:
        """Write one long-term memory record with provenance.

        Raises:
            MemoryBackendUnavailableError: when no backend is attached.
        """
        return self._require_backend().write(
            content,
            source_session_id=source_session_id,
            source_message_id=source_message_id,
            run_id=run_id,
            importance=importance,
            tags=tags,
            created_at=created_at,
        )

    def search_long_term(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SEARCH_LIMIT,
        tags: Sequence[str] = (),
    ) -> tuple[MemorySearchHit, ...]:
        """Return ranked long-term memory hits for one full-text query.

        Raises:
            MemoryBackendUnavailableError: when no backend is attached.
        """
        return self._require_backend().search(query, limit=limit, tags=tags)

    def forget_long_term(self, memory_id: str) -> MemoryRecord:
        """Delete one long-term memory record and return what was removed.

        Raises:
            MemoryBackendUnavailableError: when no backend is attached.
        """
        return self._require_backend().forget(memory_id)

    def long_term_status(self) -> MemoryBackendStatus | None:
        """Return the long-term backend health snapshot, or None when detached."""
        if self.backend is None:
            return None
        return self.backend.status()

    def close(self) -> None:
        """Close the long-term backend connection when the backend supports it."""
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()

    def _require_backend(self) -> MemoryBackend:
        if self.backend is None:
            raise MemoryBackendUnavailableError("no long-term memory backend is attached")
        return cast(MemoryBackend, self.backend)


__all__ = ["MemoryService"]
