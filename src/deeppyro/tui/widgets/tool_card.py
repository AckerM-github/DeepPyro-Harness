"""Rendering helpers for tool calls inside the agent process block.

AgentProcess renders one line per tool invocation: a one-line argument summary
plus a short result preview. These helpers only format data; no files are read
and no tool is executed.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from deeppyro.tui.format import compact_text

#: Tool names that point at one file path; the call line shows that path.
_PATH_TOOLS = frozenset({"read", "write", "edit", "multi_edit", "multiedit"})
#: Shell-like tools; the call line shows the beginning of the command.
_SHELL_TOOLS = frozenset({"bash", "sh", "shell", "pwsh", "powershell", "cmd", "exec", "command"})
#: Tools whose successful result also renders a small before/after diff.
_DIFF_TOOLS = frozenset({"write", "edit", "multi_edit", "multiedit", "apply_patch", "str_replace"})
#: Maximum number of diff lines rendered per direction.
_MAX_DIFF_LINES = 3
#: Maximum characters per diff line before truncating.
_MAX_DIFF_CHARS = 110
#: Reserved PTC transport tool: its arguments carry a description and Python code.
RUN_CODE_NAME = "run_code"
#: Expanded run_code detail shows at most this many code lines.
RUN_CODE_DETAIL_LINES = 20


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


def _run_code_fields(arguments: str | dict[str, Any] | None) -> tuple[str, str]:
    """Return (description, code) from run_code arguments.

    The PTC transport names the program field "code"; older records and the
    current model-facing schema use "program", so both are accepted. The
    description is what the model wrote to explain the call; it is the only
    part shown on the collapsed tool line.
    """
    args = parse_arguments(arguments)
    description = str(args.get("description") or "").strip()
    code = args.get("code")
    if code in (None, ""):
        code = args.get("program")
    source = "" if code in (None, "") else str(code).strip("\n")
    return description, source


def run_code_detail(arguments: str | dict[str, Any] | None) -> str:
    """Return the expanded argument text for a run_code call.

    The description is shown in full. The program source is capped to the first
    RUN_CODE_DETAIL_LINES lines plus a counter for the rest, so a long program
    never floods the transcript. An empty string means the caller should fall
    back to the generic pretty-printed arguments.
    """
    description, source = _run_code_fields(arguments)
    blocks: list[str] = []
    if description:
        blocks.append("description: " + description)
    if source:
        lines = source.splitlines()
        shown = lines[:RUN_CODE_DETAIL_LINES]
        remaining = len(lines) - len(shown)
        code_block = "\n".join(["code (" + str(len(lines)) + " lines):", *shown])
        if remaining > 0:
            code_block += "\n... " + str(remaining) + " more lines"
        blocks.append(code_block)
    return "\n".join(blocks)


def summarize_arguments(name: str, arguments: str | dict[str, Any] | None) -> str:
    """Return a one-line summary of a tool call: path, command or short JSON."""
    tool = (name or "tool").strip().lower()
    if tool == RUN_CODE_NAME:
        description, _ = _run_code_fields(arguments)
        if description:
            return compact_text(description, max_lines=1, max_chars=80)
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


__all__ = [
    "RUN_CODE_DETAIL_LINES",
    "RUN_CODE_NAME",
    "clean_result_text",
    "diff_preview_lines",
    "parse_arguments",
    "run_code_detail",
    "summarize_arguments",
]
