"""Sandbox, approval and permission-preset services: DSH policy semantics."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from deeppyro.core.errors import ConfigError, SessionError
from deeppyro.sandbox.approval import (
    APPROVAL_POLICY_EVENT,
    ApprovalService,
)
from deeppyro.sandbox.permission import (
    PERMISSION_PRESET_EVENT,
    PermissionPresetsService,
)
from deeppyro.sandbox.policy import (
    SANDBOX_MODE_EVENT,
    SandboxPolicyService,
    approval_policy_text,
    sandbox_policy_text,
    workspace_writable_roots,
    writable_roots,
)
from deeppyro.sandbox.types import (
    CUSTOM_PERMISSION_PRESET,
    DEFAULT_PERMISSION_PRESETS,
)


class _Record:
    def __init__(self, record_type: str, data: dict[str, object]) -> None:
        self.type = record_type
        self.data = data


class FakeSession:
    """Minimal record log with the append API the services need."""

    def __init__(self, cwd: Path | str = ".") -> None:
        self.records: list[_Record] = []
        self.header = SimpleNamespace(cwd=str(cwd))

    def append(self, record_type: str, data: dict[str, object]) -> None:
        self.records.append(_Record(record_type, data))

    def types(self) -> list[str]:
        return [record.type for record in self.records]


# --------------------------------------------------------------------------
# sandbox policy
# --------------------------------------------------------------------------


def test_policy_resolves_the_workspace_from_the_session_cwd(tmp_path: Path) -> None:
    service = SandboxPolicyService(default_mode="workspace-write", workspace_root=tmp_path / "configured")
    session = FakeSession(tmp_path / "session-cwd")
    policy = service.resolve(session)
    assert policy.mode == "workspace-write"
    assert policy.workspace_root == (tmp_path / "session-cwd").resolve()
    assert service.workspace_root(None) == (tmp_path / "configured").resolve()


def test_policy_mode_switches_are_log_only_and_idempotent(tmp_path: Path) -> None:
    service = SandboxPolicyService(default_mode="workspace-write", workspace_root=tmp_path)
    session = FakeSession()
    assert service.last_mode(session) is None
    assert service.set_mode(session, "read-only") == "read-only"
    assert service.set_mode(session, "read-only") == "read-only"
    assert session.types() == [SANDBOX_MODE_EVENT]
    assert session.records[0].data == {"mode": "read-only"}
    assert service.last_mode(session) == "read-only"
    assert service.resolve(session).mode == "read-only"


def test_policy_explicit_mode_wins_and_invalid_values_are_config_errors(tmp_path: Path) -> None:
    service = SandboxPolicyService(default_mode="workspace-write", workspace_root=tmp_path)
    assert service.resolve(None, "danger-full-access").mode == "danger-full-access"
    with pytest.raises(ConfigError):
        service.set_mode(FakeSession(), "yolo")


def test_policy_rejects_corrupt_records(tmp_path: Path) -> None:
    service = SandboxPolicyService(default_mode="workspace-write", workspace_root=tmp_path)
    session = FakeSession()
    session.append(SANDBOX_MODE_EVENT, {"mode": "yolo"})
    with pytest.raises(SessionError, match="invalid sandbox/mode record"):
        service.last_mode(session)


def test_writable_roots_include_the_workspace_additional_roots_and_temporary_areas(tmp_path: Path) -> None:
    extra = tmp_path / "extra"
    policy = SandboxPolicyService(
        default_mode="workspace-write",
        workspace_root=tmp_path / "workspace",
        additional_writable_roots=(extra,),
    ).resolve(None)
    workspace_roots = workspace_writable_roots(policy)
    assert workspace_roots == ((tmp_path / "workspace").resolve(), extra.resolve())
    roots = writable_roots(policy)
    assert (tmp_path / "workspace").resolve() in roots
    assert extra.resolve() in roots
    assert Path(tempfile.gettempdir()).resolve() in roots
    assert (
        writable_roots(SandboxPolicyService(default_mode="read-only", workspace_root=tmp_path).resolve(None))
        == ()
    )


def test_sandbox_and_approval_policy_text_matches_dsh(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    read_only = SandboxPolicyService(default_mode="read-only", workspace_root=workspace).resolve(None)
    assert sandbox_policy_text(read_only).startswith("Current DSH file policy: read-only.")
    danger = SandboxPolicyService(default_mode="danger-full-access", workspace_root=workspace).resolve(None)
    assert "does not restrict file modifications" in sandbox_policy_text(danger)
    write = SandboxPolicyService(default_mode="workspace-write", workspace_root=workspace).resolve(None)
    text = sandbox_policy_text(write)
    assert f'"{workspace.resolve()}"' in text
    assert "Some platform temporary areas may also be writable." in text
    assert "Approval prompts are enabled" in approval_policy_text("ask")
    assert "Approval prompts are disabled" in approval_policy_text("never")


# --------------------------------------------------------------------------
# approval service
# --------------------------------------------------------------------------


def test_approval_policy_switches_are_log_only_and_idempotent(tmp_path: Path) -> None:
    service = ApprovalService(policy="ask")
    session = FakeSession()
    assert service.last_policy(session) is None
    assert service.effective_policy(session) == "ask"
    assert service.set_policy(session, "never") == "never"
    assert service.set_policy(session, "never") == "never"
    assert session.types() == [APPROVAL_POLICY_EVENT]
    assert session.records[0].data == {"policy": "never"}
    assert service.effective_policy(session) == "never"
    with pytest.raises(ConfigError):
        service.set_policy(session, "always")


async def test_approval_never_policy_rejects_before_any_waterfall() -> None:
    service = ApprovalService(policy="ask")
    session = FakeSession()
    service.set_policy(session, "never")
    outcome = await service.request(_request(), session=session)
    assert outcome == "rejected"


async def test_approval_answerer_receives_the_request_once() -> None:
    seen: list[object] = []

    async def answerer(request: object) -> bool:
        seen.append(request)
        return True

    service = ApprovalService(policy="ask", answerer=answerer)
    request = _request()
    outcome = await service.request(request)
    assert outcome == "allowed-once"
    assert seen == [request]


async def test_approval_answerer_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(request: object) -> bool:
        raise RuntimeError("ui crashed")

    service = ApprovalService(policy="ask", answerer=broken)
    assert await service.request(_request()) == "unavailable"


def _request():
    from deeppyro.sandbox.types import ApprovalRequest

    return ApprovalRequest(kind="command", summary="run it")


# --------------------------------------------------------------------------
# permission presets
# --------------------------------------------------------------------------


def _permission_service(tmp_path: Path) -> tuple[PermissionPresetsService, FakeSession]:
    policy = SandboxPolicyService(default_mode="workspace-write", workspace_root=tmp_path)
    approval = ApprovalService(policy="ask")
    service = PermissionPresetsService(
        default="workspace-write",
        policy_service=policy,
        approval_service=approval,
    )
    return service, FakeSession(tmp_path)


def test_permission_presets_expose_the_dsh_catalog() -> None:
    service = PermissionPresetsService()
    assert service.names() == ("read-only", "workspace-write", "danger-full-access")
    assert [spec.name for spec in service.catalog()] == list(service.names())
    assert service.default == "workspace-write"
    assert DEFAULT_PERMISSION_PRESETS[2].sandbox == "danger-full-access"
    assert DEFAULT_PERMISSION_PRESETS[2].approval == "never"
    with pytest.raises(ConfigError, match='unknown preset "yolo"'):
        service.get("yolo")


def test_permission_apply_writes_only_changed_knobs(tmp_path: Path) -> None:
    service, session = _permission_service(tmp_path)
    assert service.current(session) == CUSTOM_PERMISSION_PRESET
    assert service.pin_initial(session) == "workspace-write"
    assert service.current(session) == "workspace-write"
    assert service.apply(session, "read-only") == "read-only"
    assert session.types()[3:] == [PERMISSION_PRESET_EVENT, SANDBOX_MODE_EVENT]
    assert session.records[3].data == {"preset": "read-only"}
    assert session.records[4].data == {"mode": "read-only"}
    assert service.current(session) == "read-only"
    service.apply(session, "workspace-write")
    assert session.types()[5:] == [PERMISSION_PRESET_EVENT, SANDBOX_MODE_EVENT]
    assert service.current(session) == "workspace-write"
    before = len(session.records)
    service.apply(session, "workspace-write")
    assert len(session.records) == before


def test_permission_derives_custom_from_unmatched_knobs(tmp_path: Path) -> None:
    service, session = _permission_service(tmp_path)
    service.pin_initial(session)
    policy_service = service._policy_service
    approval_service = service._approval_service
    assert policy_service is not None and approval_service is not None
    policy_service.set_mode(session, "danger-full-access")
    approval_service.set_policy(session, "ask")
    assert service.current(session) == CUSTOM_PERMISSION_PRESET
    assert service.is_custom(session) is True
    with pytest.raises(ConfigError, match="cannot be selected"):
        service.apply(session, CUSTOM_PERMISSION_PRESET)


def test_permission_pin_initial_seeds_a_fresh_session_once(tmp_path: Path) -> None:
    service, session = _permission_service(tmp_path)
    assert service.pin_initial(session) == "workspace-write"
    assert session.types() == [
        SANDBOX_MODE_EVENT,
        APPROVAL_POLICY_EVENT,
        PERMISSION_PRESET_EVENT,
    ]
    assert service.pin_initial(session) == "workspace-write"
    assert len(session.records) == 3


def test_permission_validates_the_default_and_specs() -> None:
    with pytest.raises(ConfigError, match="unknown default permission preset"):
        PermissionPresetsService(default="yolo")
    from deeppyro.sandbox.types import PermissionPresetSpec

    with pytest.raises(ConfigError, match="reserved"):
        PermissionPresetsService([PermissionPresetSpec("custom", "read-only", "ask")])
    with pytest.raises(ConfigError, match="invalid sandbox mode"):
        PermissionPresetsService([PermissionPresetSpec("x", "yolo", "ask")])  # type: ignore[arg-type]


def test_permission_unknown_apply_message_lists_available(tmp_path: Path) -> None:
    service, session = _permission_service(tmp_path)
    with pytest.raises(ConfigError, match="available: read-only, workspace-write, danger-full-access"):
        service.apply(session, "yolo")
