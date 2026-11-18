"""Native message references over the folded session view (schema 1).

The v4 reference store (compact/refs.py) maps refs to session event seqs
because v4 reads a flat record log.  Native sessions already fold into a
SessionView of messages, so a native reference is keyed by message id: the ref
is a stable, human-writable handle (m00005) for one message id, and the folded
view decides ordering.  Refs are never rendered into prompt text; they are
shown to the user and echoed back by the model through the compress tool.

The sidecar is a rebuildable cache, exactly like the v4 one: a missing or torn
file only loses assignments, never session content.  It uses its own file
suffix so the v4 and native reference formats can coexist until the 0.3
deletion wave.

Pinned messages are not addressable: a message whose source is
CompactionSource is a summary checkpoint and may never be covered by a later
compaction (fold would reject a summary id inside covers).
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from deeppyro.compact.blocks import REFS_SUFFIX, CompactError
from deeppyro.core.ids import now_ms
from deeppyro.core.messages import CompactionSource, Message
from deeppyro.session.fold import SessionView
from deeppyro.session.layout import encode_segment

#: Width of the zero-padded numeric part of a reference.
REF_WIDTH = 5

#: Native reference sidecar suffix (placed beside the v4 .refs.jsonl file).
NATIVE_REFS_SUFFIX = ".native" + REFS_SUFFIX

_REF_RE = re.compile(r"^[mM](\d{1,9})$")


def _code(name: str) -> str:
    return "COMPACT_" + name


def canonical_ref(value: int | str) -> str:
    """Return the canonical m00005 form for *value*.

    Accepts an ordinal (5) or a loosely formatted reference (m5, M00005,
    " m00005 ").  Anything else is a tool-contract violation.
    """
    if isinstance(value, bool):
        raise CompactError(
            f"invalid message reference {value!r}; expected a form like m00005",
            code=_code("INVALID_REF"),
        )
    if isinstance(value, int):
        ordinal = value
    else:
        match = _REF_RE.fullmatch(str(value).strip())
        if match is None:
            raise CompactError(
                f"invalid message reference {value!r}; expected a form like m00005",
                code=_code("INVALID_REF"),
            )
        ordinal = int(match.group(1))
    if ordinal < 1:
        raise CompactError(
            f"invalid message reference {value!r}; expected a form like m00005",
            code=_code("INVALID_REF"),
        )
    return f"m{ordinal:0{REF_WIDTH}d}"


def ref_ordinal(ref: str) -> int:
    """Return the numeric ordinal of *ref* in canonical form."""
    return int(canonical_ref(ref)[1:])


@dataclass(frozen=True, slots=True)
class RefEntry:
    """One frozen mapping between a ref and a native message id."""

    ref: str
    message_id: str
    assigned_at: int

    def __post_init__(self) -> None:
        if not self.message_id:
            raise ValueError("ref entry message_id must be non-empty")
        object.__setattr__(self, "ref", canonical_ref(self.ref))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "message_id": self.message_id,
            "assigned_at": self.assigned_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RefEntry:
        ref = raw.get("ref")
        message_id = raw.get("message_id")
        assigned_at = raw.get("assigned_at")
        if not isinstance(ref, str) or not isinstance(message_id, str) or not message_id:
            raise ValueError("ref entry must contain ref and message_id strings")
        if isinstance(assigned_at, bool) or not isinstance(assigned_at, int) or assigned_at < 0:
            raise ValueError("ref entry assigned_at must be a non-negative integer")
        return cls(ref=ref, message_id=message_id, assigned_at=assigned_at)


@dataclass(frozen=True, slots=True)
class RefLocation:
    """A resolved reference: the ref, its message, and its view index."""

    ref: str
    message: Message
    index: int


@dataclass(frozen=True, slots=True)
class RefPlan:
    """The current reference assignment for one folded view.

    assignments is the addressable (ref, message) sequence in view order, with
    compaction summaries removed.  pending holds entries that
    RefStore.ensure() would append; entries holds every known mapping.
    """

    session_id: str
    assignments: tuple[tuple[str, Message], ...] = ()
    entries: Mapping[str, RefEntry] = field(default_factory=dict)
    by_message: Mapping[str, str] = field(default_factory=dict)
    pending: tuple[RefEntry, ...] = ()

    def ref_for(self, message_id: str) -> str | None:
        """Return the ref frozen for message_id, or None."""
        return self.by_message.get(message_id)

    def entry(self, ref: str) -> RefEntry | None:
        """Return the stored entry for ref, or None."""
        return self.entries.get(ref)

    def addressable(self) -> tuple[tuple[str, Message], ...]:
        """Return (ref, message) pairs currently addressable, in view order."""
        return self.assignments


def _view_index(view: SessionView) -> dict[str, int]:
    return {message.id: index for index, message in enumerate(view.messages)}


def addressable_messages(view: SessionView, plan: RefPlan) -> tuple[tuple[str, Message], ...]:
    """Return (ref, message) pairs for visible, non-summary messages.

    Ordered by view.messages; summaries are pinned and never addressable.
    """
    return tuple(
        (ref, message)
        for ref, message in plan.assignments
        if not isinstance(message.source, CompactionSource)
    )


class RefStore:
    """Append-only native reference sidecars keyed by message id."""

    def __init__(self, root: pathlib.Path | str) -> None:
        self.root = pathlib.Path(root)
        self._lock = threading.RLock()

    # -- paths -------------------------------------------------------------
    def path(self, session_id: str) -> pathlib.Path:
        """Path of the native reference sidecar for session_id."""
        return self.root / (encode_segment(session_id) + NATIVE_REFS_SUFFIX)

    # -- reading -----------------------------------------------------------
    def load(self, session_id: str) -> tuple[list[RefEntry], int]:
        """Return (entries, corrupt_line_count); first entry per ref wins."""
        path = self.path(session_id)
        entries: list[RefEntry] = []
        seen_refs: set[str] = set()
        corrupt = 0
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return entries, corrupt
        except OSError as exc:
            raise CompactError(
                f"cannot read native reference store {path}: {exc}",
                code=_code("REF_STORE_UNREADABLE"),
            ) from exc
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line.decode("utf-8"))
                entry = RefEntry.from_dict(raw)
            except (UnicodeDecodeError, TypeError, ValueError):
                corrupt += 1
                continue
            if entry.ref in seen_refs:
                continue
            seen_refs.add(entry.ref)
            entries.append(entry)
        return entries, corrupt

    def entries(self, session_id: str) -> list[RefEntry]:
        """Return every readable entry for session_id in file order."""
        return self.load(session_id)[0]

    # -- assignment --------------------------------------------------------
    def plan(self, session_id: str, view: SessionView) -> RefPlan:
        """Compute the reference assignment for view without writing.

        Existing entries keep their refs; unseen visible messages get the next
        free ordinals in view order.  Deterministic and idempotent.
        """
        stored = self.entries(session_id)
        entries: dict[str, RefEntry] = {entry.ref: entry for entry in stored}
        by_message: dict[str, str] = {}
        for entry in entries.values():
            previous = by_message.get(entry.message_id)
            if previous is not None and previous != entry.ref:
                raise CompactError(
                    f"message {entry.message_id!r} is frozen twice "
                    f"({previous} and {entry.ref}); the reference store is corrupt",
                    code=_code("REF_CONFLICT"),
                )
            by_message[entry.message_id] = entry.ref
        next_ordinal = 1
        for entry in entries.values():
            next_ordinal = max(next_ordinal, ref_ordinal(entry.ref) + 1)
        pending: list[RefEntry] = []
        assignments: list[tuple[str, Message]] = []
        stamp = now_ms()
        for message in view.messages:
            if isinstance(message.source, CompactionSource):
                continue
            ref = by_message.get(message.id)
            if ref is None:
                while True:
                    candidate = canonical_ref(next_ordinal)
                    next_ordinal += 1
                    if candidate not in entries:
                        break
                entry = RefEntry(ref=candidate, message_id=message.id, assigned_at=stamp)
                entries[candidate] = entry
                by_message[message.id] = candidate
                pending.append(entry)
                ref = candidate
            assignments.append((ref, message))
        return RefPlan(
            session_id=session_id,
            assignments=tuple(assignments),
            entries=dict(entries),
            by_message=dict(by_message),
            pending=tuple(pending),
        )

    def ensure(self, session_id: str, view: SessionView) -> RefPlan:
        """Plan the view assignment and persist any new entries.

        Returns the full plan (including the entries just written).
        """
        with self._lock:
            plan = self.plan(session_id, view)
            if plan.pending:
                self.register(session_id, plan.pending)
            return plan

    def register(self, session_id: str, entries: Iterable[RefEntry]) -> None:
        """Append entries; identical mappings are a no-op, conflicts raise."""
        with self._lock:
            stored = self.entries(session_id)
            by_ref: dict[str, RefEntry] = {entry.ref: entry for entry in stored}
            by_message: dict[str, str] = {}
            for entry in stored:
                previous = by_message.get(entry.message_id)
                if previous is not None and previous != entry.ref:
                    raise CompactError(
                        f"message {entry.message_id!r} is frozen twice "
                        f"({previous} and {entry.ref}); the reference store is corrupt",
                        code=_code("REF_CONFLICT"),
                    )
                by_message[entry.message_id] = entry.ref
            appended: list[RefEntry] = []
            for entry in entries:
                existing = by_ref.get(entry.ref)
                if existing is not None:
                    if existing.message_id == entry.message_id:
                        continue
                    raise CompactError(
                        f"message reference {entry.ref} is already frozen: "
                        f"expected message {existing.message_id}, got {entry.message_id}",
                        code=_code("REF_CONFLICT"),
                    )
                previous_ref = by_message.get(entry.message_id)
                if previous_ref is not None:
                    raise CompactError(
                        f"message reference {entry.ref} is already frozen: "
                        f"message {entry.message_id} is {previous_ref}",
                        code=_code("REF_CONFLICT"),
                    )
                by_ref[entry.ref] = entry
                by_message[entry.message_id] = entry.ref
                appended.append(entry)
            if not appended:
                return
            path = self.path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    for entry in appended:
                        payload = json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":"))
                        handle.write(payload + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise CompactError(
                    f"cannot append to native reference store {path}: {exc}",
                    code=_code("REF_STORE_WRITE_FAILED"),
                ) from exc

    # -- transaction helpers ----------------------------------------------
    def offsets(self, session_id: str) -> int:
        """Current byte size of the reference file (for rollback)."""
        try:
            return self.path(session_id).stat().st_size
        except FileNotFoundError:
            return 0

    def rollback(self, session_id: str, offset: int) -> None:
        """Truncate the sidecar back to offset bytes; missing files are ignored."""
        path = self.path(session_id)
        try:
            with path.open("r+b") as handle:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
        except FileNotFoundError:
            return


def resolve_refs(
    view: SessionView,
    plan: RefPlan,
    refs: Sequence[str],
) -> tuple[RefLocation, ...]:
    """Resolve model-supplied refs to visible messages.

    Raises CompactError with a stable code: invalid syntax
    (COMPACT_INVALID_REF), a ref listed twice (COMPACT_REF_CONFLICT), a ref that
    is not frozen (COMPACT_REF_NOT_FOUND), or a frozen ref whose message is no
    longer visible after compaction (COMPACT_REF_NOT_ON_SURFACE).
    """
    index_by_id = _view_index(view)
    resolved: list[RefLocation] = []
    seen: set[str] = set()
    addressable_count = len(plan.assignments)
    for raw in refs:
        ref = canonical_ref(raw)
        if ref in seen:
            raise CompactError(
                f"message reference {ref} is listed twice in one compress call",
                code=_code("REF_CONFLICT"),
            )
        seen.add(ref)
        entry = plan.entry(ref)
        if entry is None:
            raise CompactError(
                f"message reference {ref} is out of range: "
                f"the session has {addressable_count} addressable message(s)",
                code=_code("REF_NOT_FOUND"),
            )
        index = index_by_id.get(entry.message_id)
        if index is None:
            raise CompactError(
                f"message {ref} is no longer on the visible surface; "
                "refs point at already-compressed messages",
                code=_code("REF_NOT_ON_SURFACE"),
            )
        resolved.append(RefLocation(ref=ref, message=view.messages[index], index=index))
    return tuple(resolved)


def pinned_prefix_end(view: SessionView) -> int:
    """Return the index just after the last pinned compaction summary."""
    end = 0
    for index, message in enumerate(view.messages):
        if isinstance(message.source, CompactionSource):
            end = index + 1
    return end


__all__ = [
    "NATIVE_REFS_SUFFIX",
    "REF_WIDTH",
    "RefEntry",
    "RefLocation",
    "RefPlan",
    "RefStore",
    "addressable_messages",
    "canonical_ref",
    "pinned_prefix_end",
    "ref_ordinal",
    "resolve_refs",
]
