"""todo_write: replace the session's todo list and report status counts.

The list is replaced as a whole on every call (there is no partial update), so
the model always sees a consistent plan. A successfully written list is also
mirrored into `ctx.services["todos"]` for the session/TUI.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool
from deeppyro.tools.params import ToolArgs

TODO_STATUSES = ("pending", "in_progress", "completed")


class TodoItem(ToolArgs):
    """One entry of the todo list (the wire schema forbids extra fields)."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(description="What the task is - a short imperative line.")
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="pending (not started) | in_progress (now) | completed (done).",
    )


class TodoWriteTool(BaseTool):
    """Record and update a task list to plan multi-step work."""

    name = "todo_write"
    description = (
        "Record and update a task list to plan multi-step work and show progress; skip it for trivial "
        "single-step tasks. Add one todo per concrete step before starting. The complete list replaces any "
        "previous list."
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for todo_write."""

        todos: list[TodoItem] = Field(
            description="The complete task list, replacing any previous list.",
        )

        @classmethod
        def missing_message(cls, field: str) -> str:
            return "todos must be a list"

        @classmethod
        def field_error_message(cls, field: str, error: dict[str, Any]) -> str | None:
            if field != "todos":
                return None
            loc: tuple[Any, ...] = tuple(error.get("loc") or ())
            index = next((part for part in loc if isinstance(part, int)), 0)
            name = str(loc[-1]) if loc else ""
            kind = str(error.get("type", ""))
            if kind in ("list_type", "tuple_type", "missing") or error.get("input") is None:
                if name == "content":
                    return f"todo item {index} content must be a non-empty string"
                if name == "status":
                    return f"todo item {index} status must be one of: {', '.join(TODO_STATUSES)}"
                return "todos must be a list"
            if kind in ("model_type", "dict_type", "model_attributes_type"):
                return f"todo item {index} must be an object"
            if kind == "extra_forbidden":
                return f"todo item {index} has unexpected fields: {loc[-1]}"
            if kind in ("string_type", "string_unicode"):
                return f"todo item {index} content must be a non-empty string"
            if kind == "literal_error":
                return f"todo item {index} status must be one of: {', '.join(TODO_STATUSES)}"
            return None

    def __init__(self) -> None:
        self.todos: list[dict[str, str]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed = TodoWriteTool.Args.parse(args)
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(parsed.todos):
            if not item.content.strip():
                raise ToolError(f"todo item {index} content must be a non-empty string")
            if item.content in seen:
                raise ToolError(f"todo item {index} content is duplicated: {item.content!r}")
            seen.add(item.content)
            normalized.append({"content": item.content, "status": item.status})
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
