"""Collapsible reasoning block: a six-line streaming tail, one line when done.

Reasoning is noisy and mostly transient, so during streaming the block only shows
the tail of the stream. When the turn ends it collapses to a single line such as
"· thinking (1.2k chars)"; clicking that line expands or collapses the full text.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.widgets import Static

from deeppyro.tui.format import compact_text

#: Number of trailing reasoning lines visible while streaming.
STREAMING_LINES = 6
#: Maximum characters per streaming line before truncating.
STREAMING_LINE_CHARS = 120
#: Expanded view limits; the full reasoning can be very long.
EXPANDED_LINES = 40
EXPANDED_CHARS = 4000


def format_char_count(count: int) -> str:
    """Render a character count compactly: 987, 1.2k, 12.3k."""
    value = int(count or 0)
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


class ThinkingBlock(Static):
    """Reasoning stream that ends as a clickable one-line summary."""

    def __init__(self) -> None:
        super().__init__("", classes="thinking-block")
        self._text = ""
        self._finished = False
        self._expanded = False
        self._mounted = False

    # -- state ----------------------------------------------------------
    @property
    def char_count(self) -> int:
        return len(self._text)

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def finished(self) -> bool:
        return self._finished

    def on_mount(self) -> None:
        self._mounted = True
        self._refresh()

    # -- streaming ------------------------------------------------------
    def append(self, chunk: str) -> None:
        """Append a reasoning chunk; safe before the widget is mounted."""
        if not chunk:
            return
        self._text += chunk
        if self._mounted:
            self._refresh()

    def finish(self) -> None:
        """Collapse the block into its summary line (idempotent)."""
        if self._finished:
            return
        self._finished = True
        self._expanded = False
        if self._mounted:
            self._refresh()

    # -- interaction ----------------------------------------------------
    def on_click(self, event: events.Click) -> None:
        if not self._finished or not self._text.strip():
            return
        self._expanded = not self._expanded
        self._refresh()
        event.stop()

    # -- rendering ------------------------------------------------------
    def _refresh(self) -> None:
        if not self._text.strip():
            self.display = False
            return
        self.display = True
        self.update(self._build_text())

    def _build_text(self) -> Text:
        text = Text()
        if not self._finished:
            tail = self._text.rstrip().splitlines()[-STREAMING_LINES:]
            for index, line in enumerate(tail):
                if index:
                    text.append("\n")
                text.append(_trim(line), style="dim italic")
            return text
        text.append(f"· thinking ({format_char_count(self.char_count)} chars)", style="dim italic")
        if not self._expanded:
            return text
        body = compact_text(self._text, max_lines=EXPANDED_LINES, max_chars=EXPANDED_CHARS)
        if body:
            text.append("\n", style="dim")
            text.append(body, style="dim italic")
        return text


def _trim(line: str, *, limit: int = STREAMING_LINE_CHARS) -> str:
    line = line.rstrip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


__all__ = ["ThinkingBlock", "format_char_count"]
