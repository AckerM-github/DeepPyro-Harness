"""Durable storage for compress blocks and their message references.

A compression block is one summary the model wrote for a contiguous range of
model-visible messages.  Blocks live outside the session log so they can be
searched and decompressed later without replaying the (possibly huge) session
file.  Each session gets two append-only JSONL files under the block root::

    <root>/<encodeSegment(sessionId)>.jsonl        # BlockRecord, one per line
    <root>/<encodeSegment(sessionId)>.refs.jsonl   # RefEntry, one per line

The format is versioned (see BLOCK_STORE_VERSION); every line is compact JSON
terminated by a newline and fsynced before the call returns.  Readers are
tolerant: a torn or corrupt line is skipped and counted, never fatal, because
losing a summary must not make a session unreadable.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from deeppyro.core.errors import SessionError
from deeppyro.core.ids import now_ms
from deeppyro.session.layout import encode_segment

#: On-disk version of the block/ref records.  Bump only on incompatible change.
BLOCK_STORE_VERSION = 1

#: File suffix shared by every per-session block file.
BLOCK_SUFFIX = ".jsonl"
#: File suffix for the reference sidecar.
REFS_SUFFIX = ".refs.jsonl"

_ERROR_BASE = "COMPACT_"


class CompactError(SessionError):
    """A compress failure with a stable, machine-readable code.

    The code lives in this package (not core.errors) because it describes a
    compress-tool contract violation rather than a corrupt session log.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "COMPACT_ERROR",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail: dict[str, Any] = dict(detail or {})

    def __reduce__(self) -> tuple[type[CompactError], tuple[str], dict[str, Any]]:
        return (type(self), (str(self),), {"code": self.code, "detail": self.detail})


def _code(name: str) -> str:
    return _ERROR_BASE + name


class BlockRecord:
    """One stored summary and the message range it replaces.

    start_ref/end_ref are the stable m references shown to the model (for
    example m00005); start_seq/end_seq are the corresponding session event seqs,
    recorded so a reader can locate the original events without re-resolving
    references that may have shifted since.
    """

    __slots__ = (
        "created_at",
        "end_ref",
        "end_seq",
        "id",
        "session_id",
        "start_ref",
        "start_seq",
        "summary",
        "title",
    )

    def __init__(
        self,
        *,
        id: str,
        session_id: str,
        start_ref: str,
        end_ref: str,
        start_seq: int,
        end_seq: int,
        title: str,
        summary: str,
        created_at: int,
    ) -> None:
        if not id or not id.startswith("b") or not id[1:].isdigit():
            raise ValueError("block id must look like 'b1'")
        if not session_id:
            raise ValueError("block session_id must be non-empty")
        if not start_ref or not end_ref:
            raise ValueError("block range refs must be non-empty")
        if start_seq < 0 or end_seq < start_seq:
            raise ValueError("block seq range must be non-negative and ordered")
        if not isinstance(summary, str):
            raise TypeError("block summary must be a string")
        self.id = id
        self.session_id = session_id
        self.start_ref = start_ref
        self.end_ref = end_ref
        self.start_seq = start_seq
        self.end_seq = end_seq
        self.title = title
        self.summary = summary
        self.created_at = created_at

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON object written to disk."""
        return {
            "version": BLOCK_STORE_VERSION,
            "id": self.id,
            "sessionId": self.session_id,
            "range": {"start": self.start_ref, "end": self.end_ref},
            "startSeq": self.start_seq,
            "endSeq": self.end_seq,
            "title": self.title,
            "summary": self.summary,
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BlockRecord:
        """Validate and decode one stored line; raises ValueError when invalid."""
        if not isinstance(raw, Mapping):
            raise TypeError("block record must be an object")
        block_range = raw.get("range")
        if not isinstance(block_range, Mapping):
            raise TypeError("block record lacks range")
        created = raw.get("createdAt")
        if created is None:
            created = 0
        if isinstance(created, bool) or not isinstance(created, int) or created < 0:
            raise ValueError("block record createdAt must be a non-negative integer")
        return cls(
            id=str(raw.get("id", "")),
            session_id=str(raw.get("sessionId", "")),
            start_ref=str(block_range.get("start", "")),
            end_ref=str(block_range.get("end", "")),
            start_seq=_as_int(raw.get("startSeq")),
            end_seq=_as_int(raw.get("endSeq")),
            title=str(raw.get("title", "")),
            summary=str(raw.get("summary", "")),
            created_at=created,
        )

    def _key(self) -> tuple[Any, ...]:
        return (
            self.id,
            self.session_id,
            self.start_ref,
            self.end_ref,
            self.start_seq,
            self.end_seq,
            self.title,
            self.summary,
            self.created_at,
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, BlockRecord) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"BlockRecord(id={self.id!r}, session_id={self.session_id!r}, "
            f"range={self.start_ref!r}..{self.end_ref!r})"
        )


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative integer")
    return value


class BlockStore:
    """Append-only per-session block files under one root directory."""

    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self._lock = threading.RLock()

    # -- paths -------------------------------------------------------------
    def path(self, session_id: str) -> pathlib.Path:
        """Path of the block file for *session_id*."""
        return self.root / (encode_segment(session_id) + BLOCK_SUFFIX)

    def refs_path(self, session_id: str) -> pathlib.Path:
        """Path of the reference sidecar for *session_id*."""
        return self.root / (encode_segment(session_id) + REFS_SUFFIX)

    # -- reading -----------------------------------------------------------
    def load(self, session_id: str) -> tuple[list[BlockRecord], int]:
        """Return (blocks, corrupt_line_count) for one session.

        Missing files yield an empty list.  Invalid lines are counted and
        skipped; a valid line after a corrupt one is still returned.
        """
        path = self.path(session_id)
        records: list[BlockRecord] = []
        corrupt = 0
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return records, corrupt
        except OSError as exc:
            raise CompactError(
                f"cannot read block store {path}: {exc}",
                code=_code("BLOCK_STORE_UNREADABLE"),
            ) from exc
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line.decode("utf-8"))
                records.append(BlockRecord.from_dict(raw))
            except (UnicodeDecodeError, TypeError, ValueError):
                corrupt += 1
        return records, corrupt

    def list(self, session_id: str) -> list[BlockRecord]:
        """Return every readable block for *session_id* in file order."""
        return self.load(session_id)[0]

    def get(self, session_id: str, block_id: str) -> BlockRecord | None:
        """Return one block by id, or None."""
        for record in self.list(session_id):
            if record.id == block_id:
                return record
        return None

    # -- writing -----------------------------------------------------------
    def add(
        self,
        session_id: str,
        *,
        start_ref: str,
        end_ref: str,
        start_seq: int,
        end_seq: int,
        title: str,
        summary: str,
        created_at: int | None = None,
        block_id: str | None = None,
    ) -> BlockRecord:
        """Append one block, assigning the next bN id, and return it."""
        with self._lock:
            existing, _ = self.load(session_id)
            if block_id is None:
                block_id = self._next_id(existing)
            elif any(record.id == block_id for record in existing):
                raise CompactError(
                    f"block id {block_id!r} already exists for session {session_id!r}",
                    code=_code("BLOCK_CONFLICT"),
                )
            record = BlockRecord(
                id=block_id,
                session_id=session_id,
                start_ref=start_ref,
                end_ref=end_ref,
                start_seq=start_seq,
                end_seq=end_seq,
                title=title,
                summary=summary,
                created_at=now_ms() if created_at is None else created_at,
            )
            self._append_line(self.path(session_id), record.to_dict())
            return record

    @staticmethod
    def _next_id(existing: Iterable[BlockRecord]) -> str:
        highest = 0
        for record in existing:
            try:
                highest = max(highest, int(record.id[1:]))
            except ValueError:  # pragma: no cover - BlockRecord validates its id
                continue
        return f"b{highest + 1}"

    def next_id(self, session_id: str) -> str:
        """Return the id the next add() call would use (without writing)."""
        with self._lock:
            existing, _ = self.load(session_id)
            return self._next_id(existing)

    def _append_line(self, path: pathlib.Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise CompactError(
                f"cannot append to block store {path}: {exc}",
                code=_code("BLOCK_STORE_WRITE_FAILED"),
            ) from exc

    # -- transaction helpers ----------------------------------------------
    def offsets(self, session_id: str) -> tuple[int, int]:
        """Current byte sizes of the block and ref files (for rollback)."""
        return _size(self.path(session_id)), _size(self.refs_path(session_id))

    def rollback(self, session_id: str, offsets: tuple[int, int]) -> None:
        """Truncate both files back to *offsets*, dropping appended lines.

        Used when a compress transaction fails before any session event was
        appended.  Missing files are ignored.
        """
        _truncate(self.path(session_id), offsets[0])
        _truncate(self.refs_path(session_id), offsets[1])


def _size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _truncate(path: pathlib.Path, size: int) -> None:
    try:
        with path.open("r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return


__all__ = [
    "BLOCK_STORE_VERSION",
    "BLOCK_SUFFIX",
    "REFS_SUFFIX",
    "BlockRecord",
    "BlockStore",
    "CompactError",
]
