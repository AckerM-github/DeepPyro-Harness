"""Atomic JSON storage and markdown projections for runtime memory.

The JSON file memories.json is the source of truth. USER.md and MEMORY.md are
human-readable projections rebuilt after every successful mutation; the runtime
never reads them back, so a stale projection cannot change what the model sees
(snapshots always render from the JSON entries).

Every write is atomic per file: the new bytes go to a temporary file in the
same directory, are flushed and fsynced, then moved into place with os.replace.
A failed write leaves the previous file intact.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deeppyro.memory.errors import (
    MemoryAmbiguousMatchError,
    MemoryCapacityError,
    MemoryCorruptError,
    MemoryEntryNotFoundError,
    MemoryStoreError,
    MemoryValidationError,
)
from deeppyro.memory.types import (
    CAPACITY_LIMITS,
    DEFAULT_IMPORTANCE,
    IMPORTANCES,
    PROJECTION_FILENAMES,
    STORE_FILENAME,
    STORE_VERSION,
    TARGETS,
    MemoryEntry,
    MemoryImportance,
    MemoryReceipt,
    MemorySnapshot,
    MemoryStatus,
    MemoryTarget,
)
from deeppyro.paths import deeppyro_home

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _projection_text(entries: Iterable[MemoryEntry], target: MemoryTarget) -> str:
    """Render one projection: entries separated by one blank line, LF endings."""
    return "\n\n".join(entry.content for entry in entries if entry.target == target)


@dataclass(frozen=True, slots=True)
class _PersistedState:
    revision: int
    entries: tuple[MemoryEntry, ...]


class MemoryStore:
    """The memories.json store plus its USER.md and MEMORY.md projections.

    root is the memory directory; tests pass an explicit temporary directory.
    The default is deeppyro_home()/memory, resolved lazily at construction so
    DEEPPYRO_HOME overrides work.
    """

    def __init__(self, root: Path | None = None, *, clock: Clock | None = None) -> None:
        self._root = Path(root) if root is not None else deeppyro_home() / "memory"
        self._clock = clock if clock is not None else _utc_now

    # -- locations ---------------------------------------------------------
    @property
    def root(self) -> Path:
        return self._root

    @property
    def data_path(self) -> Path:
        return self._root / STORE_FILENAME

    def projection_path(self, target: MemoryTarget) -> Path:
        return self._root / PROJECTION_FILENAMES[self.check_target(target)]

    # -- validation --------------------------------------------------------
    @staticmethod
    def check_target(target: str) -> MemoryTarget:
        if target not in TARGETS:
            raise MemoryValidationError(
                f"unknown memory target {target!r}; expected one of: {', '.join(TARGETS)}"
            )
        return target

    @staticmethod
    def check_importance(importance: str) -> MemoryImportance:
        if importance not in IMPORTANCES:
            raise MemoryValidationError(
                f"unknown importance {importance!r}; expected one of: {', '.join(IMPORTANCES)}"
            )
        return importance

    @staticmethod
    def check_content(content: str) -> str:
        if not isinstance(content, str):
            raise MemoryValidationError("content must be a string")
        stripped = content.strip()
        if not stripped:
            raise MemoryValidationError("content must be a non-empty string")
        return stripped

    # -- reads -------------------------------------------------------------
    @property
    def revision(self) -> int:
        return self._read().revision

    def entries(self, target: MemoryTarget) -> tuple[MemoryEntry, ...]:
        checked = self.check_target(target)
        return tuple(entry for entry in self._read().entries if entry.target == checked)

    def snapshot(self, target: MemoryTarget) -> MemorySnapshot:
        checked = self.check_target(target)
        entries = self.entries(checked)
        text = _projection_text(entries, checked)
        return MemorySnapshot(
            target=checked,
            entries=entries,
            text=text,
            bytes=len(text.encode("utf-8")),
            limit=CAPACITY_LIMITS[checked],
        )

    def status(self) -> MemoryStatus:
        state = self._read()
        counts = {target: sum(1 for entry in state.entries if entry.target == target) for target in TARGETS}
        sizes = {target: len(_projection_text(state.entries, target).encode("utf-8")) for target in TARGETS}
        return MemoryStatus(
            revision=state.revision,
            total=len(state.entries),
            entries=counts,
            bytes=sizes,
            limits=dict(CAPACITY_LIMITS),
            data_path=str(self.data_path),
            projections={target: str(self.projection_path(target)) for target in TARGETS},
        )

    def resolve(self, target: MemoryTarget, old_text: str) -> MemoryEntry:
        """Find the single entry in target whose content contains old_text."""
        checked = self.check_target(target)
        if not isinstance(old_text, str):
            raise MemoryValidationError("old_text must be a string")
        needle = old_text.strip()
        if not needle:
            raise MemoryValidationError("old_text must be a non-empty string")
        matches = [entry for entry in self.entries(checked) if needle in entry.content]
        if not matches:
            raise MemoryEntryNotFoundError(f"no memory entry in target {checked!r} contains {needle!r}")
        if len(matches) > 1:
            raise MemoryAmbiguousMatchError(
                f"{len(matches)} memory entries in target {checked!r} contain {needle!r}; "
                "use a longer unique substring"
            )
        return matches[0]

    # -- mutations ---------------------------------------------------------
    def remember(
        self,
        target: MemoryTarget,
        content: str,
        importance: MemoryImportance = DEFAULT_IMPORTANCE,
    ) -> MemoryReceipt:
        checked_target = self.check_target(target)
        checked_importance = self.check_importance(importance)
        checked_content = self.check_content(content)
        state = self._read()
        now = _timestamp(self._clock())
        entry = MemoryEntry(
            id=uuid.uuid4().hex,
            content=checked_content,
            target=checked_target,
            importance=checked_importance,
            created_at=now,
            updated_at=now,
        )
        entries = (*state.entries, entry)
        revision = self._commit(state.revision + 1, entries, checked_target)
        return MemoryReceipt(
            id=entry.id,
            action="add",
            target=checked_target,
            revision=revision,
            entry=entry,
        )

    def replace(
        self,
        target: MemoryTarget,
        entry_id: str,
        content: str,
        importance: MemoryImportance | None = None,
    ) -> MemoryReceipt:
        checked_target = self.check_target(target)
        checked_content = self.check_content(content)
        checked_importance = self.check_importance(importance) if importance is not None else None
        state = self._read()
        index = self._index_of(state.entries, checked_target, entry_id)
        previous = state.entries[index]
        now = _timestamp(self._clock())
        updated = MemoryEntry(
            id=previous.id,
            content=checked_content,
            target=checked_target,
            importance=checked_importance if checked_importance is not None else previous.importance,
            created_at=previous.created_at,
            updated_at=now,
        )
        entries = list(state.entries)
        entries[index] = updated
        revision = self._commit(state.revision + 1, tuple(entries), checked_target)
        return MemoryReceipt(
            id=updated.id,
            action="replace",
            target=checked_target,
            revision=revision,
            entry=updated,
        )

    def forget(self, target: MemoryTarget, entry_id: str) -> MemoryReceipt:
        checked_target = self.check_target(target)
        state = self._read()
        index = self._index_of(state.entries, checked_target, entry_id)
        removed = state.entries[index]
        entries = list(state.entries)
        del entries[index]
        revision = self._commit(state.revision + 1, tuple(entries), checked_target)
        return MemoryReceipt(
            id=removed.id,
            action="remove",
            target=checked_target,
            revision=revision,
            entry=removed,
        )

    # -- persistence -------------------------------------------------------
    def _read(self) -> _PersistedState:
        path = self.data_path
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _PersistedState(revision=0, entries=())
        except OSError as exc:
            raise MemoryStoreError(f"cannot read memory store {path}: {exc}") from exc
        if not raw.strip():
            raise MemoryCorruptError(f"memory store {path} is empty")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemoryCorruptError(f"memory store {path} is not valid JSON: {exc}") from exc
        return _parse_payload(payload, path)

    def _commit(
        self,
        revision: int,
        entries: tuple[MemoryEntry, ...],
        mutated_target: MemoryTarget,
    ) -> int:
        self._check_capacity(entries, mutated_target)
        self._write_state(revision, entries)
        self._write_projections(entries)
        return revision

    def _check_capacity(self, entries: tuple[MemoryEntry, ...], mutated_target: MemoryTarget) -> None:
        filename = PROJECTION_FILENAMES[mutated_target]
        limit = CAPACITY_LIMITS[mutated_target]
        text = _projection_text(entries, mutated_target)
        size = len(text.encode("utf-8"))
        if size > limit:
            count = sum(1 for entry in entries if entry.target == mutated_target)
            raise MemoryCapacityError(
                f"cannot update {filename}: {count} entries would need {size} bytes, "
                f"limit is {limit} bytes; shorten or remove entries"
            )

    def _write_state(self, revision: int, entries: tuple[MemoryEntry, ...]) -> None:
        payload = {
            "version": STORE_VERSION,
            "revision": revision,
            "entries": [entry.to_dict() for entry in entries],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self._atomic_write(self.data_path, text)

    def _write_projections(self, entries: tuple[MemoryEntry, ...]) -> None:
        for target in TARGETS:
            self._atomic_write(self.projection_path(target), _projection_text(entries, target))

    def _index_of(
        self,
        entries: tuple[MemoryEntry, ...],
        target: MemoryTarget,
        entry_id: str,
    ) -> int:
        for index, entry in enumerate(entries):
            if entry.target == target and entry.id == entry_id:
                return index
        raise MemoryEntryNotFoundError(f"no memory entry {entry_id!r} in target {target!r}")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=path.name + ".",
                suffix=".tmp",
                dir=str(path.parent),
            )
            temp_path = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except OSError as exc:
            raise MemoryStoreError(f"cannot write memory file {path}: {exc}") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def _parse_payload(payload: Any, path: Path) -> _PersistedState:
    if not isinstance(payload, dict):
        raise MemoryCorruptError(f"memory store {path} must contain a JSON object")
    version = payload.get("version")
    if version != STORE_VERSION:
        raise MemoryCorruptError(
            f"unsupported memory store version {version!r} in {path}; expected {STORE_VERSION}"
        )
    revision = payload.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise MemoryCorruptError(f"memory store {path} has an invalid revision: {revision!r}")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise MemoryCorruptError(f"memory store {path} must contain an entries array")
    entries: list[MemoryEntry] = []
    seen: set[str] = set()
    for position, item in enumerate(raw_entries):
        entry = _parse_entry(item, path, position)
        if entry.id in seen:
            raise MemoryCorruptError(f"memory store {path} has duplicate entry id {entry.id!r}")
        seen.add(entry.id)
        entries.append(entry)
    return _PersistedState(revision=revision, entries=tuple(entries))


def _parse_entry(item: Any, path: Path, position: int) -> MemoryEntry:
    if not isinstance(item, dict):
        raise MemoryCorruptError(f"memory store {path} entry {position} must be a JSON object")
    entry_id = item.get("id")
    content = item.get("content")
    target = item.get("target")
    importance = item.get("importance", DEFAULT_IMPORTANCE)
    created_at = item.get("created_at", "")
    updated_at = item.get("updated_at", "")
    if not isinstance(entry_id, str) or not entry_id:
        raise MemoryCorruptError(f"memory store {path} entry {position} is missing a string id")
    if not isinstance(content, str) or not content.strip():
        raise MemoryCorruptError(f"memory store {path} entry {position} is missing string content")
    if target not in TARGETS:
        raise MemoryCorruptError(f"memory store {path} entry {position} has unknown target {target!r}")
    if importance not in IMPORTANCES:
        raise MemoryCorruptError(
            f"memory store {path} entry {position} has unknown importance {importance!r}"
        )
    if not isinstance(created_at, str) or not isinstance(updated_at, str):
        raise MemoryCorruptError(f"memory store {path} entry {position} has invalid timestamps")
    return MemoryEntry(
        id=entry_id,
        content=content,
        target=target,
        importance=importance,
        created_at=created_at,
        updated_at=updated_at,
    )


__all__ = ["Clock", "MemoryStore"]
