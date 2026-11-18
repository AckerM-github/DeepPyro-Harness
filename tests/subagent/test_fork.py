"""Tests for the background fork tool: immediate activation and error mapping."""

from __future__ import annotations

import json
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.subagent import SubagentActivation, SubagentError
from deeppyro.tools.builtin.subagent import SubagentForkTool
from tests.tools.helpers import make_context


class FakeForkService:
    """Service double that records fork starts and returns a canned activation."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.activation = SubagentActivation(
            child_id="subagent-abc123",
            session_id="subagent-abc123",
            label="scout",
            provider="local",
            model="m",
            mode="fork",
            created_at=7,
        )
        self.error = error

    async def start(self, prompt: str, **options: Any) -> SubagentActivation:
        self.calls.append((prompt, options))
        if self.error is not None:
            raise self.error
        return self.activation


def test_fork_tool_definition() -> None:
    tool = SubagentForkTool()

    assert tool.name == "subagent_fork"
    assert tool.parallel_safe is False
    assert "background" in tool.description
    assert tool.parameters["required"] == ["prompt", "description"]
    assert set(tool.parameters["properties"]) == {"prompt", "description", "name"}
    assert tool.parameters["additionalProperties"] is False


async def test_fork_tool_requires_the_service(tmp_path) -> None:
    tool = SubagentForkTool()
    ctx = make_context(tmp_path)

    with pytest.raises(ToolError, match="subagent service is not available"):
        await tool.run({"prompt": "p", "description": "d"}, ctx)


async def test_fork_tool_returns_the_activation_immediately(tmp_path) -> None:
    service = FakeForkService()
    tool = SubagentForkTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    result = await tool.run({"prompt": "continue", "description": "d", "name": "scout"}, ctx)

    assert result.is_error is False
    payload = json.loads(result.text)
    assert payload == {"kind": "activation", "subagentId": "subagent-abc123"}
    assert result.meta == {
        "childSessionId": "subagent-abc123",
        "label": "scout",
        "mode": "fork",
    }
    assert service.calls == [
        ("continue", {"description": "d", "name": "scout", "mode": "fork"}),
    ]


async def test_fork_tool_reports_start_failures(tmp_path) -> None:
    service = FakeForkService(error=SubagentError("boom"))
    tool = SubagentForkTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    result = await tool.run({"prompt": "p", "description": "d"}, ctx)

    assert result.is_error is True
    assert result.text == "subagent fork failed: boom"


async def test_fork_tool_rejects_blank_prompt(tmp_path) -> None:
    service = FakeForkService()
    tool = SubagentForkTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    with pytest.raises(ToolError, match="prompt must be a non-empty string"):
        await tool.run({"prompt": "", "description": "d"}, ctx)
