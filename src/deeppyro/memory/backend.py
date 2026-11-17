"""Long-term memory backend contract: provenance, records and search hits.

The hot-memory store in deeppyro.memory.store is deliberately small and
human-editable: two capped markdown projections plus their JSON source. Long-term
memory is the opposite: append-only, unbounded and queried by relevance, so it
lives behind a backend (SQLite FTS5 first) and every record carries provenance so
an answer can always be traced back to the session, message and run that produced
it.

This module owns the backend-neutral data shapes and the Protocol. Concrete
backends live next to it; deeppyro.memory.sqlite.SqliteMemoryBackend is the first
one.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast, runtime_checkable

from deeppyro.memory.errors import MemoryValidationError
from deeppyro.memory.types import DEFAULT_IMPORTANCE, IMPORTANCES, MemoryImportance

#: Number of hits returned when a caller does not pass an explicit limit.
DEFAULT_SEARCH_LIMIT = 10

#: Long-term memory record ids are stable random hex, one namespace per record.
MEMORY_ID_RE = re.compile(r"^mem_[0-9a-f]{16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Provenance fields a caller may require to be present on a search hit.
PROVENANCE_FIELDS = (
    "source_session_id",
    "source_message_id",
    "run_id",
    "created_at",
    "content_sha256",
    "importance",
    "tags",
)


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def format_timestamp(moment: datetime) -> str:
    """Render one datetime the way memory records store it: ISO-8601 UTC, ms, Z."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def hash_content(content: str) -> str:
    """Return the lowercase hex sha256 of a memory record's content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_content(content: object) -> str:
    """Validate and return memory content; it must be a non-blank string."""
    if not isinstance(content, str) or not content.strip():
        raise MemoryValidationError("long-term memory content must be a non-empty string")
    return content


def validate_importance(importance: object) -> MemoryImportance:
    """Validate and return one memory importance."""
    if importance not in IMPORTANCES:
        raise MemoryValidationError(f"unknown memory importance {importance!r}")
    return cast(MemoryImportance, importance)


def validate_tags(tags: object) -> tuple[str, ...]:
    """Normalize an optional tag sequence: stripped, unique, order preserved.

    A bare string is rejected instead of being iterated per character, and tags
    may not contain a double quote or backslash because the SQLite backend stores
    them as a JSON array and filters them with json_each.
    """
    if tags is None:
        return ()
    if isinstance(tags, str):
        raise MemoryValidationError("memory tags must be a sequence of strings, not a string")
    if not isinstance(tags, Sequence):
        raise MemoryValidationError("memory tags must be a sequence of strings")
    result: list[str] = []
    for item in tags:
        if not isinstance(item, str):
            raise MemoryValidationError("memory tags must be strings")
        tag = item.strip()
        if not tag:
            raise MemoryValidationError("memory tags must be non-empty")
        if '"' in tag or "\\" in tag:
            raise MemoryValidationError("memory tags may not contain quotes or backslashes")
        if len(tag) > 64:
            raise MemoryValidationError("memory tags may not exceed 64 characters")
        if tag not in result:
            result.append(tag)
    return tuple(result)


def validate_memory_id(memory_id: object) -> str:
    """Validate one long-term memory record id."""
    if not isinstance(memory_id, str) or not MEMORY_ID_RE.fullmatch(memory_id):
        raise MemoryValidationError("memory id must match 'mem_<16 hex>'")
    return memory_id


def validate_limit(limit: object) -> int:
    """Validate a search limit: a positive integer."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise MemoryValidationError("search limit must be a positive integer")
    return limit


def validate_created_at(created_at: object) -> str:
    """Validate one explicit provenance timestamp string (ISO-8601, UTC)."""
    if not isinstance(created_at, str) or not created_at:
        raise MemoryValidationError("memory created_at must be a non-empty ISO-8601 string")
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryValidationError(f"memory created_at is not ISO-8601: {created_at!r}") from exc
    return created_at


def _optional_str(value: object, subject: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise MemoryValidationError(f"memory {subject} must be a non-empty string or null")
    return value


@dataclass(frozen=True, slots=True)
class MemoryProvenance:
    """Where one long-term memory came from, plus its normalized identity fields.

    source_session_id, source_message_id and run_id are optional because a memory
    may be written by a background job or imported from a fixture; created_at,
    content_sha256 and importance are always present.
    """

    content_sha256: str
    created_at: str
    importance: MemoryImportance = DEFAULT_IMPORTANCE
    source_session_id: str | None = None
    source_message_id: str | None = None
    run_id: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.content_sha256, str) or not SHA256_RE.fullmatch(self.content_sha256):
            raise MemoryValidationError("memory content_sha256 must be 64 lowercase hex characters")
        validate_created_at(self.created_at)
        validate_importance(self.importance)
        _optional_str(self.source_session_id, "source_session_id")
        _optional_str(self.source_message_id, "source_message_id")
        _optional_str(self.run_id, "run_id")
        validate_tags(self.tags)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping, omitting null source fields."""
        data: dict[str, object] = {
            "content_sha256": self.content_sha256,
            "created_at": self.created_at,
            "importance": self.importance,
        }
        if self.source_session_id is not None:
            data["source_session_id"] = self.source_session_id
        if self.source_message_id is not None:
            data["source_message_id"] = self.source_message_id
        if self.run_id is not None:
            data["run_id"] = self.run_id
        if self.tags:
            data["tags"] = list(self.tags)
        return data

    @classmethod
    def from_dict(cls, raw: object) -> MemoryProvenance:
        """Parse a provenance mapping, refusing unknown fields."""
        if not isinstance(raw, dict):
            raise MemoryValidationError("memory provenance must be a JSON object")
        allowed = {
            "content_sha256",
            "created_at",
            "importance",
            "source_session_id",
            "source_message_id",
            "run_id",
            "tags",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise MemoryValidationError(f"memory provenance has unknown field {unknown[0]!r}")
        missing = sorted({"content_sha256", "created_at"} - set(raw))
        if missing:
            raise MemoryValidationError(f"memory provenance lacks required field {missing[0]!r}")
        importance = validate_importance(raw.get("importance", DEFAULT_IMPORTANCE))
        return cls(
            content_sha256=str(raw["content_sha256"]),
            created_at=validate_created_at(raw["created_at"]),
            importance=importance,
            source_session_id=_optional_str(raw.get("source_session_id"), "source_session_id"),
            source_message_id=_optional_str(raw.get("source_message_id"), "source_message_id"),
            run_id=_optional_str(raw.get("run_id"), "run_id"),
            tags=validate_tags(raw.get("tags")),
        )


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One durable long-term memory entry and its provenance."""

    id: str
    content: str
    provenance: MemoryProvenance

    def __post_init__(self) -> None:
        validate_memory_id(self.id)
        validate_content(self.content)
        if not isinstance(self.provenance, MemoryProvenance):
            raise MemoryValidationError("memory record provenance must be MemoryProvenance")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping."""
        return {
            "id": self.id,
            "content": self.content,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> MemoryRecord:
        """Parse one record mapping, refusing unknown fields."""
        if not isinstance(raw, dict):
            raise MemoryValidationError("memory record must be a JSON object")
        unknown = sorted(set(raw) - {"id", "content", "provenance"})
        if unknown:
            raise MemoryValidationError(f"memory record has unknown field {unknown[0]!r}")
        for name in ("id", "content", "provenance"):
            if name not in raw:
                raise MemoryValidationError(f"memory record lacks required field {name!r}")
        return cls(
            id=validate_memory_id(raw["id"]),
            content=validate_content(raw["content"]),
            provenance=MemoryProvenance.from_dict(raw["provenance"]),
        )

    def provenance_value(self, field: str) -> object:
        """Return one provenance field by name, for coverage-style checks."""
        if field == "tags":
            return self.provenance.tags
        if field == "importance":
            return self.provenance.importance
        if field == "created_at":
            return self.provenance.created_at
        if field == "content_sha256":
            return self.provenance.content_sha256
        if field == "source_session_id":
            return self.provenance.source_session_id
        if field == "source_message_id":
            return self.provenance.source_message_id
        if field == "run_id":
            return self.provenance.run_id
        raise MemoryValidationError(f"unknown provenance field {field!r}")


@dataclass(frozen=True, slots=True)
class MemorySearchHit:
    """One ranked search hit: the record plus its 1-based rank and score."""

    record: MemoryRecord
    rank: int
    score: float

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise MemoryValidationError("memory search rank must be positive")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping."""
        return {"record": self.record.to_dict(), "rank": self.rank, "score": self.score}


@dataclass(frozen=True, slots=True)
class MemoryBackendStatus:
    """Health and size snapshot of one long-term memory backend."""

    path: str
    schema_version: int
    count: int
    fts: bool
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping."""
        return {
            "path": self.path,
            "schema_version": self.schema_version,
            "count": self.count,
            "fts": self.fts,
            "size_bytes": self.size_bytes,
        }


@runtime_checkable
class MemoryBackend(Protocol):
    """The long-term memory surface the service depends on.

    Implementations must be idempotent on construction (creating the schema twice
    is fine) and must never need the user's real home directory: the default path
    is resolved from deeppyro.paths.deeppyro_home, which honors DEEPPYRO_HOME.
    """

    def write(
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
        """Persist one memory and return the stored record."""
        ...

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SEARCH_LIMIT,
        tags: Sequence[str] = (),
    ) -> tuple[MemorySearchHit, ...]:
        """Return ranked hits for one full-text query, best first."""
        ...

    def forget(self, memory_id: str) -> MemoryRecord:
        """Delete one record and return what was removed."""
        ...

    def status(self) -> MemoryBackendStatus:
        """Return the backend health/size snapshot."""
        ...


__all__ = [
    "DEFAULT_SEARCH_LIMIT",
    "MEMORY_ID_RE",
    "PROVENANCE_FIELDS",
    "SHA256_RE",
    "MemoryBackend",
    "MemoryBackendStatus",
    "MemoryProvenance",
    "MemoryRecord",
    "MemorySearchHit",
    "format_timestamp",
    "hash_content",
    "utc_now",
    "validate_content",
    "validate_created_at",
    "validate_importance",
    "validate_limit",
    "validate_memory_id",
    "validate_tags",
]
