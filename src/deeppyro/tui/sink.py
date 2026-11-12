"""Event sink that forwards agent events into the Textual app.

AgentSession emits plain dictionaries through a synchronous emit method.
The TUI runs on the same asyncio loop as the agent worker, so the sink can
hand each event straight to the app; the app owns all widget mutation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class TuiSink:
    """EventSink implementation bound to one DeepPyroApp."""

    def __init__(self, on_event: Callable[[dict[str, Any]], None]) -> None:
        self._on_event = on_event
        self.event_count = 0
        self.saw_error = False
        self.final_text = ""
        self.last_usage: dict[str, int] | None = None

    def begin_turn(self) -> None:
        self.saw_error = False
        self.final_text = ""
        self.last_usage = None

    def end_turn(self) -> None:
        return None

    def emit(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        kind = str(payload.get("type", ""))
        self.event_count += 1
        if kind == "error":
            self.saw_error = True
            self.final_text = ""
        elif kind == "final":
            self.final_text = str(payload.get("text") or "")
        elif kind == "usage":
            self.last_usage = {key: int(payload.get(key) or 0) for key in ("input", "output", "cached")}
        self._on_event(payload)


__all__ = ["TuiSink"]
