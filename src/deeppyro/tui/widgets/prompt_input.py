"""The message prompt.

Textual's Input submits on Enter, which matches the primary interaction. A
multiline composer (Shift+Enter newline, history, slash completion) replaces
this widget in the UI workstream; the app only depends on the public Input API.
"""

from __future__ import annotations

from textual.widgets import Input

PLACEHOLDER = "Message DeepPyro…  (/help for commands, Ctrl+D to quit)"


class PromptInput(Input):
    """Single-line prompt with a stable id and placeholder."""

    def __init__(self) -> None:
        super().__init__(placeholder=PLACEHOLDER, id="prompt")


__all__ = ["PLACEHOLDER", "PromptInput"]
