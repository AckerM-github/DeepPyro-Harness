"""pwsh/bash: real execution, exit codes, timeouts, background rejection."""

from __future__ import annotations

import pytest

from deeppyro.core.errors import PermissionDenied, ToolError
from deeppyro.sandbox import ApprovalRequest, PermissionGate
from deeppyro.sandbox.provider import RunnerFailureRule
from deeppyro.tools import BashTool, PwshTool
from tests.tools.helpers import ConfiningSandbox, make_context


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


async def test_pwsh_runs_in_read_only_mode_through_the_runner(workspace) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("read-only", "never", workspace)
    sandbox = ConfiningSandbox()
    result = await PwshTool().run(
        {"description": "Write output", "command": "Write-Output 'x'"},
        make_context(workspace, permission=gate, services={"sandbox": sandbox}),
    )
    assert result.is_error is False
    assert result.text == "x"
    assert result.meta["sandbox"] == {"mode": "read-only", "enforcement": "partial", "denied": False}
    assert [policy.mode for policy in sandbox.policies] == ["read-only"]


async def test_pwsh_marks_a_sandbox_denial_and_offers_escalation(workspace) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("workspace-write", "never", workspace)
    sandbox = ConfiningSandbox(denial_signatures=("access is denied",))
    result = await PwshTool().run(
        {"description": "Denied write", "command": "Write-Output 'access is denied'"},
        make_context(workspace, permission=gate, services={"sandbox": sandbox}),
    )
    assert result.is_error is False
    assert "[sandbox: file access denied under workspace-write mode]" in result.text
    assert "[sandbox: escalation available" in result.text
    assert result.meta["sandbox"]["denied"] is True


async def test_pwsh_reports_a_runner_failure_as_a_sandbox_problem(workspace) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("workspace-write", "never", workspace)
    sandbox = ConfiningSandbox(
        runner_failure_rules=(RunnerFailureRule((127,), ("windows-acl-run: ",)),),
    )
    result = await PwshTool().run(
        {
            "description": "Broken runner",
            "command": "Write-Output 'windows-acl-run: token'; exit 127",
        },
        make_context(workspace, permission=gate, services={"sandbox": sandbox}),
    )
    assert result.is_error is False
    assert "the sandbox runner itself failed under workspace-write mode" in result.text
    assert result.meta["sandbox"]["runnerFailed"] is True
    assert result.meta["sandbox"]["denied"] is False


async def test_pwsh_removes_the_private_temp_root_after_the_run(workspace, tmp_path) -> None:  # type: ignore[no-untyped-def]
    temp_root = tmp_path / "sandbox-temp"
    temp_root.mkdir()
    sandbox = ConfiningSandbox(temp_root=temp_root)
    await PwshTool().run(
        {"description": "Clean temp", "command": "Write-Output 'x'"},
        make_context(workspace, services={"sandbox": sandbox}),
    )
    assert not temp_root.exists()


async def test_pwsh_escalation_requires_justification_and_a_wider_target(workspace) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("workspace-write", "never", workspace)
    ctx = make_context(workspace, permission=gate)
    with pytest.raises(ToolError, match="requires a justification"):
        await PwshTool().run(
            {
                "description": "Escalate",
                "command": "Write-Output 1",
                "sandbox_permissions": "danger-full-access",
            },
            ctx,
        )
    danger_ctx = make_context(workspace, permission=PermissionGate("danger-full-access", "never", workspace))
    with pytest.raises(ToolError, match="not strictly wider"):
        await PwshTool().run(
            {
                "description": "Escalate",
                "command": "Write-Output 1",
                "sandbox_permissions": "workspace-write",
                "justification": "x",
            },
            danger_ctx,
        )
    with pytest.raises(PermissionDenied, match="rejected escalating this command"):
        await PwshTool().run(
            {
                "description": "Escalate",
                "command": "Write-Output 1",
                "sandbox_permissions": "danger-full-access",
                "justification": "need full access",
            },
            ctx,
        )


async def test_pwsh_command_approval_request_carries_the_tool_name(workspace) -> None:  # type: ignore[no-untyped-def]
    asked: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        asked.append(req)
        return True

    gate = PermissionGate("workspace-write", "ask", workspace, ask=grant)
    result = await PwshTool().run(
        {
            "description": "Escalate",
            "command": "Write-Output 1",
            "sandbox_permissions": "danger-full-access",
            "justification": "need one-shot escalation",
        },
        make_context(workspace, permission=gate),
    )
    assert result.is_error is False
    assert [req.kind for req in asked] == ["sandbox-escalation"]
    assert asked[0].tool == "pwsh"


async def test_bash_runs_command_when_available(workspace) -> None:  # type: ignore[no-untyped-def]
    try:
        result = await BashTool().run({"command": "echo bash-tool"}, make_context(workspace))
    except ToolError as exc:
        pytest.skip(f"bash is not available on this machine: {exc}")
    if result.meta is not None and result.meta.get("exitCode") not in (0, None):
        pytest.skip(f"bash is not usable on this machine: {result.text}")
    assert result.is_error is False
    assert "bash-tool" in result.text
