"""PermissionGate: DSH sandbox modes, writable roots and one-shot approvals."""

from __future__ import annotations

import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from deeppyro.core.errors import ConfigError
from deeppyro.hooks import APPROVAL_REQUEST, HookService
from deeppyro.sandbox import ApprovalRequest, PermissionGate


class RecordSession:
    """Minimal session that captures approval audit records."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def append(self, event: str, payload: dict[str, object]) -> None:
        self.records.append((event, dict(payload)))


class BrokenSession:
    """Session whose audit write fails; the decision must not change."""

    def append(self, event: str, payload: dict[str, object]) -> None:
        raise RuntimeError("session log is read-only")


def outside_path() -> Path:
    """A path outside both the workspace and the platform temporary areas."""
    return Path("C:/deeppyro-test-outside") if sys.platform == "win32" else Path("/deeppyro-test-outside")


def test_invalid_preset_and_approval_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        PermissionGate("yolo", "ask", tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        PermissionGate("read-only", "sometimes", tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        PermissionGate("read-only", "always", tmp_path)  # type: ignore[arg-type]


def test_check_read_is_allowed_in_every_preset(tmp_path: Path) -> None:
    for preset in ("read-only", "workspace-write", "danger-full-access"):
        gate = PermissionGate(preset, "never", tmp_path)
        assert gate.check_read(tmp_path / "secret.txt").allowed is True


def test_read_only_denies_every_write_but_allows_commands(tmp_path: Path) -> None:
    gate = PermissionGate("read-only", "never", tmp_path)
    write = gate.check_write(tmp_path / "a.txt")
    assert write.allowed is False and write.needs_approval is False
    assert "read-only" in write.reason
    assert gate.check_command("echo hi").allowed is True


def test_workspace_write_allows_the_workspace_and_temporary_areas(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = PermissionGate("workspace-write", "never", workspace)
    assert gate.check_write(workspace / "inside.txt").allowed is True
    assert gate.check_write(tmp_path / "scratch.tmp").allowed is True


def test_workspace_write_denies_outside_paths_without_prompting(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = PermissionGate("workspace-write", "never", workspace)
    outside = gate.check_write(outside_path())
    assert outside.allowed is False and outside.needs_approval is False
    assert "file access denied under workspace-write mode" in outside.reason


def test_workspace_boundary_resolves_dotdot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = outside_path()
    gate = PermissionGate("workspace-write", "never", workspace)
    assert gate.check_write(workspace / "nested" / ".." / "inside.txt").allowed is True
    assert gate.check_write(outside / ".." / outside.name).allowed is False
    assert PermissionGate("danger-full-access", "never", workspace).check_write(outside).allowed is True


def test_danger_full_access_allows_writes_in_every_location(tmp_path: Path) -> None:
    gate = PermissionGate("danger-full-access", "never", tmp_path)
    assert gate.check_write(outside_path()).allowed is True
    assert gate.check_command("rm -rf /").allowed is True


def test_validate_escalation_only_moves_wider(tmp_path: Path) -> None:
    gate = PermissionGate("workspace-write", "ask", tmp_path)
    gate.validate_escalation("danger-full-access")
    gate.validate_escalation("workspace-write")  # the current mode is a no-op
    with pytest.raises(ConfigError, match="not strictly wider"):
        gate.validate_escalation("read-only")
    with pytest.raises(ConfigError):
        gate.validate_escalation("bogus")
    read_only = PermissionGate("read-only", "ask", tmp_path)
    read_only.validate_escalation("workspace-write")
    read_only.validate_escalation("danger-full-access")
    with pytest.raises(ConfigError, match="not strictly wider"):
        PermissionGate("danger-full-access", "ask", tmp_path).validate_escalation("workspace-write")


async def test_request_ask_policy_uses_the_answerer(tmp_path: Path) -> None:
    asked: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        asked.append(req)
        return True

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=grant)
    req = ApprovalRequest(kind="command", summary="run it", detail="echo hi")
    decision = await gate.request(req)
    assert decision.allowed is True
    assert decision.reason == "approved for this call"
    assert asked == [req]
    assert gate.approvals == [(req, decision)]


async def test_request_answerer_denial_maps_to_rejected(tmp_path: Path) -> None:
    async def deny(req: ApprovalRequest) -> bool:
        return False

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=deny)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert decision.reason == "approval was rejected"


async def test_request_never_rejects_before_the_waterfall(tmp_path: Path) -> None:
    hooks = HookService()
    called: list[dict[str, object]] = []

    async def allow(payload: dict[str, object], next_step) -> str:  # type: ignore[no-untyped-def]
        called.append(dict(payload))
        return "allow"

    hooks.on(APPROVAL_REQUEST, allow)
    session = RecordSession()
    gate = PermissionGate("workspace-write", "never", tmp_path, hooks=hooks, session=session)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert decision.reason == "approval was rejected"
    assert called == []
    assert session.records == []


async def test_request_without_an_answerer_fails_closed(tmp_path: Path) -> None:
    gate = PermissionGate("workspace-write", "ask", tmp_path)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert decision.reason == "no interactive approval handler is available"


async def test_request_answerer_exception_fails_closed(tmp_path: Path) -> None:
    async def broken(req: ApprovalRequest) -> bool:
        raise RuntimeError("ui crashed")

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=broken)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert decision.reason == "no interactive approval handler is available"


async def test_request_cancelled_signal_preempts_the_waterfall(tmp_path: Path) -> None:
    hooks = HookService()
    called: list[object] = []

    async def allow(payload: object, next_step) -> str:  # type: ignore[no-untyped-def]
        called.append(payload)
        return "allow"

    hooks.on(APPROVAL_REQUEST, allow)
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)

    class Signal:
        def is_set(self) -> bool:
            return True

    decision = await gate.request(ApprovalRequest(kind="x", summary="y"), signal=Signal())
    assert decision.allowed is False
    assert decision.reason == "approval was cancelled"
    assert called == []


# --------------------------------------------------------------------------
# approval hooks
# --------------------------------------------------------------------------


def _hook_service() -> HookService:
    return HookService()


async def test_approval_request_hook_allow_short_circuits_the_answerer(tmp_path: Path) -> None:
    hooks = _hook_service()
    seen: list[dict[str, object]] = []

    async def allow(payload: dict[str, object], next_step) -> str:  # type: ignore[no-untyped-def]
        seen.append(dict(payload))
        return "allow"

    hooks.on(APPROVAL_REQUEST, allow)
    session = RecordSession()
    called: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        called.append(req)
        return False

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=grant, hooks=hooks, session=session)
    req = ApprovalRequest(kind="command", summary="run it", tool="pwsh", call_id="call-1")
    decision = await gate.request(req)

    assert decision.allowed is True
    assert decision.reason == "approved for this call"
    assert called == []
    assert gate.approvals == [(req, decision)]
    payload = seen[0]
    assert payload["request"] == req
    assert payload["approval"] == {"id": session.records[0][1]["id"], "toolName": "pwsh", "callId": "call-1"}
    assert payload["policy"] == "ask"
    assert payload["agent"] is None
    assert payload["signal"] is None
    assert [name for name, _ in session.records] == ["approval/asked", "approval/decided"]
    assert session.records[0][1]["toolName"] == "pwsh"
    assert session.records[0][1]["callId"] == "call-1"
    assert session.records[0][1]["reason"] == "run it"
    assert session.records[1][1]["outcome"] == "allowed-once"


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("deny", "approval was rejected"),
        ("cancel", "approval was cancelled"),
        ("rejected", "approval was rejected"),
        ("cancelled", "approval was cancelled"),
        ("allowed-once", "approved for this call"),
    ],
)
async def test_approval_request_hook_outcomes(tmp_path: Path, outcome: str, reason: str) -> None:
    hooks = _hook_service()
    hooks.on(APPROVAL_REQUEST, lambda payload, next_step: outcome)
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.reason == reason
    assert decision.allowed is (reason == "approved for this call")


async def test_approval_request_hook_ask_falls_through_to_the_answerer(tmp_path: Path) -> None:
    hooks = _hook_service()

    async def relay(payload, next_step):  # type: ignore[no-untyped-def]
        return await next_step()

    hooks.on(APPROVAL_REQUEST, relay)
    session = RecordSession()
    requested: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        requested.append(req)
        return True

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=grant, hooks=hooks, session=session)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))

    assert decision.allowed is True
    assert requested == [ApprovalRequest(kind="x", summary="y")]
    assert [name for name, _ in session.records] == ["approval/asked", "approval/decided"]
    assert session.records[0][1]["toolName"] is None
    assert session.records[0][1]["reason"] == "y"
    assert session.records[1][1]["outcome"] == "allowed-once"


async def test_approval_request_unknown_hook_result_fails_closed(tmp_path: Path) -> None:
    hooks = _hook_service()
    hooks.on(APPROVAL_REQUEST, lambda payload, next_step: "maybe")
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert decision.reason == "no interactive approval handler is available"


async def test_approval_request_hook_errors_fail_closed(tmp_path: Path) -> None:
    hooks = _hook_service()

    async def explode(payload: object, next_step) -> str:  # type: ignore[no-untyped-def]
        raise RuntimeError("plugin crashed")

    hooks.on(APPROVAL_REQUEST, explode)
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is False
    assert decision.reason == "no interactive approval handler is available"


async def test_approval_hook_payload_is_frozen(tmp_path: Path) -> None:
    hooks = _hook_service()
    captured: list[object] = []

    def observe(payload: object, next_step) -> str:  # type: ignore[no-untyped-def]
        captured.append(payload)
        return "deny"

    hooks.on(APPROVAL_REQUEST, observe)
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)
    await gate.request(ApprovalRequest(kind="x", summary="y"))

    assert isinstance(captured[0], MappingProxyType)
    with pytest.raises(TypeError):
        captured[0]["agent"] = object()  # type: ignore[index]


async def test_audit_write_failure_does_not_change_the_decision(tmp_path: Path) -> None:
    gate = PermissionGate(
        "workspace-write", "ask", tmp_path, ask=lambda request: True, session=BrokenSession()
    )
    decision = await gate.request(ApprovalRequest(kind="x", summary="y"))
    assert decision.allowed is True
    assert decision.reason == "approved for this call"
