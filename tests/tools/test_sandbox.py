"""PermissionGate: presets, workspace boundary, dangerous commands, approvals."""

from __future__ import annotations

import pytest

from deeppyro.core.errors import ConfigError
from deeppyro.sandbox import ApprovalRequest, PermissionGate


def test_invalid_preset_and_approval_are_config_errors(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError):
        PermissionGate("yolo", "ask", tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        PermissionGate("read-only", "sometimes", tmp_path)  # type: ignore[arg-type]


def test_check_read_is_allowed_in_every_preset(tmp_path) -> None:  # type: ignore[no-untyped-def]
    for preset in ("read-only", "workspace-write", "danger-full-access"):
        gate = PermissionGate(preset, "never", tmp_path)
        assert gate.check_read(tmp_path / "secret.txt").allowed is True


def test_read_only_rejects_writes_and_commands(tmp_path) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("read-only", "never", tmp_path)
    write = gate.check_write(tmp_path / "a.txt")
    assert write.allowed is False and write.needs_approval is False
    assert "read-only" in write.reason
    command = gate.check_command("echo hi")
    assert command.allowed is False
    assert "read-only" in command.reason


def test_workspace_write_allows_inside_and_gates_outside(tmp_path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = PermissionGate("workspace-write", "never", workspace)
    assert gate.check_write(workspace / "inside.txt").allowed is True
    outside = gate.check_write(tmp_path / "outside.txt")
    assert outside.allowed is False
    # The never policy resolves the pending approval itself, so the decision
    # arrives fully decided rather than waiting for a handler.
    assert outside.needs_approval is False
    assert "outside the workspace" in outside.reason
    assert "approval policy is never" in outside.reason


def test_workspace_boundary_resolves_dotdot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = PermissionGate("workspace-write", "always", workspace)
    escape = workspace / ".." / "outside.txt"
    decision = gate.check_write(escape)
    assert decision.allowed is True  # approval policy always resolves the request
    never_gate = PermissionGate("workspace-write", "never", workspace)
    assert never_gate.check_write(escape).allowed is False
    assert PermissionGate("danger-full-access", "never", workspace).check_write(escape).allowed is True


def test_approval_always_widens_and_ask_marks_pending(tmp_path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    always = PermissionGate("workspace-write", "always", workspace)
    assert always.check_write(outside).allowed is True
    ask = PermissionGate("workspace-write", "ask", workspace)
    decision = ask.check_write(outside)
    assert decision.allowed is False and decision.needs_approval is True
    assert "approval policy" not in decision.reason


def test_dangerous_commands_require_approval_under_workspace_write(tmp_path) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("workspace-write", "never", tmp_path)
    refused = gate.check_command("Remove-Item -Recurse -Force C:\\temp")
    assert refused.allowed is False and refused.needs_approval is False
    assert "recursive force delete" in refused.reason
    assert "approval policy is never" in refused.reason
    assert gate.check_command("git status").allowed is True
    assert gate.check_command("echo hello").allowed is True
    asking = PermissionGate("workspace-write", "ask", tmp_path)
    assert asking.check_command("git push --force origin main").needs_approval is True
    danger = PermissionGate("danger-full-access", "never", tmp_path)
    assert danger.check_command("rm -rf /").allowed is True


async def test_request_ask_policy_uses_callback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    asked: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        asked.append(req)
        return True

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=grant)
    req = ApprovalRequest(kind="command", summary="run it", detail="echo hi")
    decision = await gate.request(req)
    assert decision.allowed is True
    assert asked == [req]
    assert gate.approvals == [(req, decision)]


async def test_request_never_and_always_do_not_call_handler(tmp_path) -> None:  # type: ignore[no-untyped-def]
    called: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        called.append(req)
        return True

    req = ApprovalRequest(kind="x", summary="y")
    never = PermissionGate("workspace-write", "never", tmp_path, ask=grant)
    assert (await never.request(req)).allowed is False
    assert "approval policy is never" in never.approvals[0][1].reason
    always = PermissionGate("workspace-write", "always", tmp_path, ask=grant)
    assert (await always.request(req)).allowed is True
    assert called == []


async def test_request_without_handler_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("workspace-write", "ask", tmp_path)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert "no interactive approval handler" in decision.reason


async def test_request_handler_exception_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def broken(req: ApprovalRequest) -> bool:
        raise RuntimeError("ui crashed")

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=broken)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert "approval handler failed" in decision.reason


def test_validate_escalation_only_moves_wider(tmp_path) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("workspace-write", "ask", tmp_path)
    gate.validate_escalation("danger-full-access")
    with pytest.raises(ConfigError):
        gate.validate_escalation("workspace-write")
    with pytest.raises(ConfigError):
        gate.validate_escalation("read-only")
    with pytest.raises(ConfigError):
        gate.validate_escalation("bogus")
    read_only = PermissionGate("read-only", "ask", tmp_path)
    read_only.validate_escalation("workspace-write")
