"""Free-text workspace path modal opened from the cwd dropdown."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static


class PathPromptScreen(ModalScreen[str | None]):
    """Ask for one directory path; returns the trimmed path or None on cancel."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    PathPromptScreen {
        align: center middle;
        background: $background 70%;
    }
    #path-dialog {
        width: 78;
        height: auto;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #path-title {
        color: $accent;
        text-style: bold;
    }
    #path-input {
        margin: 1 0 0 0;
    }
    #path-hint {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, current: str = "", title: str = "Switch workspace") -> None:
        super().__init__()
        self._current = str(current)
        self._title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="path-dialog"):
            yield Static(self._title, id="path-title")
            yield Input(value=self._current, placeholder="D:/code/project", id="path-input")
            yield Static("Enter confirm · Esc cancel", id="path-hint")

    def on_mount(self) -> None:
        path_input = self.query_one("#path-input", Input)
        path_input.focus()
        path_input.cursor_position = len(path_input.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["PathPromptScreen"]
