"""Rolling one-line reasoning ticker that collapses when the answer starts.

Reasoning is noisy: showing paragraphs of it pushes the actual answer off screen.
The block therefore shows exactly one line at a time - the newest sentence of the
stream - so a turn reads like a live progress line rather than a wall of text.
When the answer starts (or the turn ends) the ticker collapses to a single
summary line; clicking it expands the accumulated reasoning for inspection.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.widgets import Static

from deeppyro.tui.format import compact_text

#: Maximum characters of the live sentence visible while streaming.
STREAMING_LINE_CHARS = 120
#: Expanded view limits; the full reasoning can be very long.
EXPANDED_LINES = 40
EXPANDED_CHARS = 4000

_STREAM_MARK = "✻ "
_SUMMARY_MARK = "· thinking · "
_ENDINGS = "。！？!?；;"


def format_char_count(count: int) -> str:
    """Render a character count compactly: 987, 1.2k, 12.3k."""
    value = int(count or 0)
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def split_sentences(text: str) -> list[str]:
    """Split reasoning into sentences (CJK/ASCII terminators and newlines)."""
    sentences: list[str] = []
    buffer = ""
    for char in text:
        if char == "\n":
            piece = buffer.strip()
            if piece:
                sentences.append(piece)
            buffer = ""
            continue
        buffer += char
        if char in _ENDINGS:
            piece = buffer.strip()
            if piece:
                sentences.append(piece)
            buffer = ""
    tail = buffer.strip()
    if tail:
        sentences.append(tail)
    return sentences


def latest_sentence(text: str) -> str:
    """Return the newest sentence, or an empty string for empty reasoning."""
    sentences = split_sentences(text)
    if not sentences:
        return ""
    return " ".join(sentences[-1].split())


def _trim_tail(line: str, *, limit: int = STREAMING_LINE_CHARS) -> str:
    """Keep the newest characters of a long live sentence."""
    line = " ".join(line.split())
    if len(line) <= limit:
        return line
    return "…" + line[-(limit - 1) :]


class ThinkingBlock(Static):
    """Single-line reasoning ticker that ends as a clickable summary."""

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

    @property
    def sentence_count(self) -> int:
        return len(split_sentences(self._text))

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
        """Collapse the ticker into its summary line (idempotent)."""
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
            sentence = latest_sentence(self._text)
            if not sentence:
                sentence = _trim_tail(self._text)
            text.append(_STREAM_MARK, style="dim")
            text.append(_trim_tail(sentence), style="dim italic")
            return text
        count = max(self.sentence_count, 1)
        unit = "sentence" if count == 1 else "sentences"
        text.append(f"{_SUMMARY_MARK}{count} {unit}", style="dim italic")
        text.append("  (click to collapse)" if self._expanded else "  (click to expand)", style="dim")
        if not self._expanded:
            return text
        body = compact_text(self._text, max_lines=EXPANDED_LINES, max_chars=EXPANDED_CHARS)
        if body:
            text.append("\n", style="dim")
            text.append(body, style="dim italic")
        return text


__all__ = [
    "EXPANDED_CHARS",
    "EXPANDED_LINES",
    "STREAMING_LINE_CHARS",
    "ThinkingBlock",
    "format_char_count",
    "latest_sentence",
    "split_sentences",
]
