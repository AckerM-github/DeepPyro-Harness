"""Agent-facing schedule tool: create, list, update and delete reminders.

The tool resolves the schedule service from ToolContext.services, so the
kernel schedule plugin owns the manager and the runtime decides whether a
session exposes the capability. Child sessions (delegation depth > 0) are
rejected even when the runtime forgets to filter the tool, because scheduled
deliveries always target a top-level session.

Every mutation requires the user's explicit request; the management
authorization sentence is also registered as a prompt section by the plugin.
"""

from __future__ import annotations

import json
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.schedule import ScheduleError, ScheduleInputError
from deeppyro.tools.args import optional_bool, optional_int, optional_str, require_str
from deeppyro.tools.base import BaseTool, make_parameters

#: Verbatim sentence the system prompt shows next to the schedule tool; the
#: model must only schedule work the user explicitly asked for.
MANAGEMENT_AUTHORIZATION = (
    "Only create or modify scheduled tasks at the user's explicit request; "
    "prior explicit user authorization for the running scheduled task remains valid."
)

#: Prompt section name and order (core rules live at order 50).
SCHEDULE_PROMPT_SECTION = "tool:schedule"
SCHEDULE_PROMPT_ORDER = 45

ACTIONS = ("create", "list", "update", "delete")
TARGET_CHOICES = ("this_session", "new_once", "new")

RRULE_HELP = (
    "Two lines: 'DTSTART:YYYYMMDDTHHMMSSZ' and 'RRULE:FREQ=...'. Supported FREQ values are "
    "DAILY, WEEKLY, MONTHLY and YEARLY with COUNT, UNTIL, INTERVAL, BYDAY, BYMONTHDAY, BYHOUR, "
    "BYMINUTE and BYSECOND parts; this is the timing input for every recurring task."
)


def _service(ctx: ToolContext) -> Any:
    service = (ctx.services or {}).get("schedule")
    if service is None:
        raise ToolError("schedule service is not available in this session")
    return service


def _tool_error(exc: Exception) -> ToolError:
    """Map a schedule error to the model-facing ToolError text."""
    if isinstance(exc, ScheduleInputError):
        return ToolError(f"{exc.code}: {exc.message}")
    return ToolError(f"schedule failed: {exc}")


async def _delegation_depth(service: Any, ctx: ToolContext) -> int:
    try:
        return int(await service.delegation_depth(ctx.session_id))
    except Exception:  # noqa: BLE001 - the host probe must never break the tool
        return 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _expected_updated_at(args: dict[str, Any]) -> str | None:
    if "expectedUpdatedAt" in args:
        return optional_str(args, "expectedUpdatedAt")
    return optional_str(args, "expected_updated_at")


def authorization_section(visible_tools: Any = None, _prompt_ctx: Any = None) -> str:
    """Render the management authorization sentence for the system prompt.

    The section is omitted when the session does not expose the schedule tool,
    mirroring DSH, which only contributes it when schedule create/edit tools are
    visible.
    """
    if visible_tools is not None:
        names = {str(getattr(tool, "name", "")) for tool in visible_tools}
        if names and "schedule" not in names:
            return ""
    return MANAGEMENT_AUTHORIZATION


class ScheduleTool(BaseTool):
    """Create, list, update and delete scheduled reminders."""

    name = "schedule"
    description = (
        "Create, list, update or delete scheduled reminders. A task delivers its prompt to a "
        "session at one or more future times. Use it only when the user explicitly asks for a "
        "reminder, follow-up or scheduled task."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "create = schedule a task; list = read tasks; update = change or pause/resume; delete = remove.",
            },
            "id": {
                "type": "string",
                "description": "Task id returned by create/list; required for update and delete.",
            },
            "title": {
                "type": "string",
                "description": "Short task title in the user's language (max 120 characters); required for create.",
            },
            "prompt": {
                "type": "string",
                "description": "Message delivered when the task fires; required for create.",
            },
            "rrule": {
                "type": "string",
                "description": RRULE_HELP + " Required for create, optional for update.",
            },
            "target": {
                "type": "string",
                "enum": list(TARGET_CHOICES),
                "description": (
                    "this_session = deliver back to this session (default); new_once = start one new "
                    "session at the first delivery; new = start a new session for every occurrence."
                ),
            },
            "active": {
                "type": "boolean",
                "description": "Update only: false pauses a task, true resumes it.",
            },
            "limit": {
                "type": "number",
                "description": "List only: page size 1-500 (default 100).",
            },
            "offset": {
                "type": "number",
                "description": "List only: page offset (default 0).",
            },
            "expectedUpdatedAt": {
                "type": "string",
                "description": "Update only: updatedAt from a previous read; rejects a concurrent change with schedule_conflict.",
            },
        },
        required=["action"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        action = require_str(args, "action")
        if action not in ACTIONS:
            raise ToolError(f"action must be one of: {', '.join(ACTIONS)}")
        if await _delegation_depth(service, ctx) > 0:
            raise ToolError("subagent_session: scheduled tasks are not available in child sessions")
        if action == "create":
            return await self._create(service, args, ctx)
        if action == "list":
            return await self._list(service, args)
        if action == "update":
            return await self._update(service, args, ctx)
        return await self._delete(service, args)

    async def _create(self, service: Any, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        request: dict[str, Any] = {
            "title": require_str(args, "title"),
            "prompt": require_str(args, "prompt"),
            "rrule": require_str(args, "rrule"),
        }
        target = optional_str(args, "target")
        if target is not None:
            request["target"] = target
        try:
            record = await service.create(request, actor_session_id=ctx.session_id)
        except ScheduleError as exc:
            raise _tool_error(exc) from exc
        return ToolResult(
            text=_json(service.task_model(record)),
            meta={"scheduleId": record.id, "status": record.status},
        )

    async def _list(self, service: Any, args: dict[str, Any]) -> ToolResult:
        active = optional_bool(args, "active") if args.get("active") is not None else None
        limit = optional_int(args, "limit", 100, minimum=1, maximum=500)
        offset = optional_int(args, "offset", 0, minimum=0)
        try:
            page = await service.list(active=active, limit=limit, offset=offset)
        except ScheduleError as exc:
            raise _tool_error(exc) from exc
        return ToolResult(
            text=_json(page),
            meta={"count": len(page["tasks"]), "total": page["total"]},
        )

    async def _update(self, service: Any, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task_id = require_str(args, "id")
        patch: dict[str, Any] = {}
        for field in ("title", "prompt", "rrule"):
            if args.get(field) is not None:
                patch[field] = require_str(args, field)
        if args.get("target") is not None:
            patch["target"] = optional_str(args, "target")
        if args.get("active") is not None:
            patch["active"] = optional_bool(args, "active")
        if not patch:
            raise ToolError("update requires at least one of title, prompt, rrule, target or active")
        try:
            record = await service.update(
                task_id,
                patch,
                expected_updated_at=_expected_updated_at(args),
                actor_session_id=ctx.session_id,
            )
        except ScheduleError as exc:
            raise _tool_error(exc) from exc
        return ToolResult(
            text=_json(service.task_model(record)),
            meta={"scheduleId": record.id, "status": record.status},
        )

    async def _delete(self, service: Any, args: dict[str, Any]) -> ToolResult:
        task_id = require_str(args, "id")
        try:
            deleted = await service.delete(task_id)
        except ScheduleError as exc:
            raise _tool_error(exc) from exc
        if not deleted:
            raise ToolError(f"unknown_task: schedule {task_id} does not exist")
        return ToolResult(text=_json({"deleted": task_id}), meta={"scheduleId": task_id})


def schedule_tools() -> list[BaseTool]:
    """The schedule tool set registered by the schedule plugin."""
    return [ScheduleTool()]


__all__ = [
    "ACTIONS",
    "MANAGEMENT_AUTHORIZATION",
    "RRULE_HELP",
    "SCHEDULE_PROMPT_ORDER",
    "SCHEDULE_PROMPT_SECTION",
    "TARGET_CHOICES",
    "ScheduleTool",
    "authorization_section",
    "schedule_tools",
]
