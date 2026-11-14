"""Background shell execution: real subprocesses delegated to the jobs service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.jobs import JobsService
from deeppyro.sandbox import PermissionGate
from deeppyro.tools import PwshTool
from deeppyro.tools.builtin.jobs import JobKillTool, JobOutputTool
from tests.tools.helpers import ConfiningSandbox, make_context


def jobs_context(
    workspace: Path,
    service: JobsService,
    *,
    permission: PermissionGate | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
):
    return make_context(
        workspace,
        permission=permission,
        services={"jobs": service},
        events=events,
    )


async def test_background_pwsh_returns_job_id_immediately(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    events: list[tuple[str, dict[str, Any]]] = []
    ctx = jobs_context(workspace, service, events=events)

    result = await PwshTool().run(
        {
            "description": "Print after a short delay",
            "command": "Start-Sleep -Milliseconds 300; Write-Output 'bg-done'",
            "run_in_background": True,
        },
        ctx,
    )
    assert result.is_error is False
    assert result.text.startswith("started background job ")
    job_id = result.meta["jobId"]
    assert result.meta["kind"] == "pwsh"
    assert job_id.startswith("pwsh-")
    assert service.get(job_id) is not None
    assert [event for event, _ in events] == ["job/registered"]

    settled = await service.wait(job_id, 15)
    assert settled is not None
    assert settled.status == "completed"
    assert settled.exit_code == 0
    assert "bg-done" in settled.output
    assert [event for event, _ in events] == ["job/registered", "job/settled"]
    assert events[1][1]["jobId"] == job_id
    assert events[1][1]["toolCall"]["callId"] == ctx.call_id


async def test_background_output_is_readable_through_job_output(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = jobs_context(workspace, service)
    started = await PwshTool().run(
        {
            "description": "Emit background output",
            "command": "Start-Sleep -Milliseconds 400; Write-Output 'background-line'",
            "run_in_background": True,
        },
        ctx,
    )
    job_id = started.meta["jobId"]
    output = await JobOutputTool().run(
        {"job_id": job_id, "wait": True, "timeout_ms": 15000},
        ctx,
    )
    assert output.meta["status"] == "completed"
    assert "background-line" in output.text
    assert output.text.endswith("[status: completed, exit code: 0]")


async def test_background_job_can_be_killed(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = jobs_context(workspace, service)
    started = await PwshTool().run(
        {
            "description": "Sleep until killed",
            "command": "Start-Sleep -Seconds 30",
            "run_in_background": True,
        },
        ctx,
    )
    job_id = started.meta["jobId"]
    killed = await JobKillTool().run({"job_id": job_id, "reason": "test cleanup"}, ctx)
    assert killed.meta["status"] == "cancelled"
    handle = service.get(job_id)
    assert handle is not None
    assert handle.status == "cancelled"
    assert handle.cause == "kill"


async def test_background_requires_the_jobs_service(workspace) -> None:  # type: ignore[no-untyped-def]
    ctx = make_context(workspace)
    with pytest.raises(ToolError, match="requires the jobs service"):
        await PwshTool().run(
            {"description": "Background", "command": "Write-Output 1", "run_in_background": True},
            ctx,
        )


async def test_background_runs_in_read_only_mode_through_the_runner(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    gate = PermissionGate("read-only", "never", workspace)
    sandbox = ConfiningSandbox()
    ctx = make_context(workspace, permission=gate, services={"jobs": service, "sandbox": sandbox})
    started = await PwshTool().run(
        {"description": "Background", "command": "Write-Output 1", "run_in_background": True},
        ctx,
    )
    job_id = started.meta["jobId"]
    settled = await service.wait(job_id, 15)
    assert settled is not None and settled.status == "completed"
    assert [policy.mode for policy in sandbox.policies] == ["read-only"]


async def test_background_marks_a_sandbox_denial(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    gate = PermissionGate("workspace-write", "never", workspace)
    sandbox = ConfiningSandbox(denial_signatures=("access is denied",))
    ctx = make_context(workspace, permission=gate, services={"jobs": service, "sandbox": sandbox})
    started = await PwshTool().run(
        {
            "description": "Denied in the background",
            "command": "Write-Output 'access is denied'",
            "run_in_background": True,
        },
        ctx,
    )
    settled = await service.wait(started.meta["jobId"], 15)
    assert settled is not None and settled.status == "completed"
    assert "access is denied" in settled.output
    assert "[sandbox: file access denied under workspace-write mode]" in settled.output
    assert "[sandbox: escalation available" in settled.output


async def test_background_removes_the_private_temp_root(workspace, tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    temp_root = tmp_path / "background-sandbox-temp"
    temp_root.mkdir()
    sandbox = ConfiningSandbox(temp_root=temp_root)
    ctx = make_context(workspace, services={"jobs": service, "sandbox": sandbox})
    started = await PwshTool().run(
        {"description": "Background temp", "command": "Write-Output 1", "run_in_background": True},
        ctx,
    )
    settled = await service.wait(started.meta["jobId"], 15)
    assert settled is not None and settled.status == "completed"
    assert not temp_root.exists()


async def test_foreground_pwsh_still_returns_inline_output(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = jobs_context(workspace, service)
    result = await PwshTool().run(
        {"description": "Say hello", "command": "Write-Output 'foreground-line'"},
        ctx,
    )
    assert result.is_error is False
    assert result.text == "foreground-line"
    assert result.meta["exitCode"] == 0
    assert service.list() == []


async def test_background_command_still_checks_workdir(workspace) -> None:  # type: ignore[no-untyped-def]
    service = JobsService()
    ctx = jobs_context(workspace, service)
    with pytest.raises(ToolError, match="workdir is not a directory"):
        await PwshTool().run(
            {
                "description": "Background in a missing directory",
                "command": "Write-Output 1",
                "run_in_background": True,
                "workdir": str(workspace / "missing"),
            },
            ctx,
        )
