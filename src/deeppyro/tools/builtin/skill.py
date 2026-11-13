"""The model-facing skill loading tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.skills.frontmatter import is_valid_skill_name
from deeppyro.skills.registry import render_skill_content
from deeppyro.tools.args import require_str
from deeppyro.tools.base import BaseTool, make_parameters

SKILL_TOOL_DESCRIPTION = (
    "Load the full instructions for a skill. Call it before acting on a task "
    "that names or clearly matches a skill in the session skill catalog."
)


class SkillTool(BaseTool):
    """Load one skill by name from the registry service."""

    name = "skill"
    description = SKILL_TOOL_DESCRIPTION
    parallel_safe = True
    parameters = make_parameters(
        {
            "name": {
                "type": "string",
                "description": "The exact skill name from the available skills list.",
            }
        },
        required=["name"],
    )

    def __init__(self, service: Any) -> None:
        self._service = service

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = require_str(args, "name").strip()
        if not is_valid_skill_name(name):
            raise ToolError(f'invalid skill name "{name}"')
        definition = self._service.get(name, cwd=Path(ctx.cwd))
        if definition is None:
            raise ToolError(f'skill "{name}" is unknown or no longer available')
        if not definition.model_invocable:
            raise ToolError(f'skill "{name}" is not available for model invocation')
        return ToolResult(
            text=render_skill_content(definition),
            meta={
                "name": definition.name,
                "path": definition.path,
                "source": definition.source,
            },
        )


def skill_tools(service: Any) -> list[SkillTool]:
    """Build the tool list registered by the skills plugin."""
    return [SkillTool(service)]


__all__ = ["SKILL_TOOL_DESCRIPTION", "SkillTool", "skill_tools"]
