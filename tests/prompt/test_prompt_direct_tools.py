"""Tests for the PTC presentation hook in the core tools prompt section."""

from __future__ import annotations

from pathlib import Path

from deeppyro.core.protocols import ToolSchema
from deeppyro.prompt import PromptContext, build_system_prompt
from deeppyro.prompt.prompt import tools_section


def make_context(tmp_path: Path, **overrides: object) -> PromptContext:
    defaults: dict[str, object] = {
        "cwd": tmp_path,
        "platform": "win32",
        "session_id": "session-ptc",
        "started_at": 1_700_000_000_000,
        "permission_preset": "workspace-write",
        "approval_policy": "ask",
        "model": "test-model",
    }
    defaults.update(overrides)
    return PromptContext(**defaults)  # type: ignore[arg-type]


def make_tools() -> list[ToolSchema]:
    return [
        ToolSchema(name="run_code", description="Run a program."),
        ToolSchema(name="read", description="Read a file."),
        ToolSchema(name="write", description="Write a file."),
    ]


def test_tools_section_lists_every_tool_without_direct_filter(tmp_path: Path) -> None:
    section = tools_section(make_tools(), make_context(tmp_path))
    assert "### run_code" in section
    assert "### read" in section
    assert "### write" in section


def test_tools_section_lists_only_directly_callable_tools(tmp_path: Path) -> None:
    section = tools_section(make_tools(), make_context(tmp_path, direct_tool_names=("run_code",)))
    assert "### run_code" in section
    assert "### read" not in section
    assert "### write" not in section


def test_tools_section_keeps_registration_order_of_surviving_tools(tmp_path: Path) -> None:
    section = tools_section(make_tools(), make_context(tmp_path, direct_tool_names=("write", "run_code")))
    assert section.index("### run_code") < section.index("### write")


def test_build_system_prompt_filters_core_tools_in_ptc_mode(tmp_path: Path) -> None:
    prompt = build_system_prompt(make_tools(), make_context(tmp_path, direct_tool_names=("run_code",)))
    assert "### run_code" in prompt
    assert "### read" not in prompt
