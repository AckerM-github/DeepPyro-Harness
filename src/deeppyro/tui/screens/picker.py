"""Generic filterable list modal used by /resume and /model.

PickerScreen is a ModalScreen that returns the selected value (the first item
of the chosen tuple) or None when the user cancels with Escape. Items are
(value, label, note) tuples; typing in the filter box matches all three fields
case-insensitively.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

PickerItem = tuple[str, str, str]


class PickerScreen(ModalScreen[str | None]):
    """Filterable list: select with Enter, cancel with Escape."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "accept", "Select", show=False),
        Binding("up", "cursor_up", "Previous", show=False),
        Binding("down", "cursor_down", "Next", show=False),
    ]

    DEFAULT_CSS = """
    PickerScreen {
        align: center middle;
        background: $background 70%;
    }
    #picker-dialog {
        width: 82;
        height: auto;
        max-height: 26;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #picker-title {
        color: $accent;
        text-style: bold;
    }
    #picker-filter {
        margin: 1 0 0 0;
    }
    #picker-list {
        height: auto;
        max-height: 18;
        margin: 1 0 0 0;
    }
    #picker-empty {
        color: $text-muted;
        margin: 1 0 0 0;
        display: none;
    }
    """

    def __init__(self, title: str, items: Sequence[PickerItem]) -> None:
        super().__init__()
        self._title = title
        self._items = list(items)
        self._values: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog"):
            yield Static(self._title, id="picker-title")
            yield Input(placeholder="Filter...", id="picker-filter")
            yield OptionList(id="picker-list")
            yield Static("No matches", id="picker-empty")

    def on_mount(self) -> None:
        self.query_one("#picker-filter", Input).focus()
        self._apply_filter("")

    def _apply_filter(self, query: str) -> None:
        needle = query.strip().lower()
        matches = [
            item for item in self._items if not needle or any(needle in field.lower() for field in item)
        ]
        options: list[Option] = []
        values: dict[str, str] = {}
        for index, (value, label, note) in enumerate(matches):
            option_id = "pick-" + str(index)
            values[option_id] = value
            text = label if not note else label + "  " + note
            options.append(Option(text, id=option_id))
        self._values = values
        option_list = self.query_one("#picker-list", OptionList)
        option_list.set_options(options)
        if options:
            option_list.highlighted = 0
        self.query_one("#picker-empty", Static).display = not options

    def on_input_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_accept()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id is not None:
            self.dismiss(self._values.get(event.option.id))
        else:
            self.dismiss(None)

    def action_accept(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        option = option_list.highlighted_option
        if option is None or option.id is None:
            self.dismiss(None)
            return
        self.dismiss(self._values.get(option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_cursor_up(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        if option_list.option_count:
            option_list.action_cursor_up()

    def action_cursor_down(self) -> None:
        option_list = self.query_one("#picker-list", OptionList)
        if option_list.option_count:
            option_list.action_cursor_down()


__all__ = ["PickerItem", "PickerScreen"]
