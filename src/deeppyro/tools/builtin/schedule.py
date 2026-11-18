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
from typing import Any, Literal

from pydantic import AliasChoices, Field

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.schedule import ScheduleError, ScheduleInputError
from deeppyro.tools.base import BaseTool
from deeppyro.tools.params import OptionalNonEmptyStr, ToolArgs, require_value, schema_enum, schema_number

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


def authorization_section(visible_tools: Any = None, _prompt_ctx: Any = None) -> str:
    """Render the management authorization sentence for the system prompt.

    The section is omitted when the session does not expose the schedule tool,
    mirroring upstream, which only contributes it when schedule create/edit tools are
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

    class Args(ToolArgs):
        """Arguments for the schedule tool."""

        action: Literal["create", "list", "update", "delete"] = Field(
            description=(
                "create = schedule a task; list = read tasks; update = change or pause/resume; "
                "delete = remove."
            ),
        )
        id: str | None = Field(
            default=None,
            description="Task id returned by create/list; required for update and delete.",
        )
        title: str | None = Field(
            default=None,
            description=(
                "Short task title in the user's language (max 120 characters); required for create."
            ),
        )
        prompt: str | None = Field(
            default=None,
            description="Message delivered when the task fires; required for create.",
        )
        rrule: str | None = Field(
            default=None,
            description=RRULE_HELP + " Required for create, optional for update.",
        )
        target: OptionalNonEmptyStr = Field(
            default=None,
            json_schema_extra=schema_enum(*TARGET_CHOICES),
            description=(
                "this_session = deliver back to this session (default); new_once = start one new "
                "session at the first delivery; new = start a new session for every occurrence."
            ),
        )
        active: bool | None = Field(
            default=None,
            description="Update only: false pauses a task, true resumes it.",
        )
        limit: int | None = Field(
            default=None,
            ge=1,
            le=500,
            json_schema_extra=schema_number(),
            description="List only: page size 1-500 (default 100).",
        )
        offset: int | None = Field(
            default=None,
            ge=0,
            json_schema_extra=schema_number(),
            description="List only: page offset (default 0).",
        )
        expectedUpdatedAt: OptionalNonEmptyStr = Field(  # noqa: N815
            default=None,
            validation_alias=AliasChoices("expectedUpdatedAt", "expected_updated_at"),
            description=(
                "Update only: updatedAt from a previous read; rejects a concurrent change with "
                "schedule_conflict."
            ),
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        parsed = ScheduleTool.Args.parse(args)
        action = parsed.action
        if await _delegation_depth(service, ctx) > 0:
            raise ToolError("subagent_session: scheduled tasks are not available in child sessions")
        if action == "create":
            return await self._create(service, parsed, ctx)
        if action == "list":
            return await self._list(service, parsed)
        if action == "update":
            return await self._update(service, parsed, ctx)
        return await self._delete(service, parsed)

    async def _create(self, service: Any, parsed: ScheduleTool.Args, ctx: ToolContext) -> ToolResult:
        request: dict[str, Any] = {
            "title": require_value(parsed.title, "title"),
            "prompt": require_value(parsed.prompt, "prompt"),
            "rrule": require_value(parsed.rrule, "rrule"),
        }
        target = parsed.target
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

    async def _list(self, service: Any, parsed: ScheduleTool.Args) -> ToolResult:
        active = parsed.active
        limit = parsed.limit if parsed.limit is not None else 100
        offset = parsed.offset if parsed.offset is not None else 0
        try:
            page = await service.list(active=active, limit=limit, offset=offset)
        except ScheduleError as exc:
            raise _tool_error(exc) from exc
        return ToolResult(
            text=_json(page),
            meta={"count": len(page["tasks"]), "total": page["total"]},
        )

    async def _update(self, service: Any, parsed: ScheduleTool.Args, ctx: ToolContext) -> ToolResult:
        task_id = require_value(parsed.id, "id")
        patch: dict[str, Any] = {}
        for field in ("title", "prompt", "rrule"):
            value = getattr(parsed, field)
            if value is not None:
                patch[field] = require_value(value, field)
        if parsed.target is not None:
            patch["target"] = parsed.target
        if parsed.active is not None:
            patch["active"] = parsed.active
        if not patch:
            raise ToolError("update requires at least one of title, prompt, rrule, target or active")
        try:
            record = await service.update(
                task_id,
                patch,
                expected_updated_at=parsed.expectedUpdatedAt,
                actor_session_id=ctx.session_id,
            )
        except ScheduleError as exc:
            raise _tool_error(exc) from exc
        return ToolResult(
            text=_json(service.task_model(record)),
            meta={"scheduleId": record.id, "status": record.status},
        )

    async def _delete(self, service: Any, parsed: ScheduleTool.Args) -> ToolResult:
        task_id = require_value(parsed.id, "id")
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
