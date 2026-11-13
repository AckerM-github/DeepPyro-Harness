"""working_directory: read or change the session working directory."""

from __future__ import annotations

from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool, make_parameters


class WorkingDirectoryTool(BaseTool):
    """Read the current working directory, or change it with cd."""

    name = "working_directory"
    description = (
        "Read the current working directory, or change it with cd. Relative paths use the current "
        "directory. Existing shells and running processes keep their own directories."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "cd": {
                "type": "string",
                "description": "Existing directory to enter. Omit to read the current directory.",
            }
        }
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("working_directory")
        if service is None:
            raise ToolError("working_directory service is not available in this session")
        if "cd" not in args:
            path = service.get(ctx.session_id)
            return ToolResult(text=str(path), meta={"cwd": str(path)})
        cd = args.get("cd")
        if not isinstance(cd, str):
            raise ToolError("cd must be a string")
        path = await service.set(ctx.session_id, cd, cancel=ctx.cancel)
        return ToolResult(text=str(path), meta={"cwd": str(path)})


__all__ = ["WorkingDirectoryTool"]
