"""One-line turn progress for a streamed reasoning trace.

A user turn owns exactly one TurnProgress line. While the model is thinking the
line shows the newest complete sentence of the current assistant step, trimmed
to a single terminal line. When the turn ends it collapses to a one-line summary
such as "· thinking · 3 steps · 12 sentences  (click to view)". Clicking the
line posts TurnProgress.ThinkingRequested carrying the complete reasoning, which
the application uses to open the full-screen thinking viewer.

The widget is pure presentation: it never talks to the agent loop or the session
log. ConversationView owns the streaming lifecycle (begin_step, append, finish)
and this module only renders it. Styling lives in DEFAULT_CSS, so no theme edit
is required.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import Static

from deeppyro.tui.history import ReasoningStep, TurnReasoning
from deeppyro.tui.widgets.thinking import STREAMING_LINE_CHARS, latest_sentence

_STREAM_MARK = "✻ "
_SUMMARY_MARK = "· thinking · "
_SUMMARY_HINT = "  (click to view)"


def _one_line(text: str, *, limit: int = STREAMING_LINE_CHARS) -> str:
    """Collapse whitespace and keep the newest characters on a single line."""
    line = " ".join(str(text or "").split())
    if len(line) <= limit:
        return line
    if limit <= 1:
        return line[:limit]
    return "…" + line[-(limit - 1) :]


def _plural(count: int, unit: str) -> str:
    """Render a count with a naive English plural: 1 step, 2 steps."""
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


class TurnProgress(Static):
    """Single progress line for one user turn of streamed reasoning."""

    class ThinkingRequested(Message):
        """Posted when the progress line is clicked."""

        def __init__(self, reasoning: TurnReasoning) -> None:
            super().__init__()
            self.reasoning = reasoning

    DEFAULT_CSS = """
    TurnProgress {
        height: auto;
        color: $text-muted;
        padding: 0 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    TurnProgress:hover {
        color: $text;
    }
    """

    def __init__(self, turn: int = 0, reasoning: TurnReasoning | None = None) -> None:
        super().__init__("", classes="turn-progress")
        self._reasoning = reasoning if reasoning is not None else TurnReasoning(turn=turn)
        if not self._reasoning.turn and turn:
            self._reasoning.turn = turn
        self._current: ReasoningStep | None = None
        self._mounted = False

    # -- state ----------------------------------------------------------
    @property
    def turn(self) -> int:
        """The user turn number this line reports on."""
        return self._reasoning.turn

    @property
    def reasoning(self) -> TurnReasoning:
        """The reasoning accumulated for this turn."""
        return self._reasoning

    @property
    def finished(self) -> bool:
        """Whether the turn has ended and the line shows its summary."""
        return bool(self._reasoning.finished)

    @property
    def step_count(self) -> int:
        """Number of assistant steps recorded for this turn."""
        return len(self._reasoning.steps)

    @property
    def sentence_count(self) -> int:
        """Number of complete reasoning sentences recorded for this turn."""
        return self._reasoning.sentence_count

    @property
    def has_text(self) -> bool:
        """Whether any reasoning text has been recorded yet."""
        return any(step.text.strip() for step in self._reasoning.steps)

    @property
    def line_text(self) -> str:
        """Plain text of the current progress line (convenience for tests)."""
        content = self.content
        plain = getattr(content, "plain", None)
        if plain is not None:
            return str(plain)
        return "" if content is None else str(content)

    # -- lifecycle ------------------------------------------------------
    def begin_step(self, step: int) -> None:
        """Start a new assistant step inside this turn."""
        item = ReasoningStep(step=step)
        self._reasoning.steps.append(item)
        self._current = item
        self._refresh_line()

    def append(self, chunk: str) -> None:
        """Append reasoning text to the current step (starts step 1 if needed)."""
        if not chunk:
            return
        if self._current is None:
            self.begin_step(1)
        current = self._current
        if current is None:  # pragma: no cover - begin_step always sets it
            return
        current.text += str(chunk)
        self._refresh_line()

    def finish(self) -> None:
        """Collapse the line into its one-line summary (idempotent)."""
        if self._reasoning.finished:
            return
        self._reasoning.finished = True
        self._refresh_line()

    # -- rendering ------------------------------------------------------
    def on_mount(self) -> None:
        self._mounted = True
        self._refresh_line()

    def on_click(self, event: events.Click) -> None:
        if not self.has_text:
            return
        self.post_message(self.ThinkingRequested(self._reasoning))
        event.stop()

    def _refresh_line(self) -> None:
        if not self._mounted:
            return
        if not self.has_text:
            self.display = False
            return
        self.display = True
        self.update(self._build_text())

    def _build_text(self) -> Text:
        text = Text()
        if not self._reasoning.finished:
            body = _one_line(self._streaming_sentence(), limit=STREAMING_LINE_CHARS - len(_STREAM_MARK))
            text.append(_STREAM_MARK, style="dim")
            text.append(body, style="dim italic")
            return text
        summary = (
            f"{_SUMMARY_MARK}{_plural(self.step_count, 'step')} · {_plural(self.sentence_count, 'sentence')}"
        )
        text.append(summary, style="dim italic")
        text.append(_SUMMARY_HINT, style="dim")
        return text

    def _streaming_sentence(self) -> str:
        raw = ""
        if self._current is not None and self._current.text.strip():
            raw = self._current.text
        if not raw:
            for step in reversed(self._reasoning.steps):
                if step.text.strip():
                    raw = step.text
                    break
        sentence = latest_sentence(raw)
        return sentence or _one_line(raw)


ThinkingRequested = TurnProgress.ThinkingRequested

__all__ = ["ThinkingRequested", "TurnProgress"]
