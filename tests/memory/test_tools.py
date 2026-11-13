"""Tests for the agent-facing mnemon_runtime_memory tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.memory.service import MemoryService
from deeppyro.tools.builtin.memory import (
    ACTIONS,
    DESCRIPTION,
    TOOL_NAME,
    MnemonRuntimeMemoryTool,
    memory_tools,
)
from tests.memory.helpers import FakeClock, make_store


def _context(service: MemoryService | None) -> ToolContext:
    services: dict[str, Any] = {} if service is None else {"memory": service}
    return ToolContext(
        cwd=Path("."),
        session_id="session-test",
        turn=0,
        step=0,
        call_id="call-test",
        cancel=asyncio.Event(),
        log=lambda event, payload=None: None,
        services=services,
    )


def _service(tmp_path: Path) -> MemoryService:
    return MemoryService(store=make_store(tmp_path, clock=FakeClock()))


async def _call(
    tool: MnemonRuntimeMemoryTool, service: MemoryService | None, args: dict[str, Any]
) -> ToolResult:
    return await tool.run(args, _context(service))


def test_tool_metadata_explains_safety_rules() -> None:
    tool = MnemonRuntimeMemoryTool()

    assert tool.name == TOOL_NAME == "mnemon_runtime_memory"
    assert tool.parallel_safe is False
    assert tool.parameters["required"] == ["action", "target"]
    properties = tool.parameters["properties"]
    assert properties["action"]["enum"] == list(ACTIONS)
    assert properties["target"]["enum"] == ["user", "memory"]
    assert properties["importance"]["enum"] == ["critical", "normal", "low"]
    assert DESCRIPTION in tool.description
    assert "durable facts" in tool.description
    assert "Do not save questions" in tool.description
    assert "not instructions" in tool.description


async def test_add_returns_compact_json_receipt(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)

    result = await _call(tool, service, {"action": "add", "target": "user", "content": "likes tea"})

    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert "\n" not in result.text
    assert '": "' not in result.text
    payload = json.loads(result.text)
    assert payload["action"] == "add"
    assert payload["target"] == "user"
    assert payload["revision"] == 1
    assert payload["entry"]["content"] == "likes tea"
    assert result.meta == {"action": "add", "target": "user", "revision": 1}
    assert service.snapshot("user").text == "likes tea"
    assert (service.store.root / "USER.md").read_text(encoding="utf-8") == "likes tea"


async def test_add_keeps_unicode_unencoded(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)

    result = await _call(tool, service, {"action": "add", "target": "user", "content": "喜欢清淡的茶"})

    assert "喜欢清淡的茶" in result.text
    assert json.loads(result.text)["entry"]["content"] == "喜欢清淡的茶"


async def test_add_forwards_importance(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)

    await _call(
        tool,
        service,
        {"action": "add", "target": "memory", "content": "always run ruff", "importance": "critical"},
    )

    entry = service.entries("memory")[0]
    assert entry.importance == "critical"


async def test_replace_matches_by_old_text(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)
    added = await _call(tool, service, {"action": "add", "target": "user", "content": "likes coffee"})
    entry_id = json.loads(added.text)["id"]

    result = await _call(
        tool,
        service,
        {"action": "replace", "target": "user", "old_text": "coffee", "content": "prefers tea"},
    )

    payload = json.loads(result.text)
    assert payload["action"] == "replace"
    assert payload["id"] == entry_id
    assert payload["revision"] == 2
    assert service.snapshot("user").text == "prefers tea"


async def test_remove_matches_by_old_text(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)
    await _call(tool, service, {"action": "add", "target": "user", "content": "likes tea"})
    await _call(tool, service, {"action": "add", "target": "user", "content": "runs tests"})

    result = await _call(tool, service, {"action": "remove", "target": "user", "old_text": "tea"})

    payload = json.loads(result.text)
    assert payload["action"] == "remove"
    assert payload["entry"]["content"] == "likes tea"
    assert service.snapshot("user").text == "runs tests"


async def test_revisions_increase_across_actions(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)
    added = await _call(tool, service, {"action": "add", "target": "user", "content": "one"})
    replaced = await _call(
        tool, service, {"action": "replace", "target": "user", "old_text": "one", "content": "two"}
    )
    removed = await _call(tool, service, {"action": "remove", "target": "user", "old_text": "two"})

    assert [json.loads(item.text)["revision"] for item in (added, replaced, removed)] == [1, 2, 3]


@pytest.mark.parametrize("action", ["replace", "remove"])
async def test_missing_old_text_is_rejected(tmp_path: Path, action: str) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)

    with pytest.raises(ToolError, match="old_text is required"):
        await _call(tool, service, {"action": action, "target": "user", "content": "x"})


async def test_missing_content_is_rejected(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)

    with pytest.raises(ToolError, match="content is required"):
        await _call(tool, service, {"action": "add", "target": "user"})
    with pytest.raises(ToolError, match="content is required"):
        await _call(tool, service, {"action": "replace", "target": "user", "old_text": "x"})


async def test_unknown_action_target_and_importance_are_rejected(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)

    with pytest.raises(ToolError, match="action must be one of: add, replace, remove"):
        await _call(tool, service, {"action": "list", "target": "user"})
    with pytest.raises(ToolError, match="target must be one of: user, memory"):
        await _call(tool, service, {"action": "add", "target": "other", "content": "x"})
    with pytest.raises(ToolError, match="importance must be one of: critical, normal, low"):
        await _call(
            tool,
            service,
            {"action": "add", "target": "user", "content": "x", "importance": "urgent"},
        )


async def test_missing_action_is_rejected(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)

    with pytest.raises(ToolError, match="action is required"):
        await _call(tool, service, {"target": "user", "content": "x"})


async def test_ambiguous_old_text_is_a_tool_error(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)
    await _call(tool, service, {"action": "add", "target": "user", "content": "likes coffee"})
    await _call(tool, service, {"action": "add", "target": "user", "content": "drinks coffee daily"})

    with pytest.raises(ToolError, match="2 memory entries"):
        await _call(tool, service, {"action": "remove", "target": "user", "old_text": "coffee"})


async def test_unknown_old_text_is_a_tool_error(tmp_path: Path) -> None:
    tool = MnemonRuntimeMemoryTool()
    service = _service(tmp_path)

    with pytest.raises(ToolError, match="no memory entry"):
        await _call(tool, service, {"action": "remove", "target": "user", "old_text": "nothing"})


async def test_missing_service_is_a_tool_error() -> None:
    tool = MnemonRuntimeMemoryTool()

    with pytest.raises(ToolError, match="memory service is not available in this session"):
        await _call(tool, None, {"action": "add", "target": "user", "content": "x"})


def test_memory_tools_returns_one_tool() -> None:
    tools = memory_tools()

    assert [tool.name for tool in tools] == [TOOL_NAME]
    assert all(isinstance(tool, MnemonRuntimeMemoryTool) for tool in tools)
