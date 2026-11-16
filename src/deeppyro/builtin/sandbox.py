"""The sandbox services: policy, approval, permission presets, and provider.

The plugin exposes four collaborating services:

* sandbox          - the provider (confine) plus the gate factory tools use
* sandboxPolicy    - resolves the standing sandbox mode and writable roots
* approval         - ask/never policy, hook waterfall, interactive answerers
* permissionPresets- named (sandbox, approval) bundles for /permission
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.api.services import lookup_service
from deeppyro.core.payload import field_of
from deeppyro.hooks.service import HookService
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.sandbox import PermissionGate
from deeppyro.sandbox.approval import ApprovalService
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.sandbox.policy import SandboxPolicy, SandboxPolicyService
from deeppyro.sandbox.provider import Confinement, LocalSandboxProvider
from deeppyro.sandbox.types import DEFAULT_PERMISSION_PRESET


class SandboxPlugin(Plugin):
    """Build the DSH-aligned sandbox services and one gate per workspace."""

    provide = "sandbox"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "sandbox"
    version = "0.1.0"
    description = "sandbox policy, execution provider, and approvals"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        cfg = ctx.require(KernelConfig).value
        sandbox = _table(cfg, "sandbox")
        approval = _table(cfg, "approval")
        permission = _table(cfg, "permission")
        self.policy_service = SandboxPolicyService(
            default_mode=_str(sandbox, "mode", "workspace-write"),
            workspace_root=_optional_str(sandbox, "workspaceRoot", "workspace_root"),
            additional_writable_roots=_roots(sandbox),
        )
        self.approval_service = ApprovalService(policy=_str(approval, "policy", "ask"))
        default_preset = _str(permission, "defaultPreset", DEFAULT_PERMISSION_PRESET, camel=False)
        self.permission_presets = PermissionPresetsService(
            default=default_preset,
            policy_service=self.policy_service,
            approval_service=self.approval_service,
        )
        self.provider = LocalSandboxProvider()

    def setup(self) -> None:
        self.ctx.provide("sandbox", self)
        self.ctx.provide("sandboxPolicy", self.policy_service)
        self.ctx.provide("approval", self.approval_service)
        self.ctx.provide("permissionPresets", self.permission_presets)

    def gate(
        self,
        workspace: str | Path,
        ask: Callable[[Any], Awaitable[bool] | bool] | None = None,
        *,
        session: Any | None = None,
    ) -> PermissionGate:
        """Create one gate rooted at workspace."""
        # hooks loads after sandbox, so resolve it lazily here.
        gate = PermissionGate(
            self.policy_service.default_mode,
            self.approval_service.policy,
            Path(workspace),
            ask=ask,
            policy_service=self.policy_service,
            approval_service=self.approval_service,
            permission_presets=self.permission_presets,
            session=session,
        )
        hooks = lookup_service(self.ctx, HookService)
        if hooks is not None:
            gate.bind_hooks(hooks)
        if ask is not None:
            gate.bind_answerer(ask)
        return gate

    def confine(
        self,
        argv: list[str] | tuple[str, ...],
        policy: SandboxPolicy,
        *,
        signal: Any | None = None,
    ) -> Confinement:
        return self.provider.confine(argv, policy, signal=signal)

    @property
    def preset(self) -> str:
        return self.policy_service.default_mode

    @property
    def approval(self) -> str:
        return self.approval_service.policy


def _table(cfg: Any, name: str) -> Mapping[str, Any]:
    value = field_of(cfg, name)
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, Mapping):
            return data
    return {}


def _first(table: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in table:
            return table[name]
    return None


def _str(table: Mapping[str, Any], name: str, default: str, *, camel: bool = True) -> str:
    names = (name, _snake(name)) if camel else (name, _camel(name))
    raw = _first(table, *names)
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"[{name}] must be a non-empty string")
    return raw


def _optional_str(table: Mapping[str, Any], *names: str) -> str | None:
    raw = _first(table, *names)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError("workspaceRoot must be a non-empty string")
    return raw


def _roots(table: Mapping[str, Any]) -> tuple[str, ...]:
    raw = _first(table, "additionalWritableRoots", "additional_writable_roots")
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ValueError("additionalWritableRoots must be a list of paths")
    roots: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ValueError("additionalWritableRoots entries must be non-empty strings")
        roots.append(item)
    return tuple(roots)


def _snake(name: str) -> str:
    out: list[str] = []
    for char in name:
        if char.isupper():
            out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out)


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


__all__ = ["SandboxPlugin"]
