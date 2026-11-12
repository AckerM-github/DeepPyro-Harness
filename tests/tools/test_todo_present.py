"""todo_write / present: validation, state, and event reporting."""

from __future__ import annotations

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.tools import PresentTool, TodoWriteTool
from tests.tools.helpers import make_context, write_file


async def test_todo_write_replaces_the_whole_list(workspace) -> None:  # type: ignore[no-untyped-def]
    events: list[tuple[str, dict]] = []
    ctx = make_context(workspace, events=events)
    tool = TodoWriteTool()
    result = await tool.run(
        {
            "todos": [
                {"content": "plan", "status": "pending"},
                {"content": "build", "status": "in_progress"},
                {"content": "ship", "status": "completed"},
            ]
        },
        ctx,
    )
    assert result.is_error is False
    assert result.text == "Updated todo list: 1 pending, 1 in progress, 1 completed."
    assert tool.todos == [
        {"content": "plan", "status": "pending"},
        {"content": "build", "status": "in_progress"},
        {"content": "ship", "status": "completed"},
    ]
    assert ctx.services["todos"] == tool.todos
    assert events[-1][0] == "todo/write"
    assert events[-1][1]["todos"] == tool.todos
    assert result.meta["counts"] == {"pending": 1, "in_progress": 1, "completed": 1}

    second = await tool.run({"todos": [{"content": "only", "status": "pending"}]}, ctx)
    assert second.text == "Updated todo list: 1 pending, 0 in progress, 0 completed."
    assert tool.todos == [{"content": "only", "status": "pending"}]


async def test_todo_write_validation(workspace) -> None:  # type: ignore[no-untyped-def]
    tool = TodoWriteTool()
    ctx = make_context(workspace)
    with pytest.raises(ToolError, match="todos must be a list"):
        await tool.run({"todos": "nope"}, ctx)
    with pytest.raises(ToolError, match="todos must be a list"):
        await tool.run({}, ctx)
    with pytest.raises(ToolError, match="status"):
        await tool.run({"todos": [{"content": "x", "status": "done"}]}, ctx)
    with pytest.raises(ToolError, match="content"):
        await tool.run({"todos": [{"content": "", "status": "pending"}]}, ctx)
    with pytest.raises(ToolError, match="duplicate"):
        await tool.run(
            {
                "todos": [
                    {"content": "same", "status": "pending"},
                    {"content": "same", "status": "completed"},
                ]
            },
            ctx,
        )
    with pytest.raises(ToolError, match="unexpected"):
        await tool.run({"todos": [{"content": "x", "status": "pending", "id": 1}]}, ctx)


async def test_present_records_absolute_paths(workspace) -> None:  # type: ignore[no-untyped-def]
    first = write_file(workspace, "out/report.md", "# report\n")
    second = write_file(workspace, "out/chart.png", "png")
    events: list[tuple[str, dict]] = []
    ctx = make_context(workspace, events=events)
    result = await PresentTool().run(
        {"files": [{"path": "out/report.md"}, {"path": str(second), "description": "chart"}]},
        ctx,
    )
    assert result.is_error is False
    assert result.text == f"Presented {first}\nPresented {second}"
    assert ctx.services["presented"] == [
        {"path": str(first), "description": None},
        {"path": str(second), "description": "chart"},
    ]
    assert events[-1][0] == "deliverables/presented"
    assert result.meta["files"] == [str(first), str(second)]


async def test_present_validation(workspace) -> None:  # type: ignore[no-untyped-def]
    tool = PresentTool()
    ctx = make_context(workspace)
    with pytest.raises(ToolError, match="files must be a non-empty list"):
        await tool.run({"files": []}, ctx)
    with pytest.raises(ToolError, match="files must be a non-empty list"):
        await tool.run({}, ctx)
    with pytest.raises(ToolError, match="not found"):
        await tool.run({"files": [{"path": "missing.md"}]}, ctx)
    directory = workspace / "dir"
    directory.mkdir()
    with pytest.raises(ToolError, match="not a regular file"):
        await tool.run({"files": [{"path": "dir"}]}, ctx)
    write_file(workspace, "ok.txt", "x")
    with pytest.raises(ToolError, match="unexpected"):
        await tool.run({"files": [{"path": "ok.txt", "bytes": 1}]}, ctx)
    write_file(workspace, "second.txt", "y")
    with pytest.raises(ToolError, match="at most 1"):
        await PresentTool(max_files=1).run({"files": [{"path": "ok.txt"}, {"path": "second.txt"}]}, ctx)
