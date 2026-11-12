"""Incremental server-sent-events decoding for OpenAI-compatible streams.

OpenAI chat completions (and llama.cpp / vLLM / DeepSeek compatible servers) answer a
streaming request with text/event-stream frames of the shape::

    data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"hi"}}]}

    data: [DONE]

Only the data field carries payload; event / id / retry are decoded and surfaced on
SseEvent but never required.  Comment lines (": ping") and the blank line that terminates
every event are ignored.  Decoding is incremental: a multi-byte UTF-8 character split across
two TCP reads is buffered with a stateful decoder so deltas are never corrupted.
"""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass


@dataclass(slots=True)
class SseEvent:
    """One dispatched SSE event."""

    data: str
    event: str | None = None
    id: str | None = None


class SseDecoder:
    """Stateful text -> SseEvent decoder.

    Feed chunks with feed(); each call returns the events completed by that chunk.  Call
    flush() once at end of stream to dispatch a trailing event that was not followed by a
    blank line (some servers omit the final separator).
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._data: list[str] = []
        self._event: str | None = None
        self._last_id: str | None = None

    # -- incremental feeding ------------------------------------------------
    def feed(self, chunk: bytes | bytearray | str) -> list[SseEvent]:
        """Decode chunk and return every event it completed."""
        if isinstance(chunk, (bytes, bytearray)):
            text = self._decoder.decode(bytes(chunk))
        else:
            text = chunk
        self._buffer += text
        events: list[SseEvent] = []
        while True:
            line = self._pop_line()
            if line is None:
                break
            dispatched = self._handle_line(line)
            if dispatched is not None:
                events.append(dispatched)
        return events

    def flush(self) -> list[SseEvent]:
        """Dispatch a pending event at end of stream (no trailing blank line)."""
        events: list[SseEvent] = []
        if self._buffer:
            # A final line without a newline is common when the stream just stops.
            line, self._buffer = self._buffer, ""
            dispatched = self._handle_line(line)
            if dispatched is not None:
                events.append(dispatched)
        dispatched = self._dispatch()
        if dispatched is not None:
            events.append(dispatched)
        return events

    # -- internals ----------------------------------------------------------
    def _pop_line(self) -> str | None:
        buffer = self._buffer
        for index, char in enumerate(buffer):
            if char == "\n":
                self._buffer = buffer[index + 1 :]
                line = buffer[:index]
                return line.removesuffix("\r")
            if char == "\r":
                if index == len(buffer) - 1:
                    # Might be the first half of a CRLF pair that is still in flight.
                    return None
                if buffer[index + 1] == "\n":
                    self._buffer = buffer[index + 2 :]
                else:
                    self._buffer = buffer[index + 1 :]
                return buffer[:index]
        return None

    def _handle_line(self, line: str) -> SseEvent | None:
        if not line:
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value or None
        elif field == "id":
            self._last_id = value or None
        # "retry" and unknown fields are ignored on purpose.
        return None

    def _dispatch(self) -> SseEvent | None:
        if not self._data:
            return None
        data = "\n".join(self._data)
        self._data = []
        event, self._event = self._event, None
        return SseEvent(data=data, event=event, id=self._last_id)


async def iter_sse(
    chunks: AsyncIterator[bytes],
    *,
    decoder: SseDecoder | None = None,
) -> AsyncIterator[SseEvent]:
    """Adapt an async byte stream to SseEvents."""
    sse = decoder if decoder is not None else SseDecoder()
    async for chunk in chunks:
        for event in sse.feed(chunk):
            yield event
    for event in sse.flush():
        yield event


def decode_sse_text(text: str) -> list[SseEvent]:
    """Decode an in-memory SSE document (test / non-streaming helper)."""
    decoder = SseDecoder()
    events = decoder.feed(text)
    events.extend(decoder.flush())
    return events


def format_sse(events: Iterable[str]) -> str:
    """Encode data payload strings as an SSE document (used by tests)."""
    return "".join(f"data: {payload}\n\n" for payload in events)
