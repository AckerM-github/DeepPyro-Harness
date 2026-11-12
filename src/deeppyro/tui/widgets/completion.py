"""Slash-command completion popup.

The popup is an OptionList that is mounted on the screen (not inside the
prompt) and absolutely positioned just above the input box. Focus stays in the
prompt, so all navigation keys are handled by PromptArea.
"""

from __future__ import annotations

from textual.widgets import OptionList
from textual.widgets.option_list import Option

MAX_VISIBLE_OPTIONS = 8


def completion_prefix(text: str) -> str | None:
    """Return the slash-command token being typed, or None when idle.

    Completion is active only for a single line that starts with a slash and
    whose first token has no whitespace. An empty string means the user typed
    just a slash.
    """
    if "\n" in text:
        return None
    if not text.startswith("/"):
        return None
    token = text[1:]
    if " " in token or "\t" in token or "/" in token:
        return None
    return token


class CompletionPopup(OptionList, can_focus=False):
    """Non-focusable list of matching slash commands."""

    DEFAULT_CSS = """
    CompletionPopup {
        position: absolute;
        width: 46;
        height: auto;
        max-height: 10;
        display: none;
        border: tall $accent;
        background: $panel;
        padding: 0 1;
    }
    """

    def show_items(self, items: list[tuple[str, str]]) -> None:
        """Show one option per (value, label) pair and highlight the first."""
        self.set_options([Option(label, id=value) for value, label in items])
        if self.option_count:
            self.highlighted = 0
        self.display = True

    def hide_popup(self) -> None:
        """Hide the popup without changing its options."""
        self.display = False

    @property
    def is_open(self) -> bool:
        return self.display

    @property
    def selected_value(self) -> str | None:
        """The id of the highlighted option, if any."""
        option = self.highlighted_option
        if option is None or option.id is None:
            return None
        return option.id

    def move_highlight(self, delta: int) -> None:
        """Move the highlight by delta, wrapping around the option list."""
        count = self.option_count
        if count == 0:
            return
        current = self.highlighted if self.highlighted is not None else 0
        self.highlighted = (current + delta) % count


__all__ = ["MAX_VISIBLE_OPTIONS", "CompletionPopup", "completion_prefix"]
