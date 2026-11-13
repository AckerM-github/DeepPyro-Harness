"""Public runtime memory service used by plugins, tools and the host."""

from __future__ import annotations

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
    """Thin, stateless facade over one MemoryStore.

    The service keeps no cache: every call reads memories.json, so two
    services or two processes that share the file see each other's writes and
    the revision always reflects disk state.
    """

    def __init__(self, *, store: MemoryStore) -> None:
        self.store = store

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


__all__ = ["MemoryService"]
