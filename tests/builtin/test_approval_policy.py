"""Tests for the builtin approval-policy consumer (task-68)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.builtin.approval_policy import ApprovalPolicyPlugin
from deeppyro.config import Config
from deeppyro.core.errors import ConfigError, PermissionDenied
from deeppyro.hooks import APPROVAL_REQUEST, HookService
from deeppyro.sandbox import ApprovalRequest, PermissionGate
from deeppyro.tools import WriteTool
from tests.tools.helpers import make_context


class FakeCtx:
    """Minimal PluginContext: service lookup and effect bookkeeping."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.effects: list[str] = []
        self.disposers: list[Any] = []

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered")
        return self._services[name]

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        dispose = execute()
        self.effects.append(label)
        self.disposers.append(dispose)
        return dispose


def make_config(approval: Any = None) -> Config:
    data: dict[str, Any] = {}
    if approval is not None:
        data["approval"] = approval
    return Config.model_validate(data)


def make_plugin(
    approval: Any = None, *, cfg: Config | None = None
) -> tuple[ApprovalPolicyPlugin, HookService, FakeCtx]:
    hooks = HookService()
    if cfg is None:
        cfg = make_config(approval)
    ctx = FakeCtx({"hooks": hooks, "config": SimpleNamespace(value=cfg)})
    plugin = ApprovalPolicyPlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, hooks, ctx


def make_request(tool: str | None = "write") -> ApprovalRequest:
    return ApprovalRequest(kind="write", summary="write a file", tool=tool)


async def dispatch(hooks: HookService, request: Any, *, fallback: Any = "ask") -> Any:
    payload = {
        "request": request,
        "preset": "workspace-write",
        "policy": "ask",
        "agent": None,
        "signal": None,
    }
    return await hooks.waterfall(APPROVAL_REQUEST, payload, fallback=fallback)


# -- configuration ---------------------------------------------------------
def test_plugin_metadata() -> None:
    assert ApprovalPolicyPlugin.plugin_id == "approval-policy"
    assert ApprovalPolicyPlugin.inject == ["hooks", "config"]


def test_missing_approval_table_defaults_to_off() -> None:
    plugin, _hooks, _ctx = make_plugin(cfg=make_config())
    assert plugin.mode == "off"
    assert plugin.tools == ()


@pytest.mark.parametrize("mode", ["off", "ask", "allow", "deny"])
def test_reads_every_supported_mode(mode: str) -> None:
    plugin, _hooks, _ctx = make_plugin({"hookMode": mode})
    assert plugin.mode == mode


def test_mode_is_normalized() -> None:
    plugin, _hooks, _ctx = make_plugin({"hookMode": "  ALLOW "})
    assert plugin.mode == "allow"


def test_invalid_mode_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="hookMode must be one of"):
        make_plugin({"hookMode": "sometimes"})


def test_non_string_mode_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="hookMode must be a string"):
        make_plugin({"hookMode": 1})


def test_hook_tools_are_read_as_a_tuple() -> None:
    plugin, _hooks, _ctx = make_plugin({"hookMode": "allow", "hookTools": ["write", "edit"]})
    assert plugin.tools == ("write", "edit")


def test_snake_case_hook_tools_alias_is_accepted() -> None:
    plugin, _hooks, _ctx = make_plugin({"hook_mode": "deny", "hook_tools": ["write"]})
    assert plugin.mode == "deny"
    assert plugin.tools == ("write",)


def test_string_hook_tools_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="hookTools must be a list"):
        make_plugin({"hookMode": "allow", "hookTools": "write"})


def test_non_string_hook_tool_entry_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="hookTools entries must be strings"):
        make_plugin({"hookMode": "allow", "hookTools": ["write", 3]})


# -- dispatch --------------------------------------------------------------
@pytest.mark.parametrize("mode", ["off", "ask"])
async def test_off_and_ask_pass_the_chain_through(mode: str) -> None:
    plugin, hooks, _ctx = make_plugin({"hookMode": mode})
    assert plugin.mode == mode

    assert await dispatch(hooks, make_request()) == "ask"


async def test_allow_short_circuits_the_chain() -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": "allow"})

    assert await dispatch(hooks, make_request()) == "allow"

    called: list[Any] = []

    def downstream(payload: dict[str, Any], next_step: Any) -> str:
        called.append(payload)
        return "deny"

    hooks.on(APPROVAL_REQUEST, downstream)
    assert await dispatch(hooks, make_request()) == "allow"
    assert called == []


async def test_deny_short_circuits_the_chain() -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": "deny"})

    assert await dispatch(hooks, make_request()) == "deny"


async def test_hook_tools_filter_limits_the_answer() -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": "allow", "hookTools": ["write"]})

    assert await dispatch(hooks, make_request("write")) == "allow"
    assert await dispatch(hooks, make_request("read")) == "ask"
    assert await dispatch(hooks, make_request(None)) == "ask"


async def test_empty_hook_tools_matches_every_request() -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": "allow", "hookTools": []})

    assert await dispatch(hooks, make_request("write")) == "allow"
    assert await dispatch(hooks, make_request(None)) == "allow"


async def test_missing_request_falls_back() -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": "allow", "hookTools": ["write"]})

    assert await dispatch(hooks, None) == "ask"


async def test_agent_none_payload_is_supported() -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": "deny", "hookTools": ["command"]})

    result = await dispatch(hooks, make_request("command"))

    assert result == "deny"


def test_disposer_removes_the_listener() -> None:
    _plugin, hooks, ctx = make_plugin({"hookMode": "allow"})
    assert hooks.has(APPROVAL_REQUEST) is True

    ctx.disposers[0]()

    assert hooks.has(APPROVAL_REQUEST) is False


async def test_disposed_plugin_no_longer_answers() -> None:
    _plugin, hooks, ctx = make_plugin({"hookMode": "allow"})
    ctx.disposers[0]()

    assert await dispatch(hooks, make_request()) == "ask"


# -- real gate path --------------------------------------------------------
async def test_gate_approves_matching_tool_without_prompting(tmp_path: Path) -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": "allow", "hookTools": ["write"]})
    asked: list[ApprovalRequest] = []

    async def ask(request: ApprovalRequest) -> bool:
        asked.append(request)
        return False

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=ask, hooks=hooks)
    decision = await gate.request(make_request("write"))

    assert decision.allowed is True
    assert decision.reason == "approved by approval plugin"
    assert asked == []
    assert gate.approvals[-1][1].allowed is True


async def test_gate_denies_matching_tool_without_prompting(tmp_path: Path) -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": "deny"})
    asked: list[ApprovalRequest] = []

    async def ask(request: ApprovalRequest) -> bool:
        asked.append(request)
        return True

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=ask, hooks=hooks)
    decision = await gate.request(make_request("write"))

    assert decision.allowed is False
    assert decision.reason == "denied by approval plugin"
    assert asked == []


@pytest.mark.parametrize("mode", ["off", "ask"])
async def test_gate_falls_back_to_the_user_for_inactive_modes(mode: str, tmp_path: Path) -> None:
    _plugin, hooks, _ctx = make_plugin({"hookMode": mode})
    asked: list[ApprovalRequest] = []

    async def ask(request: ApprovalRequest) -> bool:
        asked.append(request)
        return True

    gate = PermissionGate("workspace-write", "ask", tmp_path, ask=ask, hooks=hooks)
    decision = await gate.request(make_request("write"))

    assert decision.allowed is True
    assert decision.reason == "approved by the user"
    assert asked == [make_request("write")]


async def test_hook_denies_a_real_write_outside_the_workspace(tmp_path: Path) -> None:
    """The tool bridge populates ApprovalRequest.tool, so hookTools matches."""
    _plugin, hooks, _ctx = make_plugin({"hookMode": "deny", "hookTools": ["write"]})
    asked: list[ApprovalRequest] = []

    async def ask(request: ApprovalRequest) -> bool:
        asked.append(request)
        return True

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = PermissionGate("workspace-write", "ask", workspace, ask=ask, hooks=hooks)
    ctx = make_context(workspace, permission=gate)
    outside = tmp_path / "outside.txt"

    with pytest.raises(PermissionDenied, match="denied by approval plugin"):
        await WriteTool().run({"file_path": str(outside), "content": "no"}, ctx)

    assert asked == []
    assert not outside.exists()


async def test_hook_denies_a_real_write_sandbox_escalation(tmp_path: Path) -> None:
    """A one-shot sandbox escalation request also carries the tool name."""
    _plugin, hooks, _ctx = make_plugin({"hookMode": "deny", "hookTools": ["write"]})
    asked: list[ApprovalRequest] = []

    async def ask(request: ApprovalRequest) -> bool:
        asked.append(request)
        return True

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = PermissionGate("workspace-write", "ask", workspace, ask=ask, hooks=hooks)
    ctx = make_context(workspace, permission=gate)

    with pytest.raises(PermissionDenied, match="sandbox escalation denied: denied by approval plugin"):
        await WriteTool().run(
            {
                "file_path": "inside.txt",
                "content": "no",
                "sandbox_permissions": "danger-full-access",
                "justification": "need one-shot escalation",
            },
            ctx,
        )

    assert asked == []
    assert not (workspace / "inside.txt").exists()
