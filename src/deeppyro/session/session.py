"""One open session log: durable appends, message derivation, and TUI stats."""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Callable, Mapping
from typing import Any

from deeppyro.core.errors import SessionError
from deeppyro.core.ids import now_ms
from deeppyro.core.wire import LogRecord, Message, SessionHeader
from deeppyro.session import writer
from deeppyro.session.derive import (
    APPEND,
    REPLACE,
    fold_surface,
    plan_surface_transition,
)
from deeppyro.session.derive import (
    derive_messages as _derive_messages,
)
from deeppyro.session.events import SURFACE_EVENTS, normalize_event


class Session:
    """An append-only session v4 log.

    The in-memory records mirror the durable file exactly.  Appends validate the
    record against the canonical surface fold before writing, persist it as one
    JSON line (or one zstd frame), and only then update memory, so a failed write
    never leaves a record that a reader cannot see.
    """

    def __init__(
        self,
        *,
        path: pathlib.Path,
        header: SessionHeader,
        records: list[LogRecord] | None = None,
        truncated: bool = False,
        trimmed: bool = False,
        zstd: bool = False,
    ) -> None:
        self.path = pathlib.Path(path)
        self.header = header
        self.records: list[LogRecord] = list(records or [])
        self.truncated = truncated
        self.trimmed = trimmed
        self.zstd = zstd
        self._closed = False
        self._surface: list[LogRecord] | None = None
        self._append_observers: list[Callable[[Session, LogRecord], None]] = []

    @property
    def session_id(self) -> str:
        """Stable session id from the header."""
        return self.header.id

    @property
    def last_seq(self) -> int:
        """Seq of the newest record, or -1 for an empty session."""
        return self.records[-1].seq if self.records else -1

    def append(self, type: str, data: dict[str, Any], **envelope: object) -> int:
        """Append one validated record, persist it, and return its seq."""
        if self._closed:
            raise SessionError(f"session {self.session_id!r} is closed")
        if not isinstance(type, str) or not type:
            raise SessionError("session event type must be a non-empty string")
        if not isinstance(data, dict):
            raise SessionError("session event data must be a dict")
        seq = self.last_seq + 1
        raw: dict[str, Any] = {"type": type, "seq": seq, "data": data}
        raw["time"] = envelope.pop("time", now_ms())
        if type in SURFACE_EVENTS and "surfaceOp" not in envelope:
            envelope["surfaceOp"] = "append"
        raw.update(envelope)
        normalized = normalize_event(raw, subject=f"append {type}")
        record = LogRecord.model_validate(normalized)
        nodes = self._surface_nodes()
        records_by_seq = {existing.seq: existing for existing in self.records}
        records_by_seq[record.seq] = record
        plan = plan_surface_transition(nodes, record, strict=not self.trimmed, records_by_seq=records_by_seq)
        writer.append_events(self.path, [writer.event_to_dict(record)], zstd=self.zstd)
        self.records.append(record)
        self._notify_append(record)
        kind, start, end = plan
        if kind == APPEND:
            nodes.append(record)
        elif kind == REPLACE:
            assert start is not None and end is not None
            nodes[start : end + 1] = [record]
        return seq

    def add_append_observer(self, observer: Callable[[Session, LogRecord], None]) -> Callable[[], None]:
        """Call observer(session, record) after each successful append.

        Observers are advisory: a raised exception is logged and swallowed so a
        broken projection (or UI listener) can never break durability. The
        returned disposer is idempotent.
        """
        self._append_observers.append(observer)

        def dispose() -> None:
            try:
                self._append_observers.remove(observer)
            except ValueError:
                pass

        return dispose

    def _notify_append(self, record: LogRecord) -> None:
        for observer in list(self._append_observers):
            try:
                observer(self, record)
            except Exception:
                logging.getLogger(__name__).warning(
                    "session append observer failed for %r", record.type, exc_info=True
                )

    def derive_messages(self, *, strict: bool | None = None) -> list[Message]:
        """Messages as the model should see them, after the canonical surface fold."""
        if strict is None:
            strict = not self.trimmed
        return _derive_messages(self.records, strict=strict)

    def usage(self) -> dict[str, int]:
        """Accumulated token usage across assistant messages."""
        totals = {"input": 0, "output": 0, "cached": 0}
        for record in self.records:
            if record.type != "assistant/message":
                continue
            usage = record.data.get("usage")
            if not isinstance(usage, Mapping):
                continue
            totals["input"] += _as_int(usage.get("inputTokens"))
            totals["output"] += _as_int(usage.get("outputTokens"))
            totals["cached"] += _as_int(usage.get("cacheReadTokens"))
        return totals

    def context_tokens(self) -> int:
        """Prompt tokens of the most recent model request (the live context size).

        Cumulative usage grows across every step of a turn, so it cannot answer
        "how full is the window". The latest assistant message records the input
        tokens of the request that produced it; that is the current context.
        """
        for record in reversed(self.records):
            if record.type != "assistant/message":
                continue
            usage = record.data.get("usage")
            if isinstance(usage, Mapping):
                return _as_int(usage.get("inputTokens"))
        return 0

    def turn_count(self) -> int:
        """Number of turns started in this session."""
        return sum(1 for record in self.records if record.type == "turn/start")

    def last_step(self) -> tuple[int, int]:
        """Most recent (turn, step) pair recorded by step boundaries."""
        for record in reversed(self.records):
            if record.type not in ("step/start", "step/end"):
                continue
            turn = record.data.get("turn")
            step = record.data.get("step")
            if isinstance(turn, int) and not isinstance(turn, bool) and isinstance(step, int):
                return turn, step
        return 0, 0

    def close(self) -> None:
        """Mark the session closed; every append is already durable and fsynced."""
        self._closed = True

    def _surface_nodes(self) -> list[LogRecord]:
        if self._surface is None:
            self._surface = fold_surface(self.records, strict=not self.trimmed)
        return self._surface

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Session(id={self.session_id!r}, records={len(self.records)}, path={str(self.path)!r})"


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
