"""The model-facing skill tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.skills.discovery import SkillRoot
from deeppyro.skills.registry import SkillRegistry
from deeppyro.skills.types import CUSTOM_RANK, SOURCE_CUSTOM
from deeppyro.tools.builtin.skill import SkillTool, skill_tools

from .helpers import write_skill


def _tool(tmp_path: Path) -> tuple[SkillTool, Path]:
    skills = tmp_path / "skills"
    write_skill(skills, "demo", description="Demo skill", body="Demo body.")
    write_skill(skills, "hidden", description="Hidden", extra="disable-model-invocation: true\n")
    root = SkillRoot(rank=CUSTOM_RANK, source=SOURCE_CUSTOM, path=skills.resolve())
    return SkillTool(SkillRegistry(roots=[root])), tmp_path


def _ctx(cwd: Path) -> Any:
    return SimpleNamespace(cwd=cwd)


async def test_skill_tool_returns_rendered_instructions(tmp_path: Path) -> None:
    tool, cwd = _tool(tmp_path)
    result = await tool.run({"name": "demo"}, _ctx(cwd))
    assert result.text.startswith('<skill_content name="demo">')
    assert "<skill_instructions>\nDemo body.\n</skill_instructions>" in result.text
    assert result.meta is not None
    assert result.meta["name"] == "demo"
    assert result.meta["source"] == SOURCE_CUSTOM


async def test_skill_tool_rejects_unknown_name(tmp_path: Path) -> None:
    tool, cwd = _tool(tmp_path)
    with pytest.raises(ToolError, match="unknown or no longer available"):
        await tool.run({"name": "missing"}, _ctx(cwd))


async def test_skill_tool_rejects_invalid_name(tmp_path: Path) -> None:
    tool, cwd = _tool(tmp_path)
    with pytest.raises(ToolError, match="invalid skill name"):
        await tool.run({"name": "Bad Name"}, _ctx(cwd))


async def test_skill_tool_rejects_non_model_invocable_skill(tmp_path: Path) -> None:
    tool, cwd = _tool(tmp_path)
    with pytest.raises(ToolError, match="not available for model invocation"):
        await tool.run({"name": "hidden"}, _ctx(cwd))


async def test_skill_tool_validates_arguments(tmp_path: Path) -> None:
    tool, cwd = _tool(tmp_path)
    with pytest.raises(ToolError, match="name is required"):
        await tool.run({}, _ctx(cwd))
    with pytest.raises(ToolError, match="name must be a string"):
        await tool.run({"name": 3}, _ctx(cwd))


async def test_skill_tool_accepts_whitespace_padded_name(tmp_path: Path) -> None:
    tool, cwd = _tool(tmp_path)
    result = await tool.run({"name": " demo "}, _ctx(cwd))
    assert result.meta is not None and result.meta["name"] == "demo"


def test_skill_tools_metadata(tmp_path: Path) -> None:
    tool, _ = _tool(tmp_path)
    assert [item.name for item in skill_tools(tool._service)] == ["skill"]
    assert tool.parameters["required"] == ["name"]
    assert tool.parameters["properties"]["name"]["type"] == "string"
    assert tool.parallel_safe is True
