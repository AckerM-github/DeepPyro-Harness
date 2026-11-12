"""todo_write: replace the session's todo list and report status counts.

The list is replaced as a whole on every call (there is no partial update), so
the model always sees a consistent plan. A successfully written list is also
mirrored into `ctx.services["todos"]` for the session/TUI.
"""

from __future__ import annotations

from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool, make_parameters

TODO_STATUSES = ("pending", "in_progress", "completed")


class TodoWriteTool(BaseTool):
    """Record and update a task list to plan multi-step work."""

    name = "todo_write"
    description = (
        "Record and update a task list to plan multi-step work and show progress; skip it for trivial "
        "single-step tasks. Add one todo per concrete step before starting. The complete list replaces any "
        "previous list."
    )
    parallel_safe = False
    parameters = make_parameters(
        {
            "todos": {
                "type": "array",
                "description": "The complete task list, replacing any previous list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "What the task is - a short imperative line.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(TODO_STATUSES),
                            "description": "pending (not started) | in_progress (now) | completed (done).",
                        },
                    },
                    "required": ["content", "status"],
                    "additionalProperties": False,
                },
            }
        },
        required=["todos"],
    )

    def __init__(self) -> None:
        self.todos: list[dict[str, str]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw = args.get("todos")
        if not isinstance(raw, list):
            raise ToolError("todos must be a list")
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ToolError(f"todo item {index} must be an object")
            extra = set(item) - {"content", "status"}
            if extra:
                raise ToolError(f"todo item {index} has unexpected fields: {', '.join(sorted(extra))}")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ToolError(f"todo item {index} content must be a non-empty string")
            status = item.get("status")
            if status not in TODO_STATUSES:
                raise ToolError(f"todo item {index} status must be one of: {', '.join(TODO_STATUSES)}")
            if content in seen:
                raise ToolError(f"todo item {index} content is duplicated: {content!r}")
            seen.add(content)
            normalized.append({"content": content, "status": status})
        self.todos = normalized
        ctx.services["todos"] = [dict(item) for item in normalized]
        ctx.log("todo/write", {"todos": [dict(item) for item in normalized]})
        counts = {
            status: sum(1 for item in normalized if item["status"] == status) for status in TODO_STATUSES
        }
        text = (
            f"Updated todo list: {counts['pending']} pending, {counts['in_progress']} in progress, "
            f"{counts['completed']} completed."
        )
        return ToolResult(text=text, meta={"todos": [dict(item) for item in normalized], "counts": counts})


__all__ = ["TODO_STATUSES", "TodoWriteTool"]
