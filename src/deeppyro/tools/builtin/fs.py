"""Filesystem tools: read, write, edit.

Output formats follow the DSH tools observed in fixtures: read renders an XML
envelope with line-numbered content and a pagination footer; write reports
"Created file" or "Updated file"; edit performs a literal replacement and
reports success as a single sentence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.args import optional_bool, optional_int, require_str
from deeppyro.tools.base import BaseTool, make_parameters, sandbox_properties
from deeppyro.tools.paths import resolve_path
from deeppyro.tools.permissions import check_read, check_write

DEFAULT_READ_LIMIT = 2000
MAX_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        raise ToolError(f'cannot read "{path}": not found') from None
    except IsADirectoryError:
        raise ToolError(f'cannot read "{path}": not a regular file') from None
    except OSError as exc:
        raise ToolError(f'cannot read "{path}": {exc.strerror or exc}') from exc


def _preview_line(line: str, limit: int = MAX_LINE_LENGTH) -> str:
    line = line.rstrip("\r")
    if limit > 0 and len(line) > limit:
        return line[:limit] + " [line truncated]"
    return line


class ReadTool(BaseTool):
    """Read a UTF-8 text file and return line-numbered content."""

    name = "read"
    description = (
        "Read a UTF-8 text file and return line-numbered content. "
        "Large files are paginated with offset (1-based) and limit (default 2000 lines)."
    )
    parameters = make_parameters(
        {
            "file_path": {
                "type": "string",
                "description": "Path to read, resolved by the filesystem backend.",
            },
            "offset": {"type": "number", "description": "1-based first line to return. Defaults to 1."},
            "limit": {
                "type": "number",
                "description": "Maximum number of lines to return. Defaults to 2000.",
            },
        },
        required=["file_path"],
    )

    def __init__(self, max_line_length: int = MAX_LINE_LENGTH) -> None:
        self.max_line_length = max_line_length

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(Path(ctx.cwd), require_str(args, "file_path"))
        check_read(ctx, path)
        offset = optional_int(args, "offset", 1, minimum=1)
        limit = optional_int(args, "limit", DEFAULT_READ_LIMIT, minimum=1, maximum=MAX_READ_LIMIT)
        if not path.exists():
            raise ToolError(f'cannot read "{path}": not found')
        if not path.is_file():
            raise ToolError(f'cannot read "{path}": not a regular file')
        data = _read_bytes(path)
        text = data.decode("utf-8", errors="replace")
        text = text.removesuffix("\n")
        lines = text.split("\n") if text else []
        total = len(lines)
        if offset > 1 and offset > total:
            raise ToolError(f'"offset" {offset} is beyond the end of the file: {path} has {total} lines')
        if limit > MAX_READ_LIMIT:  # defensive: optional_int already caps it
            raise ToolError(f"limit must be less than or equal to {MAX_READ_LIMIT}")
        selected = lines[offset - 1 : offset - 1 + limit]
        truncated = len(selected) < len(lines[offset - 1 :])
        body = "\n".join(
            f"{number}: {_preview_line(line, self.max_line_length)}"
            for number, line in enumerate(selected, offset)
        )
        content = f"{body}\n" if body else ""
        end = offset + len(selected) - 1
        if not truncated:
            footer = f"(End of file - total {total} lines)"
        elif offset > 1:
            footer = f"(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
        else:
            footer = f"(Output capped. Showing lines {offset}-{end}. Use offset={end + 1} to continue.)"
        text_out = f"<path>{path}</path>\n<type>file</type>\n<content>\n{content}</content>\n{footer}"
        return ToolResult(
            text=text_out,
            meta={"path": str(path), "totalLines": total, "offset": offset, "returned": len(selected)},
        )


class WriteTool(BaseTool):
    """Create or fully replace a UTF-8 text file."""

    name = "write"
    description = "Create or fully replace a UTF-8 text file."
    parameters = make_parameters(
        sandbox_properties(
            {
                "file_path": {
                    "type": "string",
                    "description": "Path to write, resolved by the filesystem backend.",
                },
                "content": {"type": "string", "description": "Full UTF-8 text content to write."},
            }
        ),
        required=["file_path", "content"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(Path(ctx.cwd), require_str(args, "file_path"))
        content = require_str(args, "content", allow_empty=True)
        await check_write(ctx, args, path, action="write")
        if path.is_dir():
            raise ToolError(f'cannot write "{path}": not a regular file')
        existed = path.exists()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            raise ToolError(f'cannot write "{path}": {exc.strerror or exc}') from exc
        status = "Updated file" if existed else "Created file"
        text = f"<path>{path}</path>\n<type>file</type>\n<content>\n{status}\n</content>"
        return ToolResult(
            text=text, meta={"path": str(path), "created": not existed, "bytes": len(content.encode("utf-8"))}
        )


class EditTool(BaseTool):
    """Edit an existing UTF-8 text file by replacing literal text."""

    name = "edit"
    description = (
        "Edit an existing UTF-8 text file by replacing literal text. "
        "By default old_string must match exactly once; set replace_all to replace every occurrence."
    )
    parameters = make_parameters(
        sandbox_properties(
            {
                "file_path": {
                    "type": "string",
                    "description": "Path to edit, resolved by the filesystem backend.",
                },
                "old_string": {"type": "string", "description": "Literal text to replace."},
                "new_string": {
                    "type": "string",
                    "description": "Literal replacement text. Use an empty string to delete the match.",
                },
                "replace_all": {"type": "boolean", "description": "Replace all matches. Defaults to false."},
            }
        ),
        required=["file_path", "old_string", "new_string"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve_path(Path(ctx.cwd), require_str(args, "file_path"))
        old_string = require_str(args, "old_string")
        new_string = require_str(args, "new_string", allow_empty=True)
        replace_all = optional_bool(args, "replace_all", False)
        await check_write(ctx, args, path, action="edit")
        if path.is_dir():
            raise ToolError(f'cannot edit "{path}": not a regular file')
        if not path.exists():
            raise ToolError(f'cannot read "{path}": not found')
        try:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                original = handle.read()
        except OSError as exc:
            raise ToolError(f'cannot read "{path}": {exc.strerror or exc}') from exc
        occurrences = original.count(old_string)
        if occurrences == 0:
            raise ToolError(f"string to replace was not found in {path}")
        if occurrences > 1 and not replace_all:
            raise ToolError(
                f"string to replace is not unique in {path}: {occurrences} occurrences; "
                "pass replace_all=true to replace every occurrence"
            )
        updated = (
            original.replace(old_string, new_string)
            if replace_all
            else original.replace(old_string, new_string, 1)
        )
        try:
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
        except OSError as exc:
            raise ToolError(f'cannot write "{path}": {exc.strerror or exc}') from exc
        return ToolResult(
            text=f"The file {path} has been updated successfully.",
            meta={"path": str(path), "replacements": occurrences if replace_all else 1},
        )


__all__ = ["MAX_READ_LIMIT", "EditTool", "ReadTool", "WriteTool"]
