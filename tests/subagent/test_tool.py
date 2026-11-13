"""Tests for the agent-facing subagent tool."""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.subagent import SubagentError, SubagentResult
from deeppyro.tools.builtin.subagent import SubagentTool, subagent_tools
from tests.subagent.helpers import FakeAgents, make_subagent
from tests.tools.helpers import make_context


class FakeSubagentService:
    """Subagent service double that records run() calls and returns a canned result."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = SubagentResult(
            child_id="subagent-abc123",
            session_id="session-1",
            label="scout",
            provider="local",
            model="m",
            text="done",
        )
        self.error: Exception | None = None

    async def run(self, prompt: str, **options: Any) -> SubagentResult:
        self.calls.append((prompt, options))
        if self.error is not None:
            raise self.error
        return self.result


def test_tool_definition() -> None:
    tool = SubagentTool()

    assert tool.name == "subagent"
    assert tool.parallel_safe is False
    assert "child agent" in tool.description
    assert "human approval" in tool.description
    assert tool.parameters["required"] == ["prompt", "description"]
    assert set(tool.parameters["properties"]) == {
        "prompt",
        "description",
        "name",
        "provider",
        "model",
        "preset",
    }
    assert tool.parameters["additionalProperties"] is False


def test_tools_factory() -> None:
    tools = subagent_tools()

    assert [tool.name for tool in tools] == ["subagent"]
    assert all(tool.parallel_safe is False for tool in tools)


async def test_missing_service_raises(tmp_path) -> None:
    tool = SubagentTool()
    ctx = make_context(tmp_path)

    with pytest.raises(ToolError, match="subagent service is not available in this session"):
        await tool.run({"prompt": "p", "description": "d"}, ctx)


async def test_required_arguments(tmp_path) -> None:
    service = FakeSubagentService()
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    with pytest.raises(ToolError, match="prompt must be a non-empty string"):
        await tool.run({}, ctx)
    with pytest.raises(ToolError, match="description must be a non-empty string"):
        await tool.run({"prompt": "p"}, ctx)
    assert service.calls == []


async def test_success_text_and_meta(tmp_path) -> None:
    service = FakeSubagentService()
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    result = await tool.run({"prompt": "  find it  ", "description": "  research  "}, ctx)

    assert result.is_error is False
    assert result.text == "subagent scout (session-1) completed:\ndone"
    assert result.meta == {
        "childSessionId": "session-1",
        "label": "scout",
        "provider": "local",
        "model": "m",
    }
    assert service.calls == [
        (
            "find it",
            {
                "description": "research",
                "name": None,
                "provider": None,
                "model": None,
                "preset": None,
            },
        )
    ]


async def test_optional_arguments_are_forwarded(tmp_path) -> None:
    service = FakeSubagentService()
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    await tool.run(
        {
            "prompt": "p",
            "description": "d",
            "name": "scout",
            "provider": "other",
            "model": "child-model",
            "preset": "other-preset",
        },
        ctx,
    )

    assert service.calls == [
        (
            "p",
            {
                "description": "d",
                "name": "scout",
                "provider": "other",
                "model": "child-model",
                "preset": "other-preset",
            },
        )
    ]


async def test_blank_optional_arguments_become_none(tmp_path) -> None:
    service = FakeSubagentService()
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    await tool.run(
        {"prompt": "p", "description": "d", "name": "  ", "provider": "", "model": "", "preset": "  "},
        ctx,
    )

    options = service.calls[0][1]
    assert options["name"] is None
    assert options["provider"] is None
    assert options["model"] is None
    assert options["preset"] is None


async def test_error_result_is_returned(tmp_path) -> None:
    service = FakeSubagentService()
    service.result = SubagentResult(
        child_id="subagent-abc123",
        session_id="session-1",
        label="scout",
        provider="local",
        model="m",
        is_error=True,
        error="RuntimeError: boom",
    )
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    result = await tool.run({"prompt": "p", "description": "d"}, ctx)

    assert result.is_error is True
    assert result.text == "RuntimeError: boom"
    assert result.meta == {
        "childSessionId": "session-1",
        "label": "scout",
        "provider": "local",
        "model": "m",
    }


async def test_error_result_without_message_uses_fallback(tmp_path) -> None:
    service = FakeSubagentService()
    service.result = SubagentResult(
        child_id="subagent-abc123",
        session_id="session-1",
        label="scout",
        provider="local",
        model="m",
        is_error=True,
    )
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    result = await tool.run({"prompt": "p", "description": "d"}, ctx)

    assert result.is_error is True
    assert result.text == "subagent run failed"


async def test_exception_becomes_error_result(tmp_path) -> None:
    service = FakeSubagentService()
    service.error = SubagentError("no live slots")
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    result = await tool.run({"prompt": "p", "description": "d"}, ctx)

    assert result.is_error is True
    assert result.text == "subagent failed: no live slots"


async def test_blank_required_prompt_is_rejected(tmp_path) -> None:
    service = FakeSubagentService()
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": service})

    with pytest.raises(ToolError, match="prompt must be a non-empty string"):
        await tool.run({"prompt": 3, "description": "d"}, ctx)


async def test_real_service_end_to_end(tmp_path) -> None:
    env = make_subagent(tmp_path, agents=FakeAgents(reply="found it"))
    tool = SubagentTool()
    ctx = make_context(tmp_path, services={"subagent": env.service})

    result = await tool.run({"prompt": "find it", "description": "research", "name": "scout"}, ctx)

    assert result.is_error is False
    meta = result.meta
    assert meta is not None
    assert set(meta) == {"childSessionId", "label", "provider", "model"}
    assert meta["label"] == "scout"
    assert meta["provider"] == "local"
    assert meta["model"] == "parent-model"
    assert result.text == f"subagent scout ({meta['childSessionId']}) completed:\nfound it"
    child = env.store.open(meta["childSessionId"])
    assert child.header.parentSession == env.parent.session_id
    assert env.agents.agents[0].closed is True
