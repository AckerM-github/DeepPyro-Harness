"""Compact tool cards: one call summary, a short result preview and edit diffs.

The conversation stream renders every tool invocation as one card. The card keeps
the call and its result together, so multi-step turns stay readable instead of
producing a wall of JSON. Everything here is presentation only: no files are
read and no tool is executed.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from deeppyro.tui.format import compact_text

#: Tool names that point at one file path; the call line shows that path.
_PATH_TOOLS = frozenset({"read", "write", "edit", "multi_edit", "multiedit"})
#: Shell-like tools; the call line shows the beginning of the command.
_SHELL_TOOLS = frozenset({"bash", "sh", "shell", "pwsh", "powershell", "cmd", "exec", "command"})
#: Tools whose successful result also renders a small before/after diff.
_DIFF_TOOLS = frozenset({"write", "edit", "multi_edit", "multiedit", "apply_patch", "str_replace"})
#: Maximum number of diff lines rendered per direction.
_MAX_DIFF_LINES = 3
#: Maximum preview lines for a plain tool result.
_MAX_RESULT_LINES = 3
#: Maximum characters per diff line before truncating.
_MAX_DIFF_CHARS = 110


def parse_arguments(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    """Best-effort parse of tool arguments received as a JSON string or mapping.

    Agent events normally carry a JSON object encoded as a string, but tests and
    older providers may hand over a Python repr or a mapping. Unparseable input
    yields an empty mapping so rendering never fails.
    """
    if isinstance(arguments, dict):
        return arguments
    text = str(arguments or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        try:
            value = ast.literal_eval(text)
        except (SyntaxError, TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _first_value(args: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = args.get(key)
        if value not in (None, ""):
            return value
    return None


def summarize_arguments(name: str, arguments: str | dict[str, Any] | None) -> str:
    """Return a one-line summary of a tool call: path, command or short JSON."""
    tool = (name or "tool").strip().lower()
    args = parse_arguments(arguments)
    if args:
        path = _first_value(args, ("path", "file_path", "filePath", "file", "filename"))
        if path is not None and (tool in _PATH_TOOLS or tool not in _SHELL_TOOLS):
            return str(path)
        if tool in _SHELL_TOOLS:
            command = _first_value(args, ("command", "cmd", "script", "input"))
            if command is not None:
                return compact_text(str(command), max_lines=1, max_chars=60)
        rendered = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
        return compact_text(rendered, max_lines=1, max_chars=80)
    return compact_text(str(arguments or ""), max_lines=1, max_chars=80)


def _truncate(line: str, limit: int = _MAX_DIFF_CHARS) -> str:
    line = line.rstrip()
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _edit_pairs(args: dict[str, Any]) -> list[tuple[str, str]]:
    """Extract old/new text pairs from common edit tool schemas."""
    pairs: list[tuple[str, str]] = []
    for old_key, new_key in (
        ("old", "new"),
        ("old_string", "new_string"),
        ("oldString", "newString"),
        ("old_text", "new_text"),
        ("oldText", "newText"),
        ("before", "after"),
    ):
        if old_key in args or new_key in args:
            pairs.append((str(args.get(old_key) or ""), str(args.get(new_key) or "")))
    edits = args.get("edits")
    if isinstance(edits, list):
        for item in edits:
            if not isinstance(item, dict):
                continue
            old = _first_value(item, ("old", "old_string", "oldString", "old_text"))
            new = _first_value(item, ("new", "new_string", "newString", "new_text"))
            if old is not None or new is not None:
                pairs.append((str(old or ""), str(new or "")))
    return pairs


def diff_preview_lines(name: str, arguments: str | dict[str, Any] | None) -> list[tuple[str, str]]:
    """Return (sign, line) pairs for a write/edit call, capped at six lines.

    The diff is inferred from the call arguments only; it is not a real file
    diff. For write calls the new content is shown as added lines. An empty list
    means no safe preview could be inferred.
    """
    tool = (name or "").strip().lower()
    if tool not in _DIFF_TOOLS:
        return []
    args = parse_arguments(arguments)
    if not args:
        return []
    lines: list[tuple[str, str]] = []
    pairs = _edit_pairs(args)
    if pairs:
        for old, new in pairs:
            for raw in old.splitlines()[:_MAX_DIFF_LINES]:
                lines.append(("-", _truncate(raw)))
            for raw in new.splitlines()[:_MAX_DIFF_LINES]:
                lines.append(("+", _truncate(raw)))
            if len(lines) >= 2 * _MAX_DIFF_LINES + 2:
                break
        return lines[: 2 * _MAX_DIFF_LINES + 2]
    content = _first_value(args, ("content", "new_text", "text", "new_string"))
    if content is None:
        return []
    raw_lines = str(content).splitlines()
    for raw in raw_lines[:_MAX_DIFF_LINES]:
        lines.append(("+", _truncate(raw)))
    if len(raw_lines) > _MAX_DIFF_LINES:
        lines.append(("+", "…"))
    return lines


def _diff_label(lines: list[tuple[str, str]]) -> str:
    added = sum(1 for sign, _ in lines if sign == "+")
    removed = sum(1 for sign, _ in lines if sign == "-")
    parts = []
    if removed:
        parts.append(f"-{removed}")
    if added:
        parts.append(f"+{added}")
    return " ".join(parts) or "diff"


#: Payload block used by the filesystem tools: <path>..</path> <type>..</type>
#: <content>..</content> followed by a footer line.
_CONTENT_BLOCK = re.compile(r"<content>\n?(.*?)</content>", re.DOTALL)


def clean_result_text(text: str) -> str:
    """Strip transport wrappers from a result so the preview shows the payload.

    Filesystem tools frame their answer as <path>/<type>/<content> blocks plus a
    footer ("End of file", "Showing lines ..."), so only the content block is
    kept. Simpler results are wrapped in one <content> or <output> element.
    Unknown wrappers are left untouched: showing raw text beats showing nothing.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("<path>"):
        match = _CONTENT_BLOCK.search(cleaned)
        return match.group(1).strip() if match else cleaned
    for opening, closing in (("<content>", "</content>"), ("<output>", "</output>")):
        if cleaned.startswith(opening) and cleaned.endswith(closing):
            cleaned = cleaned[len(opening) : -len(closing)].strip()
    return cleaned


class ToolCallCard(Vertical):
    """One tool invocation: summary line plus result preview or edit diff."""

    def __init__(self, call_id: str, name: str, arguments: str | dict[str, Any] | None) -> None:
        super().__init__(classes="tool-card")
        self.call_id = call_id
        self.tool_name = (name or "tool").strip() or "tool"
        self.arguments = arguments
        self.is_error = False
        self._result_text = ""
        self._result_rendered = False
        self._result_widget: Static | None = None

    @property
    def summary(self) -> str:
        """Argument summary shown in the call line (also used by tests)."""
        return summarize_arguments(self.tool_name, self.arguments)

    def compose(self) -> ComposeResult:
        yield Static(self._call_text(), classes="tool-call")
        yield Static("", classes="tool-result-line")

    def on_mount(self) -> None:
        self._result_widget = self.query_one(".tool-result-line", Static)
        self._result_widget.display = self._result_rendered
        if self._result_rendered:
            self._refresh_result()

    def _call_text(self) -> Text:
        line = Text("→ ", style="bold cyan")
        summary = self.summary
        # The separator belongs to the styled name chunk: leading spaces in a
        # separate chunk are trimmed by some SVG renderers, which made the
        # summary look glued to the tool name in screenshots.
        line.append(self.tool_name + (": " if summary else ""), style="bold cyan")
        if summary:
            line.append(summary, style="dim")
        return line

    def set_result(self, text: str, is_error: bool) -> None:
        """Attach the tool result to this card and render it in place."""
        self._result_text = text or ""
        self.is_error = bool(is_error)
        self._result_rendered = True
        if self._result_widget is not None:
            self._result_widget.display = True
            self._refresh_result()

    def _refresh_result(self) -> None:
        if self._result_widget is None:
            return
        header, body = self._result_parts()
        self._result_widget.update(header if body is None else _stack(header, body))

    def _result_parts(self) -> tuple[Text, Text | None]:
        cleaned = clean_result_text(self._result_text)
        header = Text()
        if self.is_error:
            header.append("✗ ", style="bold red")
            header.append(self.tool_name + "  ", style="bold red")
            header.append("failed", style="dim red")
        else:
            header.append("✓ ", style="bold green")
            header.append(self.tool_name, style="bold green")
        diff = [] if self.is_error else diff_preview_lines(self.tool_name, self.arguments)
        if diff:
            header.append("  ", style="bold green")
            header.append(_diff_label(diff), style="dim")
            return header, _diff_text(diff)
        preview = compact_text(cleaned, max_lines=_MAX_RESULT_LINES, max_chars=400)
        if not preview:
            return header, None
        body = Text()
        lines = preview.splitlines()
        for index, line in enumerate(lines):
            if index:
                body.append("\n")
            body.append(line, style="red" if self.is_error else "dim")
        return header, body


def _diff_text(lines: list[tuple[str, str]]) -> Text:
    body = Text()
    for index, (sign, line) in enumerate(lines):
        if index:
            body.append("\n")
        body.append(sign + " ", style="bold red" if sign == "-" else "bold green")
        body.append(line, style="red" if sign == "-" else "green")
    return body


def _stack(header: Text, body: Text) -> Text:
    combined = header.copy()
    combined.append("\n")
    combined.append_text(body)
    return combined


__all__ = [
    "ToolCallCard",
    "clean_result_text",
    "diff_preview_lines",
    "parse_arguments",
    "summarize_arguments",
]
