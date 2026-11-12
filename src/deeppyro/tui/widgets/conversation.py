"""Conversation stream: user blocks, streaming assistant markdown, tool cards.

The widget is a thin rendering layer. It never talks to the agent directly:
DeepPyroApp forwards agent events to the methods below, always on the Textual
event loop, so all widget mutation stays single-threaded.

Assistant replies are grouped per user turn: the first assistant block of a turn
carries the "DeepPyro" label, later blocks produced by tool steps do not, so a
multi-step turn reads as one answer instead of repeated headers.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Markdown, Static

from deeppyro.tui.widgets.thinking import ThinkingBlock
from deeppyro.tui.widgets.tool_card import ToolCallCard

STREAM_REFRESH_SECONDS = 0.06


class AssistantBlock(Vertical):
    """One assistant reply: optional reasoning, then streaming markdown."""

    def __init__(self, *, show_label: bool = True) -> None:
        super().__init__(classes="assistant-block")
        self._show_label = show_label
        self._text = ""
        self._last_refresh = 0.0
        self._finished = False
        self._mounted = False
        self._markdown: Markdown | None = None
        self._thinking = ThinkingBlock()

    def compose(self) -> ComposeResult:
        if self._show_label:
            yield Static("DeepPyro", classes="block-label block-label-assistant")
        yield self._thinking
        yield Markdown("", classes="assistant-markdown")

    def on_mount(self) -> None:
        self._markdown = self.query_one(Markdown)
        self._mounted = True
        # Events may land before the widget finishes mounting; flush then.
        self._refresh_preview(force=True)

    # -- streaming ------------------------------------------------------
    def append_thinking(self, chunk: str) -> None:
        if not chunk:
            return
        self._thinking.append(chunk)

    def append_text(self, chunk: str) -> None:
        if not chunk:
            return
        self._text += chunk
        if self._mounted:
            self._refresh_preview()

    def finish(self, final_text: str | None = None) -> None:
        if final_text:
            self._text = final_text
        self._thinking.finish()
        self._finished = True
        self._refresh_preview(force=True)

    @property
    def text(self) -> str:
        """Accumulated assistant text (used by tests and history rendering)."""
        return self._text

    def _refresh_preview(self, *, force: bool = False) -> None:
        if not self._mounted or self._markdown is None:
            return
        now = time.monotonic()
        if not force and now - self._last_refresh < STREAM_REFRESH_SECONDS:
            return
        self._last_refresh = now
        has_text = bool(self._text.strip())
        self._markdown.display = has_text
        if not has_text:
            return
        try:
            self._markdown.update(self._text)
        except Exception:  # noqa: BLE001 - partial markdown must never break the stream
            self._markdown.update(Text(self._text))


class ConversationView(ScrollableContainer):
    """Scrollable log of user, assistant and tool blocks."""

    def __init__(self) -> None:
        super().__init__(id="conversation")
        self._assistant: AssistantBlock | None = None
        self._tool_cards: dict[str, ToolCallCard] = {}
        self._turn_has_assistant = False

    # -- user / info ----------------------------------------------------
    def add_user(self, text: str) -> None:
        block = Vertical(
            Static("You", classes="block-label"),
            Static(Text(text), classes="user-text"),
            classes="user-block",
        )
        self._turn_has_assistant = False
        self.mount(block)
        self._scroll_to_end()

    def add_info(self, text: str) -> None:
        self.mount(Static(Text(text, style="dim"), classes="system-block"))
        self._scroll_to_end()

    def add_error(self, text: str) -> None:
        self.mount(Static(Text("error: " + text, style="bold red"), classes="system-block system-error"))
        self._scroll_to_end()

    # -- assistant ------------------------------------------------------
    def _ensure_assistant(self) -> AssistantBlock:
        if self._assistant is None:
            block = AssistantBlock(show_label=not self._turn_has_assistant)
            self._turn_has_assistant = True
            self._assistant = block
            self.mount(block)
        return self._assistant

    def stream_text(self, chunk: str) -> None:
        self._ensure_assistant().append_text(chunk)
        self._scroll_to_end()

    def stream_thinking(self, chunk: str) -> None:
        self._ensure_assistant().append_thinking(chunk)
        self._scroll_to_end()

    def finish_assistant(self, final_text: str | None = None) -> None:
        if self._assistant is None and not final_text:
            return
        block = self._ensure_assistant()
        block.finish(final_text)
        self._assistant = None
        self._scroll_to_end()

    # -- tools ----------------------------------------------------------
    def add_tool_call(self, call_id: str, name: str, arguments: str) -> None:
        self.finish_assistant()
        card = ToolCallCard(call_id, name, arguments)
        if call_id:
            self._tool_cards[call_id] = card
        self.mount(card)
        self._scroll_to_end()

    def add_tool_result(self, call_id: str, is_error: bool, text: str) -> None:
        self.finish_assistant()
        card = self._tool_cards.get(call_id)
        if card is None:
            # A result without a preceding call still deserves a visible card
            # (for example when replaying a partial event stream).
            card = ToolCallCard(call_id, "tool", "")
            if call_id:
                self._tool_cards[call_id] = card
            self.mount(card)
        card.set_result(text, is_error)
        self._scroll_to_end()

    # -- lifecycle ------------------------------------------------------
    def clear(self) -> None:
        self.remove_children()
        self._assistant = None
        self._tool_cards.clear()
        self._turn_has_assistant = False

    def _scroll_to_end(self) -> None:
        self.call_after_refresh(self.scroll_end, animate=False)


__all__ = ["AssistantBlock", "ConversationView"]
