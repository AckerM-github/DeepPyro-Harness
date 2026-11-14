"""One collapsible agent process block per user turn.

A turn is more than the final answer: the model thinks, calls tools, reads the
results and thinks again. This widget keeps that whole trail in one place. The
conversation only shows a single live header line: the newest reasoning sentence
of the current step, or the tool call that is currently running. The detailed
trail (one line per thinking step and one line per tool call, in the order they
happened) stays collapsed so it never pushes the answer off screen; clicking the
header expands it for inspection. When the turn ends the header becomes one
summary line, so the conversation stays readable.

Interaction:
- clicking the header expands or collapses the detailed trail
- clicking a thinking step opens the full-screen reasoning viewer
- clicking a tool line expands its complete arguments and result

The widget is pure presentation. It never talks to the agent loop or the session
log; ConversationView feeds it through begin_step, append_thinking,
add_tool_call, add_tool_result and finish.
"""

from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static

from deeppyro.tui.format import compact_text
from deeppyro.tui.history import ReasoningStep, TurnReasoning
from deeppyro.tui.widgets.thinking import STREAMING_LINE_CHARS, latest_sentence
from deeppyro.tui.widgets.tool_card import (
    RUN_CODE_NAME,
    clean_result_text,
    run_code_detail,
    summarize_arguments,
)

#: Header and step lines are clipped to one terminal line.
MAX_LINE_CHARS = 120
#: Result preview shown on a collapsed tool line.
TOOL_PREVIEW_CHARS = 80
#: Detail caps for an expanded tool line.
DETAIL_ARG_LINES = 20
DETAIL_ARG_CHARS = 2000
DETAIL_RESULT_LINES = 40
DETAIL_RESULT_CHARS = 4000


def _one_line(text: str, *, limit: int = MAX_LINE_CHARS) -> str:
    """Collapse whitespace and keep the newest characters on a single line."""
    line = " ".join(str(text or "").split())
    if len(line) <= limit:
        return line
    if limit <= 1:
        return line[:limit]
    return "…" + line[-(limit - 1) :]


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def _pretty_arguments(arguments: str | dict[str, Any] | None) -> str:
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False, indent=2)
    raw = str(arguments or "").strip()
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return raw


def _preview_result(text: str) -> str:
    cleaned = clean_result_text(text)
    return compact_text(cleaned, max_lines=1, max_chars=TOOL_PREVIEW_CHARS)


class ToolProcessLine(Vertical):
    """One tool call inside an agent process: a line plus optional detail."""

    DEFAULT_CSS = """
    ToolProcessLine {
        height: auto;
        width: 1fr;
    }
    ToolProcessLine .process-tool-line {
        height: 1;
        padding: 0 1 0 2;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    ToolProcessLine .process-tool-line:hover {
        color: $text;
    }
    ToolProcessLine .process-tool-detail {
        height: auto;
        padding: 0 1 0 3;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, call_id: str, name: str, arguments: str | dict[str, Any] | None) -> None:
        super().__init__(classes="process-tool")
        self.call_id = call_id
        self.tool_name = (name or "tool").strip() or "tool"
        self.arguments = arguments
        self.result_text = ""
        self.is_error = False
        self.has_result = False
        self._expanded = False
        self._mounted = False
        self._line_widget: Static | None = None
        self._detail_widget: Static | None = None

    def compose(self) -> ComposeResult:
        yield Static("", classes="process-tool-line")
        yield Static("", classes="process-tool-detail")

    def on_mount(self) -> None:
        self._line_widget = self.query_one(".process-tool-line", Static)
        self._detail_widget = self.query_one(".process-tool-detail", Static)
        self._mounted = True
        self._refresh()

    # -- state ----------------------------------------------------------
    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def line_text(self) -> str:
        if self._line_widget is None:
            return self._build_line().plain
        content = self._line_widget.content
        plain = getattr(content, "plain", None)
        return str(plain) if plain is not None else str(content)

    # -- updates --------------------------------------------------------
    def set_result(self, text: str, is_error: bool) -> None:
        self.result_text = text or ""
        self.is_error = bool(is_error)
        self.has_result = True
        self._refresh()

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh()

    def on_click(self, event: events.Click) -> None:
        self.toggle()
        event.stop()

    # -- rendering ------------------------------------------------------
    def _refresh(self) -> None:
        if not self._mounted or self._line_widget is None or self._detail_widget is None:
            return
        self._line_widget.update(self._build_line())
        self._detail_widget.update(self._build_detail())
        self._detail_widget.display = self._expanded

    def _build_line(self) -> Text:
        text = Text()
        summary = summarize_arguments(self.tool_name, self.arguments)
        if self.is_error:
            text.append("✗ ", style="bold red")
            text.append(self.tool_name, style="bold red")
        elif self.has_result:
            text.append("✓ ", style="bold green")
            text.append(self.tool_name, style="bold green")
        else:
            text.append("→ ", style="bold cyan")
            text.append(self.tool_name, style="bold cyan")
        if summary:
            text.append("  " + summary, style="dim")
        if self.has_result:
            preview = _preview_result(self.result_text)
            if self.is_error:
                text.append("  failed", style="dim red")
                if preview:
                    text.append("  · " + preview, style="red")
            elif preview:
                text.append("  · " + preview, style="dim")
        return Text(_one_line(text.plain))

    def _build_detail(self) -> Text:
        text = Text()
        args = ""
        if self.tool_name.strip().lower() == RUN_CODE_NAME:
            args = run_code_detail(self.arguments)
        if not args:
            args = compact_text(
                _pretty_arguments(self.arguments),
                max_lines=DETAIL_ARG_LINES,
                max_chars=DETAIL_ARG_CHARS,
            )
        if args:
            text.append("arguments\n", style="bold")
            text.append(args, style="dim")
        if self.has_result:
            if text.plain:
                text.append("\n\n")
            text.append("result\n", style="bold red" if self.is_error else "bold")
            result = compact_text(
                clean_result_text(self.result_text),
                max_lines=DETAIL_RESULT_LINES,
                max_chars=DETAIL_RESULT_CHARS,
            )
            text.append(result or "(empty)", style="red" if self.is_error else "dim")
        if not text.plain:
            text.append("(no arguments or result yet)", style="dim")
        return text


class AgentProcess(Vertical):
    """One user turn's thinking steps and tool calls, collapsible as a unit."""

    class ThinkingRequested(Message):
        """Posted when a thinking step line is clicked."""

        def __init__(self, reasoning: TurnReasoning) -> None:
            super().__init__()
            self.reasoning = reasoning

    DEFAULT_CSS = """
    AgentProcess {
        height: auto;
        width: 1fr;
        margin: 1 0 0 1;
    }
    AgentProcess .process-header {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    AgentProcess .process-header:hover {
        color: $text;
    }
    AgentProcess .process-body {
        height: auto;
        width: 1fr;
    }
    AgentProcess .process-step {
        height: 1;
        padding: 0 1 0 2;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        overflow: hidden;
    }
    AgentProcess .process-step:hover {
        color: $text;
    }
    """

    def __init__(self, turn: int = 0, reasoning: TurnReasoning | None = None) -> None:
        super().__init__(classes="agent-process")
        self._reasoning = reasoning if reasoning is not None else TurnReasoning(turn=turn)
        if not self._reasoning.turn and turn:
            self._reasoning.turn = turn
        self._current: ReasoningStep | None = None
        self._entries: list[tuple[str, Any]] = []
        self._tools: dict[str, ToolProcessLine] = {}
        self._step_widgets: dict[int, Static] = {}
        self._widget_steps: dict[int, int] = {}
        self._mounted = False
        self._finished = False
        self._expanded = False
        self._header_widget: Static | None = None
        self._body_widget: Vertical | None = None

    def compose(self) -> ComposeResult:
        yield Static("", classes="process-header")
        yield Vertical(classes="process-body")

    def on_mount(self) -> None:
        self._header_widget = self.query_one(".process-header", Static)
        self._body_widget = self.query_one(".process-body", Vertical)
        self._mounted = True
        for kind, value in self._entries:
            if kind == "step":
                self._mount_step(value.step)
            elif kind == "tool":
                self._body_widget.mount(value)
        self._refresh()

    # -- state ----------------------------------------------------------
    @property
    def turn(self) -> int:
        return self._reasoning.turn

    @property
    def reasoning(self) -> TurnReasoning:
        return self._reasoning

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def step_count(self) -> int:
        return len(self._reasoning.steps)

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    @property
    def sentence_count(self) -> int:
        return self._reasoning.sentence_count

    @property
    def has_text(self) -> bool:
        return any(step.text.strip() for step in self._reasoning.steps)

    @property
    def header_text(self) -> str:
        if self._header_widget is None:
            return self._build_header().plain
        content = self._header_widget.content
        plain = getattr(content, "plain", None)
        return str(plain) if plain is not None else str(content)

    @property
    def line_text(self) -> str:
        return self.header_text

    # -- lifecycle ------------------------------------------------------
    def begin_step(self, step: int) -> None:
        number = int(step)
        if self._current is not None and self._current.step == number:
            return
        item = ReasoningStep(step=number)
        self._reasoning.steps.append(item)
        self._current = item
        self._entries.append(("step", item))
        if self._mounted:
            self._mount_step(number)
        self._refresh()

    def append_thinking(self, chunk: str) -> None:
        if not chunk:
            return
        if self._current is None:
            self.begin_step(1)
        current = self._current
        if current is None:  # pragma: no cover - begin_step always sets it
            return
        current.text += str(chunk)
        self._refresh()

    def add_tool_call(self, call_id: str, name: str, arguments: str | dict[str, Any] | None) -> None:
        line = ToolProcessLine(call_id, name, arguments)
        self._tools[call_id] = line
        self._entries.append(("tool", line))
        if self._mounted and self._body_widget is not None:
            self._body_widget.mount(line)
        self._refresh()

    def add_tool_result(self, call_id: str, is_error: bool, text: str) -> None:
        line = self._tools.get(call_id)
        if line is None:
            line = ToolProcessLine(call_id, "tool", "")
            self._tools[call_id] = line
            self._entries.append(("tool", line))
            if self._mounted and self._body_widget is not None:
                self._body_widget.mount(line)
        line.set_result(text, is_error)
        self._refresh()

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._reasoning.finished = True
        self._expanded = False
        self._refresh()

    def toggle(self) -> None:
        if not self.has_content:
            return
        self._expanded = not self._expanded
        self._refresh()

    # -- clicks ---------------------------------------------------------
    def on_click(self, event: events.Click) -> None:
        widget = event.widget
        if widget is self._header_widget:
            self.toggle()
            event.stop()
            return
        step = self._widget_steps.get(id(widget))
        if step is not None and self.has_text:
            self.post_message(self.ThinkingRequested(self._reasoning))
            event.stop()

    # -- rendering ------------------------------------------------------
    @property
    def has_content(self) -> bool:
        return self._reasoning.has_reasoning or bool(self._tools)

    def _mount_step(self, step: int) -> None:
        if not self._mounted or self._body_widget is None or step in self._step_widgets:
            return
        widget = Static("", classes="process-step")
        self._step_widgets[step] = widget
        self._widget_steps[id(widget)] = step
        self._body_widget.mount(widget)

    def _refresh(self) -> None:
        if not self._mounted:
            return
        if self._header_widget is not None:
            self._header_widget.update(self._build_header())
        if self._body_widget is not None:
            self._body_widget.display = self.has_content and self._expanded
        for step in self._reasoning.steps:
            widget = self._step_widgets.get(step.step)
            if widget is not None:
                widget.update(self._build_step(step))
        self.display = self.has_content

    def _build_header(self) -> Text:
        text = Text()
        if not self.has_content:
            return text
        if self._finished:
            text.append(
                f"· agent process · {_plural(self.step_count, 'step')} · "
                f"{_plural(self.tool_count, 'tool')} · {_plural(self.sentence_count, 'sentence')}",
                style="dim italic",
            )
            hint = "  (click to collapse)" if self._expanded else "  (click to view)"
            text.append(hint, style="dim")
            return text
        step = self._current.step if self._current is not None else (self.step_count or 1)
        text.append("✻ ", style="dim")
        text.append(f"Step {step}", style="dim italic")
        active_tool = self._active_tool()
        if active_tool is not None:
            text.append(" · " + active_tool.line_text, style="dim")
        else:
            sentence = self._streaming_sentence()
            if sentence:
                text.append(" · " + sentence, style="dim italic")
        if self.has_content and not self._expanded:
            text.append("  (click to view)", style="dim")
        return Text(_one_line(text.plain))

    def _active_tool(self) -> ToolProcessLine | None:
        for kind, value in reversed(self._entries):
            if kind == "tool" and isinstance(value, ToolProcessLine) and not value.has_result:
                return value
        return None

    def _build_step(self, step: ReasoningStep) -> Text:
        text = Text()
        text.append("✻ ", style="dim")
        text.append(f"Step {step.step}", style="dim italic")
        if self._finished:
            from deeppyro.tui.widgets.thinking import split_sentences

            count = len(split_sentences(step.text))
            text.append(f" · {_plural(count, 'sentence')}", style="dim italic")
            if step.text.strip():
                text.append("  (click to view)", style="dim")
        else:
            sentence = latest_sentence(step.text) or _one_line(step.text)
            if sentence:
                text.append(" · " + sentence, style="dim italic")
        return Text(_one_line(text.plain))

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
        return _one_line(sentence or raw, limit=STREAMING_LINE_CHARS - 10)


ThinkingRequested = AgentProcess.ThinkingRequested

__all__ = ["AgentProcess", "ThinkingRequested", "ToolProcessLine"]
