"""System prompt assembly.

The prompt is deliberately deterministic: everything it says comes from the
tool schemas and the PromptContext the caller passes in.  That keeps prompt
snapshots testable and makes the runtime environment (cwd, platform, model,
permissions) explicit instead of hidden in prose.

Instruction files are discovered the way coding agents generally do it: walk
from the working directory up to the filesystem root and load every
AGENTS.md found on the way.  Root-most files come first so that more specific
(deeper) instructions appear later and read as refinements.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from deeppyro.core.protocols import ToolSchema

INSTRUCTION_FILENAME = "AGENTS.md"
DEFAULT_MAX_INSTRUCTION_CHARS = 20_000
_MAX_ENV_ENTRIES = 50


@dataclass(slots=True)
class PromptContext:
    """Everything the system prompt may mention about the current run."""

    cwd: Path
    platform: str
    session_id: str
    started_at: int
    permission_preset: str
    approval_policy: str
    model: str
    instructions: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def _read_instruction_file(path: Path, *, max_chars: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff").strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n[instruction file truncated]"
    return text


def collect_instructions(cwd: Path, *, max_chars_per_file: int = DEFAULT_MAX_INSTRUCTION_CHARS) -> list[str]:
    """Return the AGENTS.md chain for cwd, root-most file first.

    Unreadable or empty files are skipped.  Each returned block is prefixed
    with the absolute path it came from so the model (and tests) can tell
    which instructions belong to which directory.
    """
    try:
        current = Path(cwd).expanduser().resolve(strict=False)
    except OSError:
        current = Path(cwd)
    directories = [current, *current.parents]
    blocks: list[str] = []
    seen: set[Path] = set()
    for directory in reversed(directories):  # root first
        candidate = directory / INSTRUCTION_FILENAME
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        try:
            text = _read_instruction_file(resolved, max_chars=max_chars_per_file)
        except OSError:
            continue
        if text:
            blocks.append(f"Instructions from {resolved}:\n\n{text}")
    return blocks


def _environment_lines(ctx: PromptContext) -> list[str]:
    entries = sorted(ctx.env.items())[:_MAX_ENV_ENTRIES]
    return [f"- {key}={value}" for key, value in entries]


def _tool_section(tools: list[ToolSchema]) -> str:
    if not tools:
        return "## Tools\n\nNo tools are currently registered. Answer directly."
    blocks = [
        "## Tools",
        "",
        "You can call the following tools. Arguments must be a single JSON object matching the schema.",
    ]
    for schema in tools:
        parameters = json.dumps(schema.parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        description = schema.description.strip() or "(no description)"
        blocks.extend(
            [
                "",
                f"### {schema.name}",
                description,
                f"Parameters (JSON Schema): {parameters}",
            ]
        )
    return "\n".join(blocks)


def build_system_prompt(tools: list[ToolSchema], ctx: PromptContext) -> str:
    """Assemble the complete system prompt for one session.

    Sections, in order: identity, environment, project instructions, tool
    catalog, operating rules.  The function never reads the filesystem itself;
    instruction files must already be collected into ctx.instructions.
    """
    started = datetime.fromtimestamp(ctx.started_at / 1000, tz=UTC)
    lines = [
        "You are DeepPyro, a coding agent running in a terminal.",
        (
            "You help with software engineering tasks in the user's workspace: inspect code, edit files, "
            "run commands, search for information, and verify your changes."
        ),
        "",
        "## Environment",
        "",
        f"- Working directory: {ctx.cwd}",
        f"- Platform: {ctx.platform}",
        f"- Model: {ctx.model or 'unknown'}",
        f"- Session: {ctx.session_id}",
        f"- Started (UTC): {started.isoformat(timespec='seconds').replace('+00:00', 'Z')}",
        f"- Permission preset: {ctx.permission_preset or 'unknown'}",
        f"- Approval policy: {ctx.approval_policy or 'unknown'}",
    ]
    env_lines = _environment_lines(ctx)
    if env_lines:
        lines.extend(["- Environment:", *[f"  {line}" for line in env_lines]])

    lines.extend(["", "## Project instructions", ""])
    if ctx.instructions:
        lines.append(
            "The following instructions were loaded from AGENTS.md files. Treat them as binding "
            "project guidance for every action in this workspace."
        )
        for index, block in enumerate(ctx.instructions, start=1):
            lines.extend(["", f"### Instruction block {index}", "", block])
    else:
        lines.append("No AGENTS.md instruction files were found for this workspace.")

    lines.extend(["", _tool_section(tools)])
    lines.extend(
        [
            "",
            "## Operating rules",
            "",
            "- Inspect before you change: read the relevant files and search for usages before editing.",
            "- Prefer targeted edits over whole-file rewrites; preserve unrelated code and formatting.",
            "- Use the shell tool for commands; prefer non-interactive invocations and capture exit codes.",
            "- Verify work: run the focused tests or checks that cover what you changed.",
            "- Never fabricate file contents, command output, or test results.",
            "- Follow the project instructions above when they are more specific than these rules.",
            "- Ask the user only when you are blocked by missing information or a decision that is theirs.",
            "- Keep replies concise and reference files by their path.",
            "- When a tool fails, read the error and adapt; do not repeat the identical failing call.",
            "",
            "If the user asks a question that needs no tools, answer directly without calling tools.",
        ]
    )
    return "\n".join(lines)
