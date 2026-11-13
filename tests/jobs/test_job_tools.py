"""job_output, job_kill and job_list tool behaviour."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deeppyro.builtin.jobs import JobsPlugin
from deeppyro.core.errors import ToolError
from deeppyro.jobs import JobHandle, JobsService
from deeppyro.tools.builtin.jobs import (
    OUTPUT_PREVIEW_CHARS,
    JobKillTool,
    JobListTool,
    JobOutputTool,
    job_tools,
)
from tests.tools.helpers import make_context


def service_ctx(
    workspace: Path, service: JobsService, events: list[tuple[str, dict[str, Any]]] | None = None
):
    return make_context(workspace, services={"jobs": service}, events=events)


async def waiting(handle: JobHandle) -> int:
    handle.write("started\n")
    await asyncio.Event().wait()
    return 0


async def immediate(handle: JobHandle, code: int = 0, text: str = "done") -> int:
    handle.write(text)
    return code


def test_plugin_metadata_and_tool_set() -> None:
    assert JobsPlugin.provide == "jobs"
    assert JobsPlugin.plugin_id == "jobs"
    assert "tools" in JobsPlugin.inject
    tools = job_tools()
    assert [tool.name for tool in tools] == ["job_output", "job_kill", "job_list"]
    assert all(tool.parallel_safe for tool in tools)


async def test_tools_require_the_jobs_service(workspace) -> None:  # type: ignore[no-untyped-def]
    ctx = make_context(workspace)
    with pytest.raises(ToolError, match="jobs service is not available"):
        await JobOutputTool().run({"job_id": "x"}, ctx)
    with pytest.raises(ToolError, match="jobs service is not available"):
        await JobKillTool().run({"job_id": "x"}, ctx)
    with pytest.raises(ToolError, match="jobs service is not available"):
        await JobListTool().run({}, ctx)


async def test_unknown_job_is_rejected(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = service_ctx(workspace, service)
    with pytest.raises(ToolError, match="unknown job"):
        await JobOutputTool().run({"job_id": "pwsh-missing"}, ctx)
    with pytest.raises(ToolError, match="unknown job"):
        await JobKillTool().run({"job_id": "pwsh-missing"}, ctx)


async def test_job_list_empty_and_populated(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = service_ctx(workspace, service)
    empty = await JobListTool().run({}, ctx)
    assert empty.text == "No background jobs."
    assert empty.meta == {"count": 0}

    first = service.start("pwsh", "echo hi", immediate)
    second = service.start("bash", "multi\nline label", immediate)
    await asyncio.gather(first.wait(5), second.wait(5))
    result = await JobListTool().run({}, ctx)
    lines = result.text.splitlines()
    assert result.meta == {"count": 2}
    assert lines[0] == f"{first.id} [pwsh] completed — echo hi"
    assert lines[1] == f"{second.id} [bash] completed — multi line label"


async def test_job_output_completed_and_empty(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = service_ctx(workspace, service)
    handle = service.start("pwsh", "produce", immediate)
    await handle.wait(5)
    result = await JobOutputTool().run({"job_id": handle.id}, ctx)
    assert result.is_error is False
    assert result.text == "done\n[status: completed, exit code: 0]"
    assert result.meta["status"] == "completed"
    assert result.meta["exitCode"] == 0
    assert result.meta["truncated"] is False

    blank = service.start("pwsh", "blank", lambda h: immediate(h, 0, ""))
    await blank.wait(5)
    empty = await JobOutputTool().run({"job_id": blank.id}, ctx)
    assert empty.text == "(no new output)\n[status: completed, exit code: 0]"


async def test_job_output_waits_for_completion(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = service_ctx(workspace, service)

    async def delayed(handle: JobHandle) -> int:
        await asyncio.sleep(0.05)
        handle.write("late output")
        return 0

    handle = service.start("pwsh", "delayed", delayed)
    result = await JobOutputTool().run(
        {"job_id": handle.id, "wait": True, "timeout_ms": 5000},
        ctx,
    )
    assert result.meta["waitTimedOut"] is False
    assert result.meta["status"] == "completed"
    assert result.text == "late output\n[status: completed, exit code: 0]"


async def test_job_output_wait_timeout_leaves_job_running(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = service_ctx(workspace, service)
    handle = service.start("pwsh", "waiting", waiting)
    await asyncio.sleep(0)
    result = await JobOutputTool().run(
        {"job_id": handle.id, "wait": True, "timeout_ms": 10},
        ctx,
    )
    assert result.meta["waitTimedOut"] is True
    assert result.meta["status"] == "running"
    assert result.text.endswith("[status: running]")
    await service.kill(handle.id)


async def test_job_output_truncates_long_output(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = service_ctx(workspace, service)

    async def long_output(handle: JobHandle) -> int:
        handle.write("x" * 5000)
        return 0

    handle = service.start("pwsh", "long", long_output)
    await handle.wait(5)
    result = await JobOutputTool().run({"job_id": handle.id}, ctx)
    assert result.meta["truncated"] is True
    assert result.meta["omittedChars"] > 0
    assert result.meta["outputChars"] == 5000
    assert "[truncated" in result.text
    assert len(result.text) < 5000


async def test_job_kill_running_and_finished(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = service_ctx(workspace, service)
    handle = service.start("pwsh", "waiting", waiting)
    await asyncio.sleep(0)
    killed = await JobKillTool().run({"job_id": handle.id, "reason": "stop it"}, ctx)
    assert killed.text == f"kill requested for {handle.id}\n[status: cancelled]"
    assert killed.meta["status"] == "cancelled"
    assert killed.meta["reason"] == "stop it"

    done = service.start("pwsh", "quick", immediate)
    await done.wait(5)
    already = await JobKillTool().run({"job_id": done.id}, ctx)
    assert already.meta["status"] == "completed"
    assert already.text.endswith("[status: completed, exit code: 0]")


async def test_job_output_preview_budget_is_used() -> None:
    assert OUTPUT_PREVIEW_CHARS == 4000
