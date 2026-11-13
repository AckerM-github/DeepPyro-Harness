"""Tests for the agent-facing team tools."""

from __future__ import annotations

import json
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.team import MemberInfo, TaskInfo
from deeppyro.tools.builtin.team import (
    TeamCancelTool,
    TeamMembersTool,
    TeamSendTool,
    TeamSpawnTool,
    TeamTaskCreateTool,
    TeamTaskGetTool,
    TeamTaskListTool,
    TeamTaskUpdateTool,
    TeamWaitTool,
    team_tools,
)
from tests.tools.helpers import make_context

MEMBER = MemberInfo(
    id="member-1",
    name="scout",
    description="researcher",
    provider="local",
    model="m",
    preset="default",
    phase="completed",
    session_id="session-child",
    last_reply="found it",
)
TASK = TaskInfo(
    id="task-1",
    revision=2,
    subject="Write docs",
    description="document the team",
    status="in_progress",
    blocked_by=(),
    write_scopes=("docs/spec/team.md",),
    owner="lead",
)


class FakeTeamService:
    """Team service double that records calls and returns canned snapshots."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.spawn_error: Exception | None = None
        self.send_error: Exception | None = None
        self.member = MEMBER
        self.task = TASK
        self.task_known = True

    def spawn(self, name: str, description: str, prompt: str, **options: Any) -> MemberInfo:
        self.calls.append(
            (
                "spawn",
                {
                    "name": name,
                    "description": description,
                    "prompt": prompt,
                    **options,
                },
            )
        )
        if self.spawn_error is not None:
            raise self.spawn_error
        return self.member

    def send(self, member_id: str, message: str) -> str:
        self.calls.append(("send", {"member_id": member_id, "message": message}))
        if self.send_error is not None:
            raise self.send_error
        return "started"

    async def wait(self, member_id: str, timeout_s: float | None = None) -> MemberInfo:
        self.calls.append(("wait", {"member_id": member_id, "timeout_s": timeout_s}))
        return self.member

    async def cancel(self, member_id: str) -> bool:
        self.calls.append(("cancel", {"member_id": member_id}))
        return True

    def members(self) -> list[MemberInfo]:
        self.calls.append(("members", {}))
        return [self.member]

    def tasks(self) -> list[TaskInfo]:
        self.calls.append(("tasks", {}))
        return [self.task]

    def get_task(self, task_id: str) -> TaskInfo | None:
        self.calls.append(("get_task", {"task_id": task_id}))
        return self.task if self.task_known else None

    def create_task(self, subject: str, description: str, **options: Any) -> TaskInfo:
        self.calls.append(("create_task", {"subject": subject, "description": description, **options}))
        return self.task

    def update_task(self, task_id: str, action: str, **options: Any) -> TaskInfo:
        self.calls.append(("update_task", {"task_id": task_id, "action": action, **options}))
        return self.task


def team_context(service: FakeTeamService, tmp_path: Any) -> Any:
    return make_context(tmp_path, services={"team": service})


async def test_missing_team_service_raises_tool_error(tmp_path) -> None:
    ctx = make_context(tmp_path)
    with pytest.raises(ToolError, match="team service is not available"):
        await TeamSpawnTool().run({"name": "a", "description": "b", "prompt": "c"}, ctx)


async def test_team_spawn_tool_passes_arguments(tmp_path) -> None:
    service = FakeTeamService()
    result = await TeamSpawnTool().run(
        {
            "name": "scout",
            "description": "researcher",
            "prompt": "find it",
            "provider": "remote",
            "model": "other",
            "preset": "worker",
        },
        team_context(service, tmp_path),
    )
    assert result.is_error is False
    payload = json.loads(result.text)
    assert payload["member"]["name"] == "scout"
    assert service.calls == [
        (
            "spawn",
            {
                "name": "scout",
                "description": "researcher",
                "prompt": "find it",
                "provider": "remote",
                "model": "other",
                "preset": "worker",
            },
        )
    ]


async def test_team_spawn_tool_reports_service_errors(tmp_path) -> None:
    service = FakeTeamService()
    service.spawn_error = RuntimeError("team is full")
    result = await TeamSpawnTool().run(
        {"name": "scout", "description": "d", "prompt": "p"},
        team_context(service, tmp_path),
    )
    assert result.is_error is True
    assert "team is full" in result.text


async def test_team_spawn_tool_requires_arguments(tmp_path) -> None:
    service = FakeTeamService()
    ctx = team_context(service, tmp_path)
    with pytest.raises(ToolError, match="name must be a non-empty string"):
        await TeamSpawnTool().run({"description": "d", "prompt": "p"}, ctx)
    with pytest.raises(ToolError, match="prompt must be a non-empty string"):
        await TeamSpawnTool().run({"name": "n", "description": "d"}, ctx)


async def test_team_send_and_cancel_tools(tmp_path) -> None:
    service = FakeTeamService()
    ctx = team_context(service, tmp_path)
    sent = await TeamSendTool().run({"member_id": "member-1", "message": "go"}, ctx)
    assert sent.is_error is False
    assert json.loads(sent.text) == {"member_id": "member-1", "status": "started"}
    cancelled = await TeamCancelTool().run({"member_id": "member-1"}, ctx)
    assert json.loads(cancelled.text) == {"member_id": "member-1", "cancelled": True}
    assert [name for name, _ in service.calls] == ["send", "cancel"]


async def test_team_send_tool_reports_errors(tmp_path) -> None:
    service = FakeTeamService()
    service.send_error = RuntimeError("member is cancelled")
    result = await TeamSendTool().run(
        {"member_id": "member-1", "message": "go"},
        team_context(service, tmp_path),
    )
    assert result.is_error is True
    assert "member is cancelled" in result.text


async def test_team_wait_tool_converts_timeout_and_returns_member(tmp_path) -> None:
    service = FakeTeamService()
    ctx = team_context(service, tmp_path)
    result = await TeamWaitTool().run({"member_id": "member-1", "timeoutMs": 1500}, ctx)
    assert result.is_error is False
    assert json.loads(result.text)["member"]["phase"] == "completed"
    assert service.calls[-1] == (
        "wait",
        {"member_id": "member-1", "timeout_s": 1.5},
    )
    await TeamWaitTool().run({"member_id": "member-1"}, ctx)
    assert service.calls[-1] == ("wait", {"member_id": "member-1", "timeout_s": None})
    invalid = await TeamWaitTool().run({"member_id": "member-1", "timeoutMs": "soon"}, ctx)
    assert invalid.is_error is True


async def test_team_members_tool_lists_members(tmp_path) -> None:
    service = FakeTeamService()
    result = await TeamMembersTool().run({}, team_context(service, tmp_path))
    payload = json.loads(result.text)
    assert [item["name"] for item in payload["members"]] == ["scout"]


async def test_team_task_create_tool_passes_board_fields(tmp_path) -> None:
    service = FakeTeamService()
    result = await TeamTaskCreateTool().run(
        {
            "subject": "Write docs",
            "description": "document the team",
            "blockedBy": ["task-1"],
            "writeScopes": ["docs/spec/team.md"],
        },
        team_context(service, tmp_path),
    )
    assert result.is_error is False
    assert json.loads(result.text)["task"]["id"] == "task-1"
    assert service.calls[-1][0] == "create_task"
    assert service.calls[-1][1]["blocked_by"] == ["task-1"]
    assert service.calls[-1][1]["write_scopes"] == ["docs/spec/team.md"]


async def test_team_task_list_tool_filters_by_status(tmp_path) -> None:
    service = FakeTeamService()
    ctx = team_context(service, tmp_path)
    all_result = await TeamTaskListTool().run({}, ctx)
    assert len(json.loads(all_result.text)["tasks"]) == 1
    missing = await TeamTaskListTool().run({"status": "completed"}, ctx)
    assert json.loads(missing.text)["tasks"] == []


async def test_team_task_get_tool_handles_unknown_task(tmp_path) -> None:
    service = FakeTeamService()
    ctx = team_context(service, tmp_path)
    found = await TeamTaskGetTool().run({"task_id": "task-1"}, ctx)
    assert json.loads(found.text)["task"]["id"] == "task-1"
    service.task_known = False
    missing = await TeamTaskGetTool().run({"task_id": "task-1"}, ctx)
    assert missing.is_error is True
    assert "unknown team task" in missing.text


async def test_team_task_update_tool_passes_action_and_fields(tmp_path) -> None:
    service = FakeTeamService()
    result = await TeamTaskUpdateTool().run(
        {
            "task_id": "task-1",
            "action": "edit",
            "subject": "New subject",
            "blockedBy": [],
            "writeScopes": ["src/deeppyro/team"],
        },
        team_context(service, tmp_path),
    )
    assert result.is_error is False
    call = service.calls[-1]
    assert call[0] == "update_task"
    assert call[1]["task_id"] == "task-1"
    assert call[1]["action"] == "edit"
    assert call[1]["subject"] == "New subject"
    assert call[1]["blocked_by"] == []
    assert call[1]["write_scopes"] == ["src/deeppyro/team"]


async def test_team_tools_are_serial_and_named() -> None:
    expected = [
        "team_spawn",
        "team_send",
        "team_wait",
        "team_cancel",
        "team_members",
        "team_task_create",
        "team_task_list",
        "team_task_get",
        "team_task_update",
    ]
    tools = team_tools()
    assert [tool.name for tool in tools] == expected
    assert all(tool.parallel_safe is False for tool in tools)
