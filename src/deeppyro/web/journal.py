"""In-memory replay journal feeding the browser EventSource.

Turns stream from the native agent host through this journal. The journal is
an output tap, not conversation state: sessions remain append-only native facts
and the UI replays transcripts from those facts. Keeping the last window of
events here lets a reconnecting EventSource resume with Last-Event-ID, so the
web process stays stateless across reconnects.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

from deeppyro.web.events import WebEvent, to_wire
from deeppyro.web.wire import JsonValue

#: How many events a reconnecting client can replay.
DEFAULT_CAPACITY = 4096

#: Seconds between SSE keep-alive comments.
DEFAULT_HEARTBEAT = 15.0


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One sequenced event in the stream."""

    seq: int
    event: WebEvent

    def to_wire(self) -> dict[str, JsonValue]:
        """The SSE data object, including its replay id."""
        payload = to_wire(self.event)
        payload["seq"] = self.seq
        return payload


class EventJournal:
    """A monotonic event log with async subscribers and replay by sequence."""

    def __init__(self, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self._entries: deque[JournalEntry] = deque(maxlen=max(int(capacity), 1))
        self._seq = 0
        self._closed = False
        self._changed = asyncio.Event()

    @property
    def last_seq(self) -> int:
        """Sequence number of the newest published event (0 when empty)."""
        return self._seq

    @property
    def closed(self) -> bool:
        """Whether the journal stopped publishing and streams should end."""
        return self._closed

    def publish(self, event: WebEvent) -> JournalEntry:
        """Append one event and wake every subscriber (synchronous, loop-safe)."""
        self._seq += 1
        entry = JournalEntry(seq=self._seq, event=event)
        self._entries.append(entry)
        self._wake()
        return entry

    def entries_after(self, seq: int) -> list[JournalEntry]:
        """Events newer than seq, oldest first (bounded by capacity)."""
        floor = max(int(seq), 0)
        return [entry for entry in self._entries if entry.seq > floor]

    async def stream(
        self,
        *,
        after: int = 0,
        heartbeat: float = DEFAULT_HEARTBEAT,
    ) -> AsyncIterator[JournalEntry | None]:
        """Yield entries after seq, then live entries; None means heartbeat.

        The generator ends when the journal closes; a cancelled consumer sees
        CancelledError and the server closes the connection.
        """
        seq = max(int(after), 0)
        while True:
            for entry in self.entries_after(seq):
                seq = entry.seq
                yield entry
            if self._closed:
                return
            waiter = self._changed
            if any(entry.seq > seq for entry in self._entries):
                continue
            try:
                await asyncio.wait_for(waiter.wait(), timeout=heartbeat)
            except TimeoutError:
                yield None

    def close(self) -> None:
        """End every live stream (server shutdown)."""
        self._closed = True
        self._wake()

    def _wake(self) -> None:
        waiter = self._changed
        self._changed = asyncio.Event()
        waiter.set()


__all__ = ["DEFAULT_CAPACITY", "DEFAULT_HEARTBEAT", "EventJournal", "JournalEntry"]
