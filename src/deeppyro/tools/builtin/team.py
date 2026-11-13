"""Agent-facing tools for the lead team service.

Each tool resolves the team service from ToolContext.services, so the runtime
decides which sessions expose team management.  Team members are real child
agents that execute their own turns; spawning one needs approval because it
starts model work in a separate session.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool, make_parameters


def _as_json(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _as_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_json(item) for item in value]
    return value


def _dump(value: Any) -> str:
    return json.dumps(_as_json(value), ensure_ascii=False, indent=2, default=str)


def _team(ctx: ToolContext) -> Any:
    service = (ctx.services or {}).get("team")
    if service is None:
        raise ToolError("team service is not available in this session")
    return service


def _required(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} must be a non-empty string")
    return value.strip()


def _optional(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    text = value.strip()
    return text or None


def _string_list(args: dict[str, Any], name: str) -> list[str] | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ToolError(f"{name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ToolError(f"{name} must be a list of non-empty strings")
        result.append(item.strip())
    return result


class TeamSpawnTool(BaseTool):
    """Spawn one child agent that works in its own session."""

    name = "team_spawn"
    description = (
        "Spawn a team member: a child agent with its own session, tools and task. "
        "The member starts working on prompt immediately and runs independently; use team_wait to "
        "collect its reply and team_send to give it more work. Up to 8 members can be live at once, "
        "and member names must be unique among live members. Spawning starts model work and "
        "therefore requires human approval."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "name": {"type": "string", "description": "unique member name"},
            "description": {"type": "string", "description": "short role description"},
            "prompt": {"type": "string", "description": "initial task for the member"},
            "provider": {
                "type": "string",
                "description": "optional llm provider name (defaults to the lead provider)",
            },
            "model": {
                "type": "string",
                "description": "optional model id (defaults to the lead model)",
            },
            "preset": {
                "type": "string",
                "description": "optional agent preset id (defaults to the lead preset)",
            },
        },
        required=["name", "description", "prompt"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        name = _required(args, "name")
        description = _required(args, "description")
        prompt = _required(args, "prompt")
        try:
            info = service.spawn(
                name,
                description,
                prompt,
                provider=_optional(args, "provider"),
                model=_optional(args, "model"),
                preset=_optional(args, "preset"),
            )
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"team_spawn failed: {exc}", is_error=True)
        return ToolResult(text=_dump({"member": info}))


class TeamSendTool(BaseTool):
    """Send a message to a member, starting a turn or queueing behind the current one."""

    name = "team_send"
    description = (
        "Send a message to a team member. An idle member starts a new turn and the call returns "
        "status=started; a busy member queues the message and returns status=queued. Use team_wait "
        "to collect the reply."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "member_id": {"type": "string", "description": "team member id"},
            "message": {"type": "string", "description": "message text"},
        },
        required=["member_id", "message"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        member_id = _required(args, "member_id")
        message = _required(args, "message")
        try:
            status = service.send(member_id, message)
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"team_send failed: {exc}", is_error=True)
        return ToolResult(text=_dump({"member_id": member_id, "status": status}))


class TeamWaitTool(BaseTool):
    """Wait for a member to become idle, completed, failed or cancelled."""

    name = "team_wait"
    description = (
        "Wait for a team member to settle (idle, completed, failed or cancelled) and return its "
        "snapshot including the last reply. timeoutMs caps the wait; when omitted the call waits "
        "until the member settles."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "member_id": {"type": "string", "description": "team member id"},
            "timeoutMs": {
                "type": "integer",
                "description": "optional wait timeout in milliseconds",
            },
        },
        required=["member_id"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        member_id = _required(args, "member_id")
        timeout_s: float | None = None
        raw_timeout = args.get("timeoutMs")
        if raw_timeout is not None:
            if not isinstance(raw_timeout, (int, float)) or isinstance(raw_timeout, bool):
                return ToolResult(text="timeoutMs must be a number", is_error=True)
            timeout_s = max(0.0, float(raw_timeout)) / 1000.0
        try:
            info = await service.wait(member_id, timeout_s)
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"team_wait failed: {exc}", is_error=True)
        return ToolResult(text=_dump({"member": info}))


class TeamCancelTool(BaseTool):
    """Cancel a member and drop its queued messages."""

    name = "team_cancel"
    description = (
        "Cancel a team member: interrupts its current turn, drops queued messages and settles it "
        "as cancelled. Returns cancelled=false when the member does not exist or already failed."
    )
    parallel_safe = False
    parameters = make_parameters(
        {"member_id": {"type": "string", "description": "team member id"}},
        required=["member_id"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        member_id = _required(args, "member_id")
        try:
            cancelled = await service.cancel(member_id)
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"team_cancel failed: {exc}", is_error=True)
        return ToolResult(text=_dump({"member_id": member_id, "cancelled": bool(cancelled)}))


class TeamMembersTool(BaseTool):
    """List team members and their live phase."""

    name = "team_members"
    description = "List every team member with its phase, provider, model and last reply."
    parallel_safe = False
    parameters = make_parameters({}, required=[])

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        return ToolResult(text=_dump({"members": service.members()}))


class TeamTaskCreateTool(BaseTool):
    """Create one item on the shared team task board."""

    name = "team_task_create"
    description = (
        "Create a shared team task in status pending. blockedBy lists task ids or subjects that "
        "must complete before the task can start; writeScopes records the files the task expects "
        "to modify."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "subject": {"type": "string", "description": "task title"},
            "description": {"type": "string", "description": "task details"},
            "blockedBy": {
                "type": "array",
                "items": {"type": "string"},
                "description": "task ids or subjects that must complete first",
            },
            "writeScopes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "workspace paths this task expects to modify",
            },
        },
        required=["subject", "description"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        subject = _required(args, "subject")
        description = _required(args, "description")
        try:
            task = service.create_task(
                subject,
                description,
                blocked_by=_string_list(args, "blockedBy") or (),
                write_scopes=_string_list(args, "writeScopes") or (),
            )
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"team_task_create failed: {exc}", is_error=True)
        return ToolResult(text=_dump({"task": task}))


class TeamTaskListTool(BaseTool):
    """List the shared team task board."""

    name = "team_task_list"
    description = (
        "List shared team tasks with id, revision, status, owner, blockers and write scopes. "
        "status filters to one of pending, in_progress or completed."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed"],
                "description": "optional status filter",
            }
        },
        required=[],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        wanted = _optional(args, "status")
        tasks = service.tasks()
        if wanted is not None:
            tasks = [task for task in tasks if task.status == wanted]
        return ToolResult(text=_dump({"tasks": tasks}))


class TeamTaskGetTool(BaseTool):
    """Read one shared team task."""

    name = "team_task_get"
    description = "Read one shared team task by id."
    parallel_safe = False
    parameters = make_parameters(
        {"task_id": {"type": "string", "description": "team task id"}},
        required=["task_id"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        task_id = _required(args, "task_id")
        task = service.get_task(task_id)
        if task is None:
            return ToolResult(text=f"unknown team task {task_id!r}", is_error=True)
        return ToolResult(text=_dump({"task": task}))


class TeamTaskUpdateTool(BaseTool):
    """Change one shared team task: claim, start, complete, reopen or edit."""

    name = "team_task_update"
    description = (
        "Update a shared team task. action=claim marks it in_progress and assigns the caller as "
        "owner; action=start marks it in_progress (blocked tasks cannot start); action=complete "
        "marks it completed; action=reopen returns it to pending; action=edit changes subject, "
        "description, blockedBy or writeScopes. Every change bumps revision."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "task_id": {"type": "string", "description": "team task id"},
            "action": {
                "type": "string",
                "enum": ["claim", "start", "complete", "reopen", "edit"],
                "description": "task transition",
            },
            "subject": {"type": "string", "description": "new title (edit only)"},
            "description": {"type": "string", "description": "new description (edit only)"},
            "blockedBy": {
                "type": "array",
                "items": {"type": "string"},
                "description": "replacement blockers (edit only)",
            },
            "writeScopes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "replacement write scopes (edit only)",
            },
        },
        required=["task_id", "action"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _team(ctx)
        task_id = _required(args, "task_id")
        action = _required(args, "action")
        try:
            task = service.update_task(
                task_id,
                action,
                subject=_optional(args, "subject"),
                description=_optional(args, "description"),
                blocked_by=_string_list(args, "blockedBy"),
                write_scopes=_string_list(args, "writeScopes"),
            )
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"team_task_update failed: {exc}", is_error=True)
        return ToolResult(text=_dump({"task": task}))


def team_tools() -> list[BaseTool]:
    """Build the lead team tool set (one instance per tool registry)."""
    return [
        TeamSpawnTool(),
        TeamSendTool(),
        TeamWaitTool(),
        TeamCancelTool(),
        TeamMembersTool(),
        TeamTaskCreateTool(),
        TeamTaskListTool(),
        TeamTaskGetTool(),
        TeamTaskUpdateTool(),
    ]


__all__ = [
    "TeamCancelTool",
    "TeamMembersTool",
    "TeamSendTool",
    "TeamSpawnTool",
    "TeamTaskCreateTool",
    "TeamTaskGetTool",
    "TeamTaskListTool",
    "TeamTaskUpdateTool",
    "TeamWaitTool",
    "team_tools",
]
