"""present: declare existing files as final deliverables.

Paths are resolved against the session cwd and recorded as absolute paths.
Every entry must be an existing regular file whose final path is not a symbolic
link; a successful call also records the files in
`ctx.services["presented"]` for the host/UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.paths import resolve_path

DEFAULT_MAX_FILES = 8


class PresentTool(BaseTool):
    """Declare existing files as final deliverables for the user."""

    name = "present"
    description = (
        "Declare existing files as final deliverables for the user. Hosts may show presented files only after "
        "the turn ends, so call it once the files are final. Use it when the user needs a separate file, "
        "especially Office documents, spreadsheets, and slide decks; prefer the final response when that "
        "suffices."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "files": {
                "type": "array",
                "description": "Usually the 1-2 most important deliverables; at most 4 per call.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path of an existing regular file."},
                        "description": {"type": "string", "description": "Brief description for the user."},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            }
        },
        required=["files"],
    )

    def __init__(self, max_files: int = DEFAULT_MAX_FILES) -> None:
        self.max_files = max_files

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = args.get("files")
        if not isinstance(raw, list) or not raw:
            raise ToolError("files must be a non-empty list")
        if len(raw) > self.max_files:
            raise ToolError(f"present accepts at most {self.max_files} files")
        base = Path(ctx.cwd).resolve(strict=False)
        records: list[dict[str, str]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ToolError(f"files[{index}] must be an object")
            extra = set(item) - {"path", "description"}
            if extra:
                raise ToolError(f"files[{index}] has unexpected fields: {', '.join(sorted(extra))}")
            path_value = item.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                raise ToolError(f"files[{index}].path must be a non-empty string")
            description = item.get("description")
            if description is not None and not isinstance(description, str):
                raise ToolError(f"files[{index}].description must be a string")
            target = resolve_path(base, path_value)
            if target.is_symlink():
                raise ToolError(f'cannot present "{target}": the final path is a symbolic link')
            if not target.exists():
                raise ToolError(f'cannot present "{target}": not found')
            if not target.is_file():
                raise ToolError(f'cannot present "{target}": not a regular file')
            records.append({"path": str(target), "description": description})
        presented = ctx.services.setdefault("presented", [])
        if isinstance(presented, list):
            presented.extend(dict(record) for record in records)
        ctx.log("deliverables/presented", {"files": records})
        text = "\n".join(f"Presented {record['path']}" for record in records)
        return ToolResult(text=text, meta={"files": [record["path"] for record in records]})


__all__ = ["DEFAULT_MAX_FILES", "PresentTool"]
