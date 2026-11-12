"""Search tools: glob (path discovery) and grep (content search).

Both are pure-Python implementations calibrated against the DSH fixtures:

* glob returns files only (never directories), newest modification time first,
  caps at 100 results, and reports "No files found" when nothing matches.
* grep groups matching lines per file as "Line N: preview", caps at 250 matches,
  and reports "Found <shown> of <total> matches" when capped.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.args import optional_str, require_str
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.paths import display_path, resolve_path

VCS_DIRS = frozenset({".git", ".hg", ".svn", ".bzr", "CVS"})
DEFAULT_GLOB_MAX_RESULTS = 100
DEFAULT_GREP_MAX_MATCHES = 250
MAX_LINE_PREVIEW = 2000
DEFAULT_GREP_TIMEOUT_MS = 30_000


def _resolve_base(ctx: ToolContext, path_arg: str | None) -> Path:
    if path_arg is None:
        return Path(ctx.cwd).resolve(strict=False)
    return resolve_path(Path(ctx.cwd), path_arg)


class GlobTool(BaseTool):
    """Find files whose paths match a glob pattern."""

    name = "glob"
    description = (
        "Find files, not directories, whose paths match a glob pattern, including hidden and ignored files. "
        "Returns up to 100 paths in modification-time order; a larger result keeps the first paths and reports "
        "where the complete list was saved. Patterns without a path separator match the basename at any depth "
        "under path (which defaults to the session working directory)."
    )
    parameters = make_parameters(
        {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match file paths against, e.g. src/**/*.ts.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (defaults to the session workspace).",
            },
        },
        required=["pattern"],
    )

    def __init__(self, max_results: int = DEFAULT_GLOB_MAX_RESULTS) -> None:
        self.max_results = max_results

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = require_str(args, "pattern").replace("\\", "/")
        if Path(pattern).is_absolute():
            raise ToolError("pattern must be a relative glob, not an absolute path")
        base = _resolve_base(ctx, optional_str(args, "path"))
        if not base.exists():
            raise ToolError(f"search path does not exist: {base}")
        if not base.is_dir():
            raise ToolError(f"search path is not a directory: {base}")
        matches: list[tuple[Path, float]] = []
        try:
            candidates = base.glob(pattern) if "/" in pattern else base.rglob(pattern)
            for candidate in candidates:
                try:
                    relative = candidate.relative_to(base)
                except ValueError:
                    continue
                if any(part in VCS_DIRS for part in relative.parts):
                    continue
                try:
                    if not candidate.is_file():
                        continue
                    mtime = candidate.stat().st_mtime
                except OSError:
                    continue
                matches.append((candidate, mtime))
        except (OSError, ValueError) as exc:
            raise ToolError(f"glob failed: {exc}") from exc
        matches.sort(key=lambda item: (-item[1], str(item[0])))
        shown = matches[: self.max_results]
        if not shown:
            return ToolResult(text="No files found", meta={"count": 0, "returned": 0})
        cwd = Path(ctx.cwd)
        text = "\n".join(display_path(candidate, cwd) for candidate, _ in shown)
        if len(matches) > len(shown):
            text += (
                f"\n\n(Showing the first {len(shown)} of {len(matches)} files. Refine the pattern for more.)"
            )
        return ToolResult(text=text, meta={"count": len(matches), "returned": len(shown)})


class GrepTool(BaseTool):
    """Search file contents with a regular expression."""

    name = "grep"
    description = (
        "Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, "
        "grouped by file. Returns up to 250 matches; a larger result reports where the complete match list "
        "was saved. include is a single positive glob filter and does not support lists or negation."
    )
    parameters = make_parameters(
        {
            "pattern": {
                "type": "string",
                "description": "Regular expression (ripgrep syntax) to search for.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search. Defaults to the session workspace.",
            },
            "include": {
                "type": "string",
                "description": "One glob filter for which files to search, e.g. *.ts.",
            },
        },
        required=["pattern"],
    )

    def __init__(
        self,
        max_matches: int = DEFAULT_GREP_MAX_MATCHES,
        max_line_length: int = MAX_LINE_PREVIEW,
        timeout_ms: int = DEFAULT_GREP_TIMEOUT_MS,
    ) -> None:
        self.max_matches = max_matches
        self.max_line_length = max_line_length
        self.timeout_ms = timeout_ms

    def _iter_files(self, base: Path, include: str | None) -> list[Path]:
        if base.is_file():
            return [base]
        files: list[Path] = []
        for root, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(name for name in dirnames if name not in VCS_DIRS)
            for filename in sorted(filenames):
                if include is not None and not fnmatch.fnmatch(filename, include):
                    continue
                files.append(Path(root) / filename)
        return files

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = require_str(args, "pattern")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression {pattern!r}: {exc}") from exc
        base = _resolve_base(ctx, optional_str(args, "path"))
        if not base.exists():
            raise ToolError(f"search path does not exist: {base}")
        include = optional_str(args, "include")
        if include is not None:
            if any(sep in include for sep in (",", ";")):
                raise ToolError("include must be a single glob pattern, not a list")
            if include.startswith("!"):
                raise ToolError("include must be a single glob pattern; negation is not supported")
        cwd = Path(ctx.cwd)
        groups: dict[str, list[str]] = {}
        total = 0
        shown = 0
        deadline = time.monotonic() + self.timeout_ms / 1000
        for file in self._iter_files(base, include):
            if time.monotonic() > deadline:
                raise ToolError(f"grep timed out after {self.timeout_ms}ms")
            try:
                raw = file.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue
            try:
                text_lines = raw.decode("utf-8", errors="replace").splitlines()
            except ValueError:
                continue
            display = display_path(file, cwd)
            for lineno, line in enumerate(text_lines, 1):
                if not compiled.search(line):
                    continue
                total += 1
                if shown >= self.max_matches:
                    continue
                shown += 1
                preview = line[: self.max_line_length]
                groups.setdefault(display, []).append(f"Line {lineno}: {preview}")
        if not groups:
            return ToolResult(text="No matches found", meta={"matches": 0, "returned": 0, "files": 0})
        header = f"Found {total} matches" if shown >= total else f"Found {shown} of {total} matches"
        blocks = [display + "\n" + "\n".join(entries) for display, entries in groups.items()]
        text = header + "\n\n" + "\n\n".join(blocks)
        if shown < total:
            text += f"\n\n(Showing the first {shown} of {total} matches. Refine the pattern for more.)"
        return ToolResult(text=text, meta={"matches": total, "returned": shown, "files": len(groups)})


__all__ = ["DEFAULT_GLOB_MAX_RESULTS", "DEFAULT_GREP_MAX_MATCHES", "VCS_DIRS", "GlobTool", "GrepTool"]
