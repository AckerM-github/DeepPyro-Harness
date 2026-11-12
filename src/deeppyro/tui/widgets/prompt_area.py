"""Multiline prompt composer with history and slash-command completion.

PromptArea posts PromptArea.Submitted(text) when the user presses Enter on a
non-empty prompt. Shift+Enter inserts a newline (the box grows up to eight
lines); Up and Down replay persisted history; a leading slash opens the
command completion popup.

Integration contract for the app switch
---------------------------------------
The app owns app.py and still mounts PromptInput for now. To switch over, make
these changes in src/deeppyro/tui/app.py:

1. Imports around line 29: replace the prompt_input import with
       from deeppyro.tui.widgets.prompt_area import PromptArea
   The "Input" name imported on line 22 is no longer needed for the prompt;
   drop it if nothing else in app.py uses it.
2. Annotation around line 63: self._prompt: PromptArea | None = None
3. Compose around line 74: yield PromptArea()
4. on_mount around line 78: self._prompt = self.query_one(PromptArea)
5. Submit handler around lines 157-173: decorate with @on(PromptArea.Submitted),
   type the parameter as PromptArea.Submitted and read event.text. The widget
   already clears itself before posting, so remove the event.input.value line.
6. The Ctrl+C clear-input branch around line 211: test prompt.text and clear
   with prompt.text = "" instead of prompt.value.

PromptArea keeps id "prompt", the same placeholder and a three-line default
height, so theme.tcss styling keeps working.
"""

from __future__ import annotations

from pathlib import Path

from textual import events
from textual.message import Message
from textual.widgets import TextArea

from deeppyro.tui.commands import candidates
from deeppyro.tui.widgets.completion import MAX_VISIBLE_OPTIONS, CompletionPopup, completion_prefix
from deeppyro.tui.widgets.history import History
from deeppyro.tui.widgets.prompt_input import PLACEHOLDER

MAX_VISIBLE_LINES = 8
BORDER_HEIGHT = 2


class PromptArea(TextArea):
    """Multiline composer: Enter submit, Shift+Enter newline, history, slash completion.

    Message handling (the app listens to Submitted):

    * Enter without an open completion popup submits a non-empty prompt.
    * Enter or Tab with the popup open accepts the highlighted command.
    * Shift+Enter and Ctrl+J insert a newline.
    * Up and Down navigate the completion popup when it is open, otherwise
      recall history when the cursor sits on the first or last line.
    * Escape closes the completion popup.
    """

    class Submitted(Message):
        """Posted when the user submits the prompt with Enter."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, *, history_path: Path | None = None) -> None:
        super().__init__(
            placeholder=PLACEHOLDER,
            soft_wrap=True,
            tab_behavior="focus",
            max_checkpoints=50,
            id="prompt",
        )
        self._history = History(path=history_path)
        self._history.load()
        self._history_index: int | None = None
        self._draft = ""
        self._completion = CompletionPopup()
        self._popup_ready = False
        self._suppress_next_completion = False
        self._applied_height = 0

    async def on_mount(self) -> None:
        await self.screen.mount(self._completion)
        self._popup_ready = True
        self._sync_height()

    def _on_resize(self) -> None:
        super()._on_resize()
        self._sync_height()

    def _sync_height(self) -> None:
        if not self.is_mounted:
            return
        visible = min(MAX_VISIBLE_LINES, max(1, self.wrapped_document.height))
        total = visible + BORDER_HEIGHT
        if total != self._applied_height:
            self._applied_height = total
            self.styles.height = total
        if self._completion.is_open:
            self._place_completion(self._completion.option_count)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_height()
        if not self._popup_ready:
            return
        if self._suppress_next_completion:
            self._suppress_next_completion = False
            return
        self._update_completion()

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        popup_open = self._completion.is_open

        if popup_open and key in {"enter", "ctrl+m"}:
            event.stop()
            event.prevent_default()
            self._accept_completion()
            return
        if popup_open and key == "tab":
            event.stop()
            event.prevent_default()
            self._accept_completion()
            return
        if popup_open and key in {"up", "down"}:
            event.stop()
            event.prevent_default()
            self._completion.move_highlight(-1 if key == "up" else 1)
            return
        if popup_open and key == "escape":
            event.stop()
            event.prevent_default()
            self._hide_completion()
            return
        if key in {"enter", "ctrl+m"}:
            event.stop()
            event.prevent_default()
            self._submit()
            return
        if key in {"shift+enter", "ctrl+j"}:
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key in {"up", "down"} and self._selection_is_empty():
            if key == "up" and self._can_recall_previous():
                event.stop()
                event.prevent_default()
                self._recall_previous()
                return
            if key == "down" and self._history_index is not None and self._at_last_line():
                event.stop()
                event.prevent_default()
                self._recall_next()
                return
        await super()._on_key(event)

    def _selection_is_empty(self) -> bool:
        return self.selection.start == self.selection.end

    def _at_last_line(self) -> bool:
        return self.cursor_location[0] >= self.document.line_count - 1

    def _can_recall_previous(self) -> bool:
        return bool(self._history.entries) and self._at_first_line()

    def _at_first_line(self) -> bool:
        return self.cursor_location[0] == 0

    def _recall_previous(self) -> None:
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self._history.entries) - 1
        else:
            self._history_index = max(0, self._history_index - 1)
        self._restore(self._history.entries[self._history_index])

    def _recall_next(self) -> None:
        if self._history_index is None:
            return
        if self._history_index >= len(self._history.entries) - 1:
            self._history_index = None
            self._restore(self._draft)
        else:
            self._history_index += 1
            self._restore(self._history.entries[self._history_index])

    def _restore(self, text: str) -> None:
        self._suppress_next_completion = True
        self.text = text
        self.move_cursor(self.document.end)

    def _submit(self) -> None:
        text = self.text
        if not text.strip():
            return
        self._history.append(text)
        self._history_index = None
        self._draft = ""
        self._hide_completion()
        self.clear()
        self.post_message(self.Submitted(text))

    def _update_completion(self) -> None:
        prefix = completion_prefix(self.text)
        if prefix is None:
            self._hide_completion()
            return
        matches = candidates(prefix)
        if not matches:
            self._hide_completion()
            return
        lowered = prefix.lower()
        if len(matches) == 1:
            only = matches[0]
            if lowered == only.name or lowered in only.aliases:
                self._hide_completion()
                return
        self._completion.show_items(
            [(command.name, "/" + command.name + "  " + command.summary) for command in matches]
        )
        self._place_completion(len(matches))

    def _place_completion(self, rows: int) -> None:
        if not self._popup_ready:
            return
        if rows <= 0:
            return
        height = min(rows, MAX_VISIBLE_OPTIONS) + BORDER_HEIGHT
        width = max(28, min(self.region.width, 72))
        self._completion.styles.width = width
        self._completion.styles.height = height
        self._completion.styles.offset = (
            self.region.x,
            max(0, self.region.y - height),
        )

    def _accept_completion(self) -> None:
        value = self._completion.selected_value
        self._hide_completion()
        if value is None:
            return
        self.text = "/" + value + " "
        self.move_cursor(self.document.end)

    def _hide_completion(self) -> None:
        if self._completion.is_open:
            self._completion.hide_popup()


__all__ = ["BORDER_HEIGHT", "MAX_VISIBLE_LINES", "PromptArea"]
