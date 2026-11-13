"""Plain data types for DeepPyro runtime memory.

Runtime memory is the small, always-in-context part of the memory system:
USER.md and MEMORY.md are projections of one JSON store. The types here are
deliberately dependency-free so the store, the service, the tool and the TUI
can all share them without importing the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: Memory belongs either to the user profile (USER.md) or to project and
#: environment knowledge (MEMORY.md).
MemoryTarget = Literal["user", "memory"]

#: Importance is a hint for the model; it does not change the storage format.
MemoryImportance = Literal["critical", "normal", "low"]

TARGET_USER: MemoryTarget = "user"
TARGET_MEMORY: MemoryTarget = "memory"

TARGETS: tuple[MemoryTarget, ...] = (TARGET_USER, TARGET_MEMORY)
IMPORTANCES: tuple[MemoryImportance, ...] = ("critical", "normal", "low")
DEFAULT_IMPORTANCE: MemoryImportance = "normal"

#: The projection file generated for each target.
PROJECTION_FILENAMES: dict[MemoryTarget, str] = {
    TARGET_USER: "USER.md",
    TARGET_MEMORY: "MEMORY.md",
}

#: Hard UTF-8 byte caps for the two projections. A write that would exceed the
#: cap for the mutated target is rejected before anything is persisted.
CAPACITY_LIMITS: dict[MemoryTarget, int] = {
    TARGET_USER: 4096,
    TARGET_MEMORY: 8192,
}

#: On-disk format version written to memories.json.
STORE_VERSION = 1
STORE_FILENAME = "memories.json"


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One durable memory entry."""

    id: str
    content: str
    target: MemoryTarget
    importance: MemoryImportance = DEFAULT_IMPORTANCE
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "target": self.target,
            "importance": self.importance,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """The rendered projection for one target at one revision."""

    target: MemoryTarget
    entries: tuple[MemoryEntry, ...]
    text: str
    bytes: int
    limit: int

    @property
    def count(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "count": self.count,
            "bytes": self.bytes,
            "limit": self.limit,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class MemoryReceipt:
    """Result of one mutation; the agent-facing tool returns it as JSON."""

    id: str
    action: str
    target: MemoryTarget
    revision: int
    entry: MemoryEntry | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "action": self.action,
            "target": self.target,
            "revision": self.revision,
        }
        if self.entry is not None:
            payload["entry"] = self.entry.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class MemoryStatus:
    """Read-only counts and paths for the memory resource."""

    revision: int
    total: int
    entries: dict[str, int]
    bytes: dict[str, int]
    limits: dict[str, int]
    data_path: str
    projections: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "total": self.total,
            "entries": dict(self.entries),
            "bytes": dict(self.bytes),
            "limits": dict(self.limits),
            "data_path": self.data_path,
            "projections": dict(self.projections),
        }


__all__ = [
    "CAPACITY_LIMITS",
    "DEFAULT_IMPORTANCE",
    "IMPORTANCES",
    "PROJECTION_FILENAMES",
    "STORE_FILENAME",
    "STORE_VERSION",
    "TARGETS",
    "TARGET_MEMORY",
    "TARGET_USER",
    "MemoryEntry",
    "MemoryImportance",
    "MemoryReceipt",
    "MemorySnapshot",
    "MemoryStatus",
    "MemoryTarget",
]
