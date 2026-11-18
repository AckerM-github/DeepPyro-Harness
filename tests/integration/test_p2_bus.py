"""Integration checks for the remaining in-tree hook consumers.

The hook journal and the hook-based retry policy were removed with the v4
runtime, so this module exercises what is still mounted: prompt-extension
appends to the assembled system prompt and approval-policy answers the
permission gate from [approval].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.builtin.approval_policy import ApprovalPolicyPlugin
from deeppyro.builtin.prompt_extension import PromptExtensionPlugin
from deeppyro.hooks import HookService
from deeppyro.sandbox import ApprovalRequest, PermissionGate
from tests.service_doubles import service_lookup, service_require


class FakeCtx:
    """Minimal PluginContext with service lookup, effect bookkeeping and emit."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.effects: list[str] = []
        self.disposers: list[Any] = []
        self.emitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered")
        return self._services[name]

    def get(self, service: str | type[Any]) -> Any | None:
        return service_lookup(self._services, service)

    def require(self, service: type[Any]) -> Any:
        return service_require(self._services, service)

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        # Production semantics: effect() only records a teardown callback; the
        # callback runs at dispose time, never during setup.
        self.effects.append(label)
        self.disposers.append(execute)
        return execute

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.emitted.append((args, kwargs))


class FakeConfigService:
    """Minimal config service exposing a plain mapping as .value."""

    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


def _prompt_extension(hooks: HookService, extension: str) -> tuple[Any, FakeCtx]:
    ctx = FakeCtx({"hooks": hooks, "config": FakeConfigService({"prompt": {"extension": extension}})})
    plugin = PromptExtensionPlugin(ctx, None)
    plugin.setup()
    return plugin, ctx


def _approval_policy(hooks: HookService, **section: Any) -> tuple[Any, FakeCtx]:
    ctx = FakeCtx({"hooks": hooks, "config": FakeConfigService({"approval": section})})
    plugin = ApprovalPolicyPlugin(ctx, None)
    plugin.setup()
    return plugin, ctx


async def test_approval_policy_plugin_answers_the_gate(tmp_path: Path) -> None:
    """[approval].hookMode decides matching requests before the interactive policy."""
    hooks = HookService()
    _plugin, _ctx = _approval_policy(hooks, hookMode="deny", hookTools=["shell"])
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)

    denied = await gate.request(ApprovalRequest(kind="command", summary="run", tool="shell"))
    assert denied.allowed is False
    assert denied.reason == "approval was rejected"

    unmatched = await gate.request(ApprovalRequest(kind="command", summary="run", tool="read"))
    assert unmatched.allowed is False
    assert unmatched.reason == "no interactive approval handler is available"


async def test_approval_policy_can_allow_without_prompting(tmp_path: Path) -> None:
    """hookMode=allow approves matching tools through the hook, not the ask path."""
    hooks = HookService()
    _plugin, _ctx = _approval_policy(hooks, hookMode="allow")
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)

    allowed = await gate.request(ApprovalRequest(kind="command", summary="run", tool="shell"))
    assert allowed.allowed is True
    assert allowed.reason == "approved for this call"
