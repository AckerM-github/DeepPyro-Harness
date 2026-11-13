"""Conversation stream: user blocks, agent processes, assistant markdown.

The widget is a thin rendering layer. It never talks to the agent directly:
DeepPyroApp forwards agent events to the methods below, always on the Textual
event loop, so all widget mutation stays single-threaded.

Every user turn owns exactly one AgentProcess block. It groups the reasoning
steps and the tool calls of that turn in the order they happened: while the turn
runs the body is expanded with one line per step and one line per tool call, and
when the turn ends it collapses to a single summary line. The final assistant
answer is rendered after the process block.

Reasoning and tool history are already durable in the session log, so replaying
a session rebuilds the same collapsed blocks through add_history_process.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.widgets import Markdown, Static

from deeppyro.tui.history import TurnProcess, TurnReasoning
from deeppyro.tui.widgets.agent_process import AgentProcess

STREAM_REFRESH_SECONDS = 0.06


class AssistantBlock(Vertical):
    """One assistant reply: optional label, then streaming markdown."""

    def __init__(self, *, show_label: bool = True) -> None:
        super().__init__(classes="assistant-block")
        self._show_label = show_label
        self._text = ""
        self._last_refresh = 0.0
        self._finished = False
        self._mounted = False
        self._markdown: Markdown | None = None

    def compose(self) -> ComposeResult:
        if self._show_label:
            yield Static("DeepPyro", classes="block-label block-label-assistant")
        yield Markdown("", classes="assistant-markdown")

    def on_mount(self) -> None:
        self._markdown = self.query_one(Markdown)
        self._mounted = True
        # Events may land before the widget finishes mounting; flush then.
        self._refresh_preview(force=True)

    # -- streaming ------------------------------------------------------
    def append_text(self, chunk: str) -> None:
        if not chunk:
            return
        self._text += chunk
        if self._mounted:
            self._refresh_preview()

    def finish(self, final_text: str | None = None) -> None:
        if final_text:
            self._text = final_text
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
    """Scrollable log of user blocks, agent processes and assistant answers."""

    def __init__(self) -> None:
        super().__init__(id="conversation")
        self._assistant: AssistantBlock | None = None
        self._process: AgentProcess | None = None
        self._process_turn = 0
        self._turn_has_assistant = False

    # -- user / info ----------------------------------------------------
    def add_user(self, text: str) -> None:
        self._finish_process()
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

    # -- agent process --------------------------------------------------
    def begin_step(self, turn: int, step: int) -> None:
        """Start a new step inside the current per-turn agent process."""
        process = self._ensure_process(turn)
        process.begin_step(step)
        self._scroll_to_end()

    def stream_thinking(self, chunk: str) -> None:
        if not chunk:
            return
        process = self._ensure_process(self._process_turn or 1)
        process.append_thinking(chunk)
        self._scroll_to_end()

    def _ensure_process(self, turn: int) -> AgentProcess:
        process = self._process
        if process is not None and not process.finished and (not turn or process.turn == turn):
            return process
        if process is not None:
            process.finish()
        number = turn or self._process_turn or 1
        process = AgentProcess(turn=number)
        self._process = process
        self._process_turn = number
        self.mount(process)
        return process

    def _finish_process(self) -> None:
        process = self._process
        if process is None:
            return
        process.finish()
        self._process = None

    def finish_turn(self, final_text: str | None = None) -> None:
        """Close the agent process block and the final assistant answer."""
        self.finish_assistant(final_text)
        self._finish_process()

    @property
    def current_reasoning(self) -> TurnReasoning | None:
        """Reasoning accumulated for the current turn, if any."""
        process = self._process
        return None if process is None else process.reasoning

    @property
    def process(self) -> AgentProcess | None:
        """The live agent process block of the current turn, if any."""
        return self._process

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
        process = self._ensure_process(self._process_turn or 1)
        process.add_tool_call(call_id, name, arguments)
        self._scroll_to_end()

    def add_tool_result(self, call_id: str, is_error: bool, text: str) -> None:
        self.finish_assistant()
        process = self._ensure_process(self._process_turn or 1)
        process.add_tool_result(call_id, is_error, text)
        self._scroll_to_end()

    # -- history --------------------------------------------------------
    def add_history_process(self, turn_process: TurnProcess) -> None:
        """Render one finished agent process while replaying a session."""
        self._finish_process()
        process = AgentProcess(turn=turn_process.turn)
        for entry in turn_process.entries:
            if entry.kind == "step":
                process.begin_step(entry.step)
                if entry.text:
                    process.append_thinking(entry.text)
            elif entry.kind == "tool" and entry.tool is not None:
                tool = entry.tool
                process.add_tool_call(tool.call_id, tool.name, tool.arguments)
                if tool.has_result:
                    process.add_tool_result(tool.call_id, tool.is_error, tool.result_text)
        process.finish()
        self.mount(process)
        self._scroll_to_end()

    # -- lifecycle ------------------------------------------------------
    def clear(self) -> None:
        self.remove_children()
        self._assistant = None
        self._process = None
        self._process_turn = 0
        self._turn_has_assistant = False

    def _scroll_to_end(self) -> None:
        self.call_after_refresh(self.scroll_end, animate=False)


__all__ = ["AssistantBlock", "ConversationView"]
