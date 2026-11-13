"""Tests for the agent-facing schedule tool and its prompt section."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext
from deeppyro.schedule import ScheduleManager, ScheduleStore
from deeppyro.tools.builtin.schedule import (
    ACTIONS,
    MANAGEMENT_AUTHORIZATION,
    SCHEDULE_PROMPT_ORDER,
    SCHEDULE_PROMPT_SECTION,
    TARGET_CHOICES,
    ScheduleTool,
    authorization_section,
    schedule_tools,
)

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
RRULE = """DTSTART:20261114T020100Z
RRULE:FREQ=DAILY;COUNT=2"""


class FakeClock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class FakeHost:
    def __init__(self) -> None:
        self.depths: dict[str, int] = {}

    async def session_exists(self, session_id: str) -> bool:
        return True

    async def delegation_depth(self, session_id: str) -> int:
        return self.depths.get(session_id, 0)

    async def deliver(self, task: object, message: str) -> None:
        return None


def _context(
    manager: ScheduleManager | None,
    *,
    session_id: str = "session-1",
    services: dict[str, object] | None = None,
) -> ToolContext:
    if services is None:
        services = {"schedule": manager} if manager is not None else {}
    return ToolContext(
        cwd=Path("."),
        session_id=session_id,
        turn=1,
        step=1,
        call_id="call-1",
        cancel=asyncio.Event(),
        log=lambda _message, _data=None: None,
        services=services,
    )


def _manager(
    tmp_path: Path,
    *,
    host: FakeHost | None = None,
    clock: FakeClock | None = None,
) -> ScheduleManager:
    return ScheduleManager(
        ScheduleStore(tmp_path / "schedules.json"),
        host=host if host is not None else FakeHost(),
        now_fn=clock if clock is not None else FakeClock(),
    )


async def _run(tool: ScheduleTool, args: dict[str, object], ctx: ToolContext) -> dict[str, object]:
    result = await tool.run(args, ctx)
    assert result.is_error is False
    return json.loads(result.text)


async def test_tool_metadata_and_registry() -> None:
    tool = ScheduleTool()
    assert tool.name == "schedule"
    assert tool.parallel_safe is False
    assert tool.parameters["required"] == ["action"]
    assert tool.parameters["properties"]["action"]["enum"] == list(ACTIONS)
    assert tool.parameters["properties"]["target"]["enum"] == list(TARGET_CHOICES)
    assert tool.parameters["additionalProperties"] is False
    tools = schedule_tools()
    assert len(tools) == 1
    assert tools[0].name == "schedule"
    assert SCHEDULE_PROMPT_SECTION == "tool:schedule"
    assert SCHEDULE_PROMPT_ORDER == 45


async def test_create_returns_task_json(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    tool = ScheduleTool()
    result = await tool.run(
        {"action": "create", "title": "standup", "prompt": "say hi", "rrule": RRULE},
        _context(manager),
    )
    assert result.is_error is False
    task = json.loads(result.text)
    assert task["title"] == "standup"
    assert task["kind"] == "rrule"
    assert task["status"] == "active"
    assert task["scheduledAt"] == "2026-11-14T02:01:00.000Z"
    assert task["timing"]["rrule"] == RRULE
    assert task["target"]["kind"] == "session"
    assert task["target"]["session_id"] == "session-1"
    assert result.meta == {"scheduleId": task["id"], "status": "active"}
    assert (await manager.view(str(task["id"]))) is not None


async def test_create_target_choices(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    tool = ScheduleTool()
    task = await _run(
        tool,
        {
            "action": "create",
            "title": "t",
            "prompt": "p",
            "rrule": RRULE,
            "target": "new_once",
        },
        _context(manager),
    )
    assert task["target"]["kind"] == "new_once"
    task = await _run(
        tool,
        {"action": "create", "title": "t", "prompt": "p", "rrule": RRULE, "target": "new"},
        _context(manager),
    )
    assert task["target"]["kind"] == "new"


async def test_create_validates_arguments(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    tool = ScheduleTool()
    ctx = _context(manager)
    with pytest.raises(ToolError, match="action is required"):
        await tool.run({}, ctx)
    with pytest.raises(ToolError, match="action must be one of"):
        await tool.run({"action": "bogus"}, ctx)
    with pytest.raises(ToolError, match="title is required"):
        await tool.run({"action": "create"}, ctx)
    with pytest.raises(ToolError, match="prompt is required"):
        await tool.run({"action": "create", "title": "t"}, ctx)
    with pytest.raises(ToolError, match="rrule is required"):
        await tool.run({"action": "create", "title": "t", "prompt": "p"}, ctx)
    with pytest.raises(ToolError, match="invalid_rule:"):
        await tool.run(
            {"action": "create", "title": "t", "prompt": "p", "rrule": "FREQ=DAILY"},
            ctx,
        )
    with pytest.raises(ToolError, match="invalid_selector: unknown schedule target"):
        await tool.run(
            {"action": "create", "title": "t", "prompt": "p", "rrule": RRULE, "target": "bogus"},
            ctx,
        )


async def test_list_returns_pages_and_filters(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    tool = ScheduleTool()
    ctx = _context(manager)
    for title in ("a", "b", "c"):
        await tool.run(
            {"action": "create", "title": title, "prompt": "p", "rrule": RRULE},
            ctx,
        )
    page = await _run(tool, {"action": "list"}, ctx)
    assert page["total"] == 3
    assert page["limit"] == 100
    assert len(page["tasks"]) == 3

    page = await _run(tool, {"action": "list", "limit": 1, "offset": 1}, ctx)
    assert page["total"] == 3
    assert page["limit"] == 1
    assert page["offset"] == 1
    assert len(page["tasks"]) == 1

    await manager.update(str(page["tasks"][0]["id"]), {"active": False})
    active = await _run(tool, {"action": "list", "active": True}, ctx)
    assert active["total"] == 2
    paused = await _run(tool, {"action": "list", "active": False}, ctx)
    assert paused["total"] == 1

    with pytest.raises(ToolError, match="greater than or equal to 1"):
        await tool.run({"action": "list", "limit": 0}, ctx)
    with pytest.raises(ToolError, match="less than or equal to 500"):
        await tool.run({"action": "list", "limit": 501}, ctx)
    with pytest.raises(ToolError, match="limit must be an integer"):
        await tool.run({"action": "list", "limit": "ten"}, ctx)


async def test_update_edits_pauses_and_checks_cas(tmp_path: Path) -> None:
    class AdvancingClock(FakeClock):
        def advance(self, seconds: float) -> None:
            from datetime import timedelta

            self.moment = self.moment + timedelta(seconds=seconds)

    clock = AdvancingClock()
    manager = _manager(tmp_path, clock=clock)
    tool = ScheduleTool()
    ctx = _context(manager)
    task = await _run(tool, {"action": "create", "title": "old", "prompt": "p", "rrule": RRULE}, ctx)
    clock.advance(5)
    updated = await _run(
        tool,
        {"action": "update", "id": task["id"], "title": "new", "active": False},
        ctx,
    )
    assert updated["title"] == "new"
    assert updated["status"] == "paused"
    assert updated["updatedAt"] != task["updatedAt"]

    clock.advance(5)
    changed = await _run(
        tool,
        {"action": "update", "id": task["id"], "active": True, "expectedUpdatedAt": updated["updatedAt"]},
        ctx,
    )
    assert changed["status"] == "active"

    with pytest.raises(ToolError, match="schedule_conflict:"):
        await tool.run(
            {
                "action": "update",
                "id": task["id"],
                "title": "stale",
                "expectedUpdatedAt": task["updatedAt"],
            },
            ctx,
        )
    with pytest.raises(ToolError, match="update requires at least one"):
        await tool.run({"action": "update", "id": task["id"]}, ctx)
    with pytest.raises(ToolError, match="id is required"):
        await tool.run({"action": "update", "title": "x"}, ctx)
    with pytest.raises(ToolError, match="unknown_task:"):
        await tool.run({"action": "update", "id": "schedule-missing", "title": "x"}, ctx)


async def test_delete_removes_task(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    tool = ScheduleTool()
    ctx = _context(manager)
    task = await _run(tool, {"action": "create", "title": "t", "prompt": "p", "rrule": RRULE}, ctx)
    result = await tool.run({"action": "delete", "id": task["id"]}, ctx)
    assert json.loads(result.text) == {"deleted": task["id"]}
    assert await manager.list() == {"tasks": [], "total": 0, "limit": 100, "offset": 0}
    with pytest.raises(ToolError, match="unknown_task:"):
        await tool.run({"action": "delete", "id": task["id"]}, ctx)
    with pytest.raises(ToolError, match="id is required"):
        await tool.run({"action": "delete"}, ctx)


async def test_child_sessions_are_rejected(tmp_path: Path) -> None:
    host = FakeHost()
    host.depths["child-1"] = 1
    manager = _manager(tmp_path, host=host)
    tool = ScheduleTool()
    with pytest.raises(ToolError, match="subagent_session"):
        await tool.run({"action": "list"}, _context(manager, session_id="child-1"))
    # A broken host probe must not reject the call (depth falls back to 0).
    child = _context(manager, session_id="child-2")
    assert (await _run(tool, {"action": "list"}, child))["total"] == 0


async def test_missing_service_raises_tool_error() -> None:
    tool = ScheduleTool()
    with pytest.raises(ToolError, match="schedule service is not available"):
        await tool.run({"action": "list"}, _context(None))
    with pytest.raises(ToolError, match="schedule service is not available"):
        await tool.run({"action": "list"}, _context(None, services={"other": object()}))


def test_authorization_section_visibility() -> None:
    assert authorization_section() == MANAGEMENT_AUTHORIZATION
    visible = [SimpleNamespace(name="schedule")]
    assert authorization_section(visible) == MANAGEMENT_AUTHORIZATION
    other = [SimpleNamespace(name="shell"), SimpleNamespace(name="read")]
    assert authorization_section(other) == ""
    assert authorization_section([]) == MANAGEMENT_AUTHORIZATION
    assert authorization_section([SimpleNamespace(name="")]) == ""
