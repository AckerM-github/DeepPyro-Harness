"""pwsh/bash: real execution, exit codes, timeouts, background rejection."""

from __future__ import annotations

import pytest

from deeppyro.core.errors import PermissionDenied, ToolError
from deeppyro.sandbox import PermissionGate
from deeppyro.tools import BashTool, PwshTool
from tests.tools.helpers import make_context


def test_shell_schemas_require_description_first() -> None:
    pwsh = PwshTool()
    assert pwsh.parallel_safe is False
    assert pwsh.parameters["required"] == ["description", "command"]
    assert "sandbox_permissions" in pwsh.parameters["properties"]
    bash = BashTool()
    assert bash.parallel_safe is False
    assert bash.parameters["required"] == ["command"]


async def test_pwsh_runs_command(workspace) -> None:  # type: ignore[no-untyped-def]
    result = await PwshTool().run(
        {"description": "Say hello", "command": "Write-Output 'hello-tool'"},
        make_context(workspace),
    )
    assert result.is_error is False
    assert result.text == "hello-tool"
    assert result.meta["exitCode"] == 0
    assert result.meta["workdir"] == str(workspace)


async def test_pwsh_reports_nonzero_exit_without_erroring(workspace) -> None:  # type: ignore[no-untyped-def]
    result = await PwshTool().run(
        {"description": "Exit with three", "command": "Write-Output 'before'; exit 3"},
        make_context(workspace),
    )
    assert result.is_error is False
    assert result.meta["exitCode"] == 3
    assert result.text == "before\n\n[exit code: 3]"


async def test_pwsh_honours_workdir(workspace) -> None:  # type: ignore[no-untyped-def]
    workdir = workspace / "sub"
    workdir.mkdir()
    result = await PwshTool().run(
        {
            "description": "Print the working directory",
            "command": "(Get-Location).Path",
            "workdir": str(workdir),
        },
        make_context(workspace),
    )
    assert result.is_error is False
    assert result.text.lower() == str(workdir).lower()


async def test_pwsh_requires_description(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError, match="description is required"):
        await PwshTool().run({"command": "Write-Output 1"}, make_context(workspace))


async def test_pwsh_background_delegates_to_jobs_service(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError, match="requires the jobs service"):
        await PwshTool().run(
            {"description": "Background", "command": "Write-Output 1", "run_in_background": True},
            make_context(workspace),
        )


async def test_pwsh_timeout_kills_command(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError, match="timed out"):
        await PwshTool().run(
            {"description": "Sleep too long", "command": "Start-Sleep -Seconds 5", "timeoutMs": 250},
            make_context(workspace),
        )


async def test_pwsh_is_denied_in_read_only_mode(workspace) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("read-only", "never", workspace)
    with pytest.raises(PermissionDenied, match="read-only"):
        await PwshTool().run(
            {"description": "Write output", "command": "Write-Output 'x'"},
            make_context(workspace, permission=gate),
        )


async def test_pwsh_escalation_requires_justification(workspace) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("workspace-write", "always", workspace)
    ctx = make_context(workspace, permission=gate)
    with pytest.raises(ToolError, match="justification is required"):
        await PwshTool().run(
            {
                "description": "Escalate",
                "command": "Write-Output 1",
                "sandbox_permissions": "danger-full-access",
            },
            ctx,
        )


async def test_bash_runs_command_when_available(workspace) -> None:  # type: ignore[no-untyped-def]
    try:
        result = await BashTool().run({"command": "echo bash-tool"}, make_context(workspace))
    except ToolError as exc:
        pytest.skip(f"bash is not available on this machine: {exc}")
    if result.meta is not None and result.meta.get("exitCode") not in (0, None):
        pytest.skip(f"bash is not usable on this machine: {result.text}")
    assert result.is_error is False
    assert "bash-tool" in result.text
