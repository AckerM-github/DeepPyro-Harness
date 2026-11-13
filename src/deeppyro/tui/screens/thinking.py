"""Full-screen viewer for the reasoning recorded across user turns.

The conversation itself only shows a one-line TurnProgress per turn. This modal
renders the complete reasoning for one or more turns: a turn heading, one heading
per assistant step and the full step text inside a scrollable area. Escape closes
the screen and returns None, matching the other read-only modals.

The screen is pure presentation: callers pass the TurnReasoning objects rebuilt
by deeppyro.tui.history (or handed over by TurnProgress.ThinkingRequested). It
never talks to the agent loop and it does not read the session log itself.
Styling lives in DEFAULT_CSS so no theme edit is required.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from deeppyro.tui.history import TurnReasoning

DEFAULT_TITLE = "Reasoning"
EMPTY_TEXT = "No reasoning recorded."
STEP_EMPTY_TEXT = "(no reasoning recorded)"


class ThinkingScreen(ModalScreen[None]):
    """Scrollable reasoning viewer for one or more user turns."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    ThinkingScreen {
        align: center middle;
        background: $background 70%;
    }
    #thinking-dialog {
        width: 90%;
        height: 80%;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #thinking-title {
        color: $accent;
        text-style: bold;
    }
    #thinking-body {
        height: 1fr;
        margin: 1 0 0 0;
    }
    .thinking-turn {
        color: $accent;
        text-style: bold;
        margin: 1 0 0 0;
    }
    .thinking-step {
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 0;
    }
    .thinking-step-text {
        color: $text;
        margin: 0 0 0 2;
    }
    #thinking-empty {
        color: $text-muted;
    }
    #thinking-hint {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, turns: TurnReasoning | Sequence[TurnReasoning] = (), title: str | None = None) -> None:
        super().__init__()
        if isinstance(turns, TurnReasoning):
            self._turns = [turns]
        else:
            self._turns = list(turns)
        self._title = title or DEFAULT_TITLE

    @property
    def turns(self) -> list[TurnReasoning]:
        """The turns displayed by this screen, in chronological order."""
        return list(self._turns)

    @property
    def heading(self) -> str:
        """The heading shown at the top of the dialog."""
        return self._title

    def compose(self) -> ComposeResult:
        with Vertical(id="thinking-dialog"):
            yield Static(self._title, id="thinking-title", markup=False)
            with VerticalScroll(id="thinking-body"):
                if not self._turns:
                    yield Static(EMPTY_TEXT, id="thinking-empty")
                for turn in self._turns:
                    yield Static(f"Turn {turn.turn}", classes="thinking-turn")
                    if not turn.steps:
                        yield Static(STEP_EMPTY_TEXT, classes="thinking-step-text", markup=False)
                        continue
                    for step in turn.steps:
                        yield Static(f"Step {step.step}", classes="thinking-step")
                        text = step.text or STEP_EMPTY_TEXT
                        yield Static(text, classes="thinking-step-text", markup=False)
            yield Static("Esc to close", id="thinking-hint")

    def on_mount(self) -> None:
        self.query_one("#thinking-body", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = ["DEFAULT_TITLE", "EMPTY_TEXT", "STEP_EMPTY_TEXT", "ThinkingScreen"]
