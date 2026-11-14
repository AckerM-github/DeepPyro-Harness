"""PermissionGate: presets, workspace boundary, dangerous commands, approvals."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from deeppyro.core.errors import ConfigError, SessionError
from deeppyro.hooks import (
    APPROVAL_ASKED,
    APPROVAL_DECIDED,
    APPROVAL_REQUEST,
    HookService,
)
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


# --------------------------------------------------------------------------
# approval hooks (task-63)
# --------------------------------------------------------------------------


def _hook_service() -> HookService:
    return HookService()


async def test_approval_request_hook_allow_short_circuits_policy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    hooks = _hook_service()
    seen: list[dict[str, object]] = []

    async def allow(payload: dict[str, object], next_step) -> str:  # type: ignore[no-untyped-def]
        seen.append(dict(payload))
        return "allow"

    hooks.on(APPROVAL_REQUEST, allow)
    called: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        called.append(req)
        return False

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=grant, hooks=hooks)
    req = ApprovalRequest(kind="command", summary="run it")
    decision = await gate.request(req)

    assert decision.allowed is True
    assert decision.reason == "approved by approval plugin"
    assert called == []
    assert gate.approvals == [(req, decision)]
    assert seen[0]["preset"] == "workspace-write"
    assert seen[0]["policy"] == "ask"
    assert seen[0]["agent"] is None
    assert seen[0]["signal"] is None


@pytest.mark.parametrize(
    ("outcome", "allowed", "reason"),
    [
        ("deny", False, "denied by approval plugin"),
        ("cancel", False, "approval canceled"),
    ],
)
async def test_approval_request_hook_deny_and_cancel(tmp_path, outcome, allowed, reason) -> None:  # type: ignore[no-untyped-def]
    hooks = _hook_service()
    hooks.on(APPROVAL_REQUEST, lambda payload, next_step: outcome)
    gate = PermissionGate("workspace-write", "always", tmp_path, hooks=hooks)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is allowed
    assert decision.reason == reason


async def test_approval_request_hook_ask_falls_through_to_policy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    hooks = _hook_service()
    events: list[tuple[str, dict[str, object]]] = []

    async def relay(payload, next_step):  # type: ignore[no-untyped-def]
        return await next_step()

    hooks.on(APPROVAL_REQUEST, relay)
    hooks.on(APPROVAL_ASKED, lambda payload: events.append(("asked", dict(payload))))
    hooks.on(APPROVAL_DECIDED, lambda payload: events.append(("decided", dict(payload))))
    requested: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        requested.append(req)
        return True

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=grant, hooks=hooks)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))

    assert decision.allowed is True
    assert decision.reason == "approved by the user"
    assert requested == [ApprovalRequest(kind="x", summary="y")]
    assert [name for name, _ in events] == ["asked", "decided"]
    assert events[0][1]["policy"] == "ask"
    assert events[1][1]["via"] == "user"
    assert events[1][1]["allowed"] is True


async def test_approval_asked_is_not_emitted_for_never_or_always(tmp_path) -> None:  # type: ignore[no-untyped-def]
    hooks = _hook_service()
    events: list[tuple[str, dict[str, object]]] = []
    hooks.on(APPROVAL_ASKED, lambda payload: events.append(("asked", dict(payload))))
    hooks.on(APPROVAL_DECIDED, lambda payload: events.append(("decided", dict(payload))))

    never = PermissionGate("workspace-write", "never", tmp_path, hooks=hooks)
    always = PermissionGate("workspace-write", "always", tmp_path, hooks=hooks)
    await never.request(ApprovalRequest(kind="x", summary="y"))
    await always.request(ApprovalRequest(kind="x", summary="y"))

    assert [name for name, _ in events] == ["decided", "decided"]
    assert [event[1]["via"] for event in events] == ["policy", "policy"]


async def test_approval_request_invalid_outcome_is_a_session_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    hooks = _hook_service()
    hooks.on(APPROVAL_REQUEST, lambda payload, next_step: "maybe")
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)
    with pytest.raises(SessionError, match="expected one of"):
        await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert gate.approvals == []


async def test_approval_hook_payload_is_frozen(tmp_path) -> None:  # type: ignore[no-untyped-def]
    hooks = _hook_service()
    captured: list[object] = []

    def observe(payload, next_step):  # type: ignore[no-untyped-def]
        captured.append(payload)
        return "ask"

    hooks.on(APPROVAL_REQUEST, observe)
    gate = PermissionGate("workspace-write", "never", tmp_path, hooks=hooks)
    await gate.request(ApprovalRequest(kind="x", summary="y"))

    assert isinstance(captured[0], MappingProxyType)
    with pytest.raises(TypeError):
        captured[0]["agent"] = object()  # type: ignore[index]


async def test_approval_decided_listener_failure_does_not_change_decision(tmp_path) -> None:  # type: ignore[no-untyped-def]
    hooks = _hook_service()

    def explode(payload) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("logger broke")

    hooks.on(APPROVAL_DECIDED, explode)
    gate = PermissionGate("workspace-write", "always", tmp_path, hooks=hooks)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is True


async def test_approval_without_matching_listener_keeps_policy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    hooks = _hook_service()
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert "no interactive approval handler" in decision.reason
