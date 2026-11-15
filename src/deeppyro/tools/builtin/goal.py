"""The goal tool: let the model record or drop the session goal."""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool


class GoalTool(BaseTool):
    """Read, set or clear the session goal."""

    name = "goal"
    description = (
        "Read or update the goal of this session. Call it with no arguments to read, with "
        "text to set a short goal (one or two sentences), or with clear=true to drop it. "
        "The goal is recorded in the session log and shown in every later turn."
    )
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The new goal. Omit to read the current one.",
            },
            "clear": {
                "type": "boolean",
                "description": "Drop the current goal instead of setting one.",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("goal")
        if service is None:
            raise ToolError("goal is not available in this session")
        session = ctx.session_id
        service.bind(session, record=ctx.log)
        if args.get("clear"):
            await service.clear(session)
            return ToolResult(text="goal cleared", meta={"goal": None})
        text = args.get("text")
        if text is None:
            goal = service.get(session)
            return ToolResult(text=goal or "no goal", meta={"goal": goal})
        goal = await service.set(session, str(text))
        return ToolResult(text=f"goal set: {goal}", meta={"goal": goal})


__all__ = ["GoalTool"]
