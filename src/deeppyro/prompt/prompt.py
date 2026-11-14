"""System prompt assembly.

The prompt is deliberately deterministic: everything it says comes from the
tool schemas and the PromptContext the caller passes in.  That keeps prompt
snapshots testable and makes the runtime environment (cwd, platform, model,
permissions) explicit instead of hidden in prose.

The prompt is assembled from ordered sections. The core sections live here and
are registered by the prompt-core plugin; other plugins can append their own
section through the prompt service without touching this module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from deeppyro.core.protocols import ToolSchema

INSTRUCTION_FILENAME = "AGENTS.md"
DEFAULT_MAX_INSTRUCTION_CHARS = 20_000
_MAX_ENV_ENTRIES = 50

SectionRenderer = Callable[[list[ToolSchema], "PromptContext"], str]


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
    #: When set, the core tools section only lists these directly callable
    #: tools; PTC mode passes ("run_code",) because every other tool is
    #: reached from inside the program.
    direct_tool_names: tuple[str, ...] | None = None


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


def environment_entries(ctx: PromptContext) -> list[str]:
    entries = sorted(ctx.env.items())[:_MAX_ENV_ENTRIES]
    return [f"- {key}={value}" for key, value in entries]


# -- core sections ---------------------------------------------------------


def identity_section(tools: list[ToolSchema], ctx: PromptContext) -> str:
    return (
        "You are DeepPyro, a coding agent running in a terminal.\n"
        "You help with software engineering tasks in the user's workspace: inspect code, edit files, "
        "run commands, search for information, and verify your changes."
    )


def environment_section(tools: list[ToolSchema], ctx: PromptContext) -> str:
    started = datetime.fromtimestamp(ctx.started_at / 1000, tz=UTC)
    lines = [
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
    env_lines = environment_entries(ctx)
    if env_lines:
        lines.extend(["- Environment:", *[f"  {line}" for line in env_lines]])
    return "\n".join(lines)


def instructions_section(tools: list[ToolSchema], ctx: PromptContext) -> str:
    lines = ["## Project instructions", ""]
    if ctx.instructions:
        lines.append(
            "The following instructions were loaded from AGENTS.md files. Treat them as binding "
            "project guidance for every action in this workspace."
        )
        for index, block in enumerate(ctx.instructions, start=1):
            lines.extend(["", f"### Instruction block {index}", "", block])
    else:
        lines.append("No AGENTS.md instruction files were found for this workspace.")
    return "\n".join(lines)


def tools_section(tools: list[ToolSchema], ctx: PromptContext) -> str:
    if ctx.direct_tool_names is not None:
        direct = set(ctx.direct_tool_names)
        tools = [schema for schema in tools if schema.name in direct]
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


def rules_section(tools: list[ToolSchema], ctx: PromptContext) -> str:
    lines = [
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
    return "\n".join(lines)


#: (name, order, renderer) of the built-in prompt sections.
CORE_PROMPT_SECTIONS: tuple[tuple[str, int, SectionRenderer], ...] = (
    ("identity", 10, identity_section),
    ("environment", 20, environment_section),
    ("instructions", 30, instructions_section),
    ("tools", 40, tools_section),
    ("rules", 50, rules_section),
)


def render_sections(
    tools: list[ToolSchema],
    ctx: PromptContext,
    sections: list[tuple[int, str, SectionRenderer]],
) -> str:
    """Render ordered sections and join them with one blank line."""
    parts: list[str] = []
    for _order, _name, renderer in sorted(sections, key=lambda item: (item[0], item[1])):
        text = renderer(tools, ctx)
        if text is None:
            continue
        text = str(text).strip("\n")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def build_system_prompt(tools: list[ToolSchema], ctx: PromptContext) -> str:
    """Assemble the complete system prompt from the core sections.

    Sections, in order: identity, environment, project instructions, tool
    catalog, operating rules.  The function never reads the filesystem itself;
    instruction files must already be collected into ctx.instructions.
    """
    return render_sections(
        tools,
        ctx,
        [(order, name, renderer) for name, order, renderer in CORE_PROMPT_SECTIONS],
    )


def core_sections() -> tuple[tuple[str, int, SectionRenderer], ...]:
    return CORE_PROMPT_SECTIONS


__all__ = [
    "CORE_PROMPT_SECTIONS",
    "DEFAULT_MAX_INSTRUCTION_CHARS",
    "INSTRUCTION_FILENAME",
    "PromptContext",
    "SectionRenderer",
    "build_system_prompt",
    "collect_instructions",
    "core_sections",
    "environment_entries",
    "environment_section",
    "identity_section",
    "instructions_section",
    "render_sections",
    "rules_section",
    "tools_section",
]
