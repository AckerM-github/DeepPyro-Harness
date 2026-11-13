"""Modal question screen for the ask_user_question tool.

The screen shows one Question at a time. A single-select question renders radio
rows, a multi-select question renders checkbox rows. A custom answer can be
typed instead of, or in addition to, picking options. Confirming or skipping
returns an Answer; Escape and Cancel return None so the caller can stop the
whole request.

Results flow: QuestionScreen -> DeepPyroApp.ask_question -> the user_questions
service handler -> the ask_user_question tool. The screen never talks to the
service directly.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from deeppyro.user_questions.types import Answer, Question, QuestionOption

CUSTOM_INPUT_ID = "question-custom"
SKIP_BUTTON_ID = "question-skip"
CANCEL_BUTTON_ID = "question-cancel"
CUSTOM_PLACEHOLDER = "Type a custom answer..."
HINT_TEXT = "↑/↓ move  ·  Space toggle  ·  Enter confirm  ·  Tab custom  ·  Esc cancel"
MARKER_SINGLE_ON = "◉"
MARKER_SINGLE_OFF = "○"
MARKER_MULTI_ON = "☑"
MARKER_MULTI_OFF = "☐"


class QuestionOptionRow(Static):
    """One option row with a radio or checkbox marker and description."""

    def __init__(self, owner: QuestionScreen, option: QuestionOption, index: int) -> None:
        super().__init__("", classes="q-option")
        self._owner = owner
        self._option = option
        self._index = index

    @property
    def index(self) -> int:
        """Position of this option in the question."""
        return self._index

    @property
    def plain_text(self) -> str:
        """The rendered row as plain text (marker + label + description)."""
        return self._content().plain

    def refresh_content(self) -> None:
        """Re-render the marker, label and description for the current state."""
        self.update(self._content())

    def _content(self) -> Text:
        selected = self._owner.is_selected(self._index)
        highlighted = self._owner.is_highlighted(self._index)
        if self._owner.multi_select:
            marker = MARKER_MULTI_ON if selected else MARKER_MULTI_OFF
        else:
            marker = MARKER_SINGLE_ON if highlighted else MARKER_SINGLE_OFF
        text = Text()
        style = "bold bright_cyan" if highlighted else ("bold" if selected else "")
        text.append(marker + " ", style="bold bright_cyan" if (selected or highlighted) else "dim")
        text.append(self._option.label, style=style)
        description = (self._option.description or "").strip()
        if description:
            text.append("\n  ")
            text.append(description, style="dim")
        self.set_class(highlighted, "q-highlighted")
        self.set_class(selected, "q-selected")
        return text


class QuestionScreen(ModalScreen[Answer | None]):
    """Ask one Question; dismiss with an Answer, or None to cancel the request.

    Parameters
    ----------
    question:
        The question to show.
    index, total:
        Optional position used by the title when several questions are asked
        one after another.
    """

    #: Disable autofocus: keys go to the screen until Tab reaches the custom input.
    AUTO_FOCUS: ClassVar[str | None] = ""
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("space", "toggle", "Toggle", show=False),
        Binding("enter", "accept", "Confirm", show=False),
    ]

    DEFAULT_CSS = """
    QuestionScreen {
        align: center middle;
        background: $background 70%;
    }
    #question-dialog {
        width: 82;
        height: auto;
        max-height: 30;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #question-title {
        color: $accent;
        text-style: bold;
    }
    #question-body {
        color: $text;
        margin: 1 0 0 0;
    }
    #question-options {
        height: auto;
        max-height: 12;
        margin: 1 0 0 0;
    }
    #question-options .q-option {
        width: 1fr;
        height: auto;
        padding: 0 1;
        color: $text;
    }
    #question-options .q-option.q-highlighted {
        background: $accent 20%;
    }
    #question-options .q-option.q-selected {
        color: $accent;
    }
    #question-empty {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    #question-custom-hint {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    #question-custom {
        margin: 0;
    }
    #question-buttons {
        height: auto;
        margin: 1 0 0 0;
        align-horizontal: right;
    }
    #question-buttons Button {
        margin-left: 2;
    }
    #question-hint {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, question: Question, *, index: int = 0, total: int = 1) -> None:
        super().__init__()
        self.question = question
        self._index = max(0, int(index))
        self._total = max(1, int(total))
        self._highlight = 0
        self._touched = False
        self._selected: set[int] = set()
        self._rows: list[QuestionOptionRow] = [
            QuestionOptionRow(self, option, position)
            for position, option in enumerate(tuple(question.options))
        ]
        self._custom: Input | None = None

    # -- state -----------------------------------------------------------
    @property
    def index(self) -> int:
        """Zero-based position of this question in the request."""
        return self._index

    @property
    def total(self) -> int:
        """Number of questions in the request."""
        return self._total

    @property
    def multi_select(self) -> bool:
        return bool(self.question.multi_select)

    @property
    def rows(self) -> list[QuestionOptionRow]:
        return list(self._rows)

    def is_highlighted(self, index: int) -> bool:
        return index == self._highlight

    def is_selected(self, index: int) -> bool:
        if self.multi_select:
            return index in self._selected
        return index == self._highlight

    # -- composition -----------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="question-dialog"):
            yield Static(self._title_text(), id="question-title")
            yield Static(self.question.question, id="question-body")
            with Vertical(id="question-options"):
                yield from self._rows
            if not self._rows:
                yield Static("Type your answer below, or skip the question.", id="question-empty")
            yield Static("Custom answer:", id="question-custom-hint")
            yield Input(placeholder=CUSTOM_PLACEHOLDER, id=CUSTOM_INPUT_ID)
            with Horizontal(id="question-buttons"):
                yield Button("Skip", id=SKIP_BUTTON_ID)
                yield Button("Cancel", id=CANCEL_BUTTON_ID)
            yield Static(HINT_TEXT, id="question-hint")

    def _title_text(self) -> str:
        header = (self.question.header or "").strip()
        count = f"Question {self._index + 1} of {self._total}"
        if header and self._total > 1:
            return f"{header}  ({self._index + 1}/{self._total})"
        if header:
            return header
        if self._total > 1:
            return count
        return "Question"

    def on_mount(self) -> None:
        self._custom = self.query_one("#" + CUSTOM_INPUT_ID, Input)
        self._sync_rows()
        self.app.set_focus(None)

    # -- keys ------------------------------------------------------------
    def action_cursor_up(self) -> None:
        self._move(-1)

    def action_cursor_down(self) -> None:
        self._move(1)

    def action_toggle(self) -> None:
        if not self._rows:
            return
        self._touched = True
        if self.multi_select:
            if self._highlight in self._selected:
                self._selected.discard(self._highlight)
            else:
                self._selected.add(self._highlight)
        self._sync_rows()

    def action_accept(self) -> None:
        self.dismiss(self.build_answer())

    def action_skip(self) -> None:
        self.dismiss(Answer(id=self.question.id, selected=(), custom=None))

    def action_cancel(self) -> None:
        self.dismiss(None)

    # -- input -----------------------------------------------------------
    @on(Input.Submitted)
    def _on_custom_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != CUSTOM_INPUT_ID:
            return
        event.stop()
        self.dismiss(self.build_answer())

    @on(Button.Pressed)
    def _on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == SKIP_BUTTON_ID:
            self.action_skip()
        elif event.button.id == CANCEL_BUTTON_ID:
            self.action_cancel()

    def on_click(self, event: events.Click) -> None:
        widget = event.widget
        for position, row in enumerate(self._rows):
            if widget is row:
                self._touched = True
                self._highlight = position
                if self.multi_select:
                    if position in self._selected:
                        self._selected.discard(position)
                    else:
                        self._selected.add(position)
                self._sync_rows()
                event.stop()
                return

    # -- answers ---------------------------------------------------------
    def build_answer(self) -> Answer:
        """Build the Answer for the current state without dismissing."""
        options = tuple(self.question.options)
        custom = self.custom_value()
        if self.multi_select:
            selected = tuple(
                option.label for position, option in enumerate(options) if position in self._selected
            )
        elif options and not (custom and not self._touched):
            selected = (options[self._highlight].label,)
        else:
            selected = ()
        return Answer(id=self.question.id, selected=selected, custom=custom)

    def custom_value(self) -> str | None:
        if self._custom is None:
            return None
        value = str(self._custom.value or "").strip()
        return value or None

    # -- helpers ---------------------------------------------------------
    def _move(self, delta: int) -> None:
        if not self._rows:
            return
        self._touched = True
        self._highlight = (self._highlight + delta) % len(self._rows)
        self._sync_rows()

    def _sync_rows(self) -> None:
        for row in self._rows:
            row.refresh_content()


__all__ = [
    "CANCEL_BUTTON_ID",
    "CUSTOM_INPUT_ID",
    "CUSTOM_PLACEHOLDER",
    "HINT_TEXT",
    "SKIP_BUTTON_ID",
    "QuestionOptionRow",
    "QuestionScreen",
]
