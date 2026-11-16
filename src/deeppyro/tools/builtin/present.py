"""present: declare existing files as final deliverables.

Paths are resolved against the session cwd and recorded as absolute paths.
Every entry must be an existing regular file whose final path is not a symbolic
link; a successful call also records the files in
`ctx.services["presented"]` for the host/UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool
from deeppyro.tools.params import ToolArgs
from deeppyro.tools.paths import resolve_path

DEFAULT_MAX_FILES = 8


class PresentationItem(ToolArgs):
    """One presented file (the wire schema forbids extra fields)."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Path of an existing regular file.")
    description: str | None = Field(default=None, description="Brief description for the user.")


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

    class Args(ToolArgs):
        """Arguments for present."""

        files: list[PresentationItem] = Field(
            description="Usually the 1-2 most important deliverables; at most 4 per call.",
        )

        @classmethod
        def missing_message(cls, field: str) -> str:
            return "files must be a non-empty list"

        @classmethod
        def field_error_message(cls, field: str, error: dict[str, Any]) -> str | None:
            if field != "files":
                return None
            loc: tuple[Any, ...] = tuple(error.get("loc") or ())
            index = next((part for part in loc if isinstance(part, int)), 0)
            name = str(loc[-1]) if loc else ""
            kind = str(error.get("type", ""))
            if kind == "missing" and name == "files":
                return "files must be a non-empty list"
            if kind in ("list_type", "tuple_type") or error.get("input") is None:
                return "files must be a non-empty list"
            if kind in ("model_type", "dict_type", "model_attributes_type"):
                return f"files[{index}] must be an object"
            if kind == "extra_forbidden":
                return f"files[{index}] has unexpected fields: {loc[-1]}"
            if name == "description":
                return f"files[{index}].description must be a string"
            return f"files[{index}].path must be a non-empty string"

    def __init__(self, max_files: int = DEFAULT_MAX_FILES) -> None:
        self.max_files = max_files

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed = PresentTool.Args.parse(args)
        if not parsed.files:
            raise ToolError("files must be a non-empty list")
        if len(parsed.files) > self.max_files:
            raise ToolError(f"present accepts at most {self.max_files} files")
        base = Path(ctx.cwd).resolve(strict=False)
        records: list[dict[str, Any]] = []
        for index, item in enumerate(parsed.files):
            if not item.path.strip():
                raise ToolError(f"files[{index}].path must be a non-empty string")
            target = resolve_path(base, item.path)
            if target.is_symlink():
                raise ToolError(f'cannot present "{target}": the final path is a symbolic link')
            if not target.exists():
                raise ToolError(f'cannot present "{target}": not found')
            if not target.is_file():
                raise ToolError(f'cannot present "{target}": not a regular file')
            records.append({"path": str(target), "description": item.description})
        presented = ctx.services.setdefault("presented", [])
        if isinstance(presented, list):
            presented.extend(dict(record) for record in records)
        ctx.log("deliverables/presented", {"files": records})
        text = "\n".join(f"Presented {record['path']}" for record in records)
        return ToolResult(text=text, meta={"files": [record["path"] for record in records]})


__all__ = ["DEFAULT_MAX_FILES", "PresentTool"]
