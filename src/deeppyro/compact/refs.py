"""Stable m references and their durable mapping to session event seqs.

The model addresses conversation ranges with refs like m00005.  A ref is
resolved once (from the current derived message list) and then frozen in a
per-session sidecar so later calls keep pointing at the same event even after
the surface changed.  References that are already registered win over any
recomputation; a registered ref never silently moves.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from deeppyro.compact.blocks import REFS_SUFFIX, CompactError
from deeppyro.core.ids import now_ms
from deeppyro.core.wire import LogRecord, Message
from deeppyro.session.derive import event_message, fold_surface
from deeppyro.session.layout import encode_segment

REF_WIDTH = 5
_REF_RE = re.compile(r"^[mM](\d{1,9})$")


def canonical_ref(value: str | int) -> str:
    """Normalize an m reference to the canonical zero-padded form.

    Accepts ints, 'm5', 'M00005' and surrounding whitespace.  Raises
    CompactError with COMPACT_INVALID_INPUT for anything else.
    """
    ordinal: int
    if isinstance(value, bool):
        raise _invalid_ref(value)
    if isinstance(value, int):
        ordinal = value
    elif isinstance(value, str):
        match = _REF_RE.match(value.strip())
        if match is None:
            raise _invalid_ref(value)
        ordinal = int(match.group(1))
    else:
        raise _invalid_ref(value)
    if ordinal < 1:
        raise _invalid_ref(value)
    return f"m{ordinal:0{REF_WIDTH}d}"


def ref_ordinal(ref: str) -> int:
    """Return the 1-based ordinal encoded in a canonical reference."""
    match = _REF_RE.match(ref)
    if match is None:
        raise _invalid_ref(ref)
    return int(match.group(1))


def _invalid_ref(value: object) -> CompactError:
    return CompactError(
        f"invalid message reference {value!r}; expected a form like m00005",
        code="COMPACT_INVALID_REF",
        detail={"value": str(value)},
    )


@dataclass(frozen=True, slots=True)
class RefEntry:
    """One frozen ref-to-event mapping."""

    ref: str
    seq: int
    message_id: str
    assigned_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "seq": self.seq,
            "messageId": self.message_id,
            "assignedAt": self.assigned_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RefEntry:
        if not isinstance(raw, Mapping):
            raise TypeError("ref entry must be an object")
        ref = canonical_ref(str(raw.get("ref", "")))
        seq = raw.get("seq")
        assigned = raw.get("assignedAt", 0)
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError("ref entry seq must be a non-negative integer")
        if isinstance(assigned, bool) or not isinstance(assigned, int) or assigned < 0:
            raise ValueError("ref entry assignedAt must be a non-negative integer")
        message_id = raw.get("messageId", "")
        if message_id is not None and not isinstance(message_id, str):
            raise ValueError("ref entry messageId must be a string")
        return cls(ref=ref, seq=seq, message_id=message_id or "", assigned_at=assigned)


@dataclass(frozen=True, slots=True)
class ResolvedRef:
    """A ref together with the event seq it currently denotes."""

    ref: str
    seq: int
    message_id: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class RefResolution:
    """Result of resolving a batch of refs before any write happens.

    resolved contains every requested ref in request order.  pending holds the
    entries that were not in the sidecar yet and still need to be registered;
    call RefStore.register (or use ensure_refs) to freeze them.
    """

    resolved: tuple[ResolvedRef, ...]
    pending: tuple[RefEntry, ...]

    def by_ref(self) -> dict[str, ResolvedRef]:
        return {entry.ref: entry for entry in self.resolved}


class RefStore:
    """Append-only per-session ref sidecar next to the block files."""

    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self._lock = threading.RLock()

    def path(self, session_id: str) -> pathlib.Path:
        """Path of the refs sidecar for *session_id*."""
        return self.root / (encode_segment(session_id) + REFS_SUFFIX)

    def load(self, session_id: str) -> tuple[dict[str, RefEntry], int]:
        """Return (refs, corrupt_line_count); the first entry for a ref wins."""
        path = self.path(session_id)
        entries: dict[str, RefEntry] = {}
        corrupt = 0
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return entries, corrupt
        except OSError as exc:
            raise CompactError(
                f"cannot read ref store {path}: {exc}",
                code="COMPACT_REF_STORE_UNREADABLE",
            ) from exc
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                entry = RefEntry.from_dict(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, TypeError, ValueError):
                corrupt += 1
                continue
            entries.setdefault(entry.ref, entry)
        return entries, corrupt

    def entries(self, session_id: str) -> list[RefEntry]:
        """Every readable ref entry for *session_id*, in first-seen order."""
        return list(self.load(session_id)[0].values())

    def register(self, session_id: str, entries: Sequence[RefEntry]) -> list[RefEntry]:
        """Append ref entries, rejecting conflicts with frozen refs.

        Re-registering the same ref with the same seq is a no-op.  A ref that
        already maps to a different seq is a COMPACT_REF_CONFLICT: a frozen ref
        must never move.
        """
        if not entries:
            return []
        with self._lock:
            existing, _ = self.load(session_id)
            new_entries: list[RefEntry] = []
            seen: set[str] = set()
            for entry in entries:
                canon = canonical_ref(entry.ref)
                if canon in seen:
                    raise _conflict(canon, "listed twice in one registration")
                seen.add(canon)
                prior = existing.get(canon)
                if prior is not None:
                    if prior.seq != entry.seq:
                        raise _conflict(canon, f"already frozen at seq {prior.seq}, got {entry.seq}")
                    continue
                new_entries.append(
                    RefEntry(
                        ref=canon,
                        seq=entry.seq,
                        message_id=entry.message_id,
                        assigned_at=entry.assigned_at,
                    )
                )
            if not new_entries:
                return []
            path = self.path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [
                json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
                for entry in new_entries
            ]
            try:
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.writelines(lines)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise CompactError(
                    f"cannot append to ref store {path}: {exc}",
                    code="COMPACT_REF_STORE_WRITE_FAILED",
                ) from exc
            return new_entries

    def offsets(self, session_id: str) -> int:
        """Current byte size of the ref file (for rollback)."""
        try:
            return self.path(session_id).stat().st_size
        except FileNotFoundError:
            return 0

    def rollback(self, session_id: str, offset: int) -> None:
        """Truncate the ref file back to *offset*."""
        path = self.path(session_id)
        try:
            with path.open("r+b") as handle:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
        except FileNotFoundError:
            return


def _conflict(ref: str, reason: str) -> CompactError:
    return CompactError(
        f"message reference {ref} is already frozen: {reason}",
        code="COMPACT_REF_CONFLICT",
        detail={"ref": ref},
    )


def addressable_messages(records: Sequence[LogRecord], *, strict: bool = True) -> list[tuple[int, Message]]:
    """Return (seq, message) for every ref-addressable derived message.

    The system prompt is not addressable: it is a fixed head that compression
    must never rewrite, and the reference harness numbers messages from the
    first non-system message.  Developer retirement placeholders and other
    nodes that project to no message are skipped.
    """
    nodes = fold_surface(records, strict=strict)
    output: list[tuple[int, Message]] = []
    for node in nodes:
        message = event_message(node)
        if message is None or message.role == "system":
            continue
        output.append((node.seq, message))
    return output


def resolve_refs(
    session: Any,
    refs: Sequence[str | int],
    store: RefStore,
    *,
    strict: bool | None = None,
) -> RefResolution:
    """Resolve refs without writing anything.

    Registered refs come from the sidecar; unregistered refs are mapped from
    the current derived message list (m00001 is the first non-system message)
    and returned as pending entries.  The caller freezes them only after the
    whole transaction validated.
    """
    session_id = session.session_id
    canon_refs: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        canon = canonical_ref(ref)
        if canon in seen:
            raise _conflict(canon, "listed twice in one compress call")
        seen.add(canon)
        canon_refs.append(canon)
    existing, _ = store.load(session_id)
    available: list[tuple[int, Message]] | None = None
    resolved: list[ResolvedRef] = []
    pending: list[RefEntry] = []
    for canon in canon_refs:
        entry = existing.get(canon)
        if entry is not None:
            resolved.append(
                ResolvedRef(ref=canon, seq=entry.seq, message_id=entry.message_id, ordinal=ref_ordinal(canon))
            )
            continue
        if available is None:
            is_strict = (not getattr(session, "trimmed", False)) if strict is None else strict
            available = addressable_messages(session.records, strict=is_strict)
        ordinal = ref_ordinal(canon)
        if ordinal > len(available):
            raise CompactError(
                f"message reference {canon} is out of range: the session has "
                f"{len(available)} addressable message(s)",
                code="COMPACT_REF_NOT_FOUND",
                detail={"ref": canon, "available": len(available)},
            )
        seq, message = available[ordinal - 1]
        resolved.append(ResolvedRef(ref=canon, seq=seq, message_id=message.id, ordinal=ordinal))
        pending.append(RefEntry(ref=canon, seq=seq, message_id=message.id, assigned_at=now_ms()))
    return RefResolution(resolved=tuple(resolved), pending=tuple(pending))


def ensure_refs(
    session: Any,
    refs: Sequence[str | int],
    store: RefStore,
    *,
    strict: bool | None = None,
) -> RefResolution:
    """Resolve refs and immediately freeze the pending ones in the sidecar."""
    resolution = resolve_refs(session, refs, store, strict=strict)
    if resolution.pending:
        store.register(session.session_id, resolution.pending)
    return resolution


__all__ = [
    "REF_WIDTH",
    "RefEntry",
    "RefResolution",
    "RefStore",
    "ResolvedRef",
    "addressable_messages",
    "canonical_ref",
    "ensure_refs",
    "ref_ordinal",
    "resolve_refs",
]
