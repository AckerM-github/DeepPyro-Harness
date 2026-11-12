"""Tests for the system prompt builder and project instruction discovery."""

from __future__ import annotations

from pathlib import Path

from deeppyro.core.protocols import ToolSchema
from deeppyro.prompt import PromptContext, build_system_prompt, collect_instructions


def make_context(tmp_path: Path, **overrides: object) -> PromptContext:
    defaults: dict[str, object] = {
        "cwd": tmp_path,
        "platform": "win32",
        "session_id": "session-test",
        "started_at": 1_700_000_000_000,
        "permission_preset": "workspace-write",
        "approval_policy": "ask",
        "model": "test-model",
        "instructions": [],
        "env": {},
    }
    defaults.update(overrides)
    return PromptContext(**defaults)  # type: ignore[arg-type]


def test_build_system_prompt_mentions_environment_and_model(tmp_path: Path) -> None:
    prompt = build_system_prompt([], make_context(tmp_path))

    assert "DeepPyro" in prompt
    assert str(tmp_path) in prompt
    assert "win32" in prompt
    assert "test-model" in prompt
    assert "session-test" in prompt
    assert "workspace-write" in prompt
    assert "## Operating rules" in prompt
    assert "answer directly without calling tools" in prompt


def test_build_system_prompt_renders_tool_schemas(tmp_path: Path) -> None:
    schema = ToolSchema(
        name="read_file",
        description="Read a file from disk.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    prompt = build_system_prompt([schema], make_context(tmp_path))

    assert "### read_file" in prompt
    assert "Read a file from disk." in prompt
    assert '"path"' in prompt
    assert "Parameters (JSON Schema):" in prompt


def test_build_system_prompt_includes_instruction_blocks(tmp_path: Path) -> None:
    prompt = build_system_prompt(
        [],
        make_context(
            tmp_path,
            instructions=["Always run tests.", "Prefer small diffs."],
            env={"CI": "1", "HOME": "C:/Users/test"},
        ),
    )

    assert "### Instruction block 1" in prompt
    assert "Always run tests." in prompt
    assert "### Instruction block 2" in prompt
    assert "Prefer small diffs." in prompt
    assert "CI=1" in prompt


def test_build_system_prompt_is_deterministic(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    first = build_system_prompt([], ctx)
    second = build_system_prompt([], ctx)
    assert first == second


def test_collect_instructions_walks_up_root_first(tmp_path: Path) -> None:
    root = tmp_path
    child = root / "pkg" / "sub"
    child.mkdir(parents=True)
    (root / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (root / "pkg" / "AGENTS.md").write_text("pkg rules", encoding="utf-8")

    blocks = collect_instructions(child)

    assert len(blocks) == 2
    assert "root rules" in blocks[0]
    assert "pkg rules" in blocks[1]
    assert str(root / "AGENTS.md") in blocks[0]


def test_collect_instructions_skips_missing_and_empty(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("   \n", encoding="utf-8")
    assert collect_instructions(tmp_path) == []


def test_collect_instructions_truncates_long_files(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * 500, encoding="utf-8")
    blocks = collect_instructions(tmp_path, max_chars_per_file=100)
    assert len(blocks) == 1
    assert "[instruction file truncated]" in blocks[0]
    assert len(blocks[0]) < 500
