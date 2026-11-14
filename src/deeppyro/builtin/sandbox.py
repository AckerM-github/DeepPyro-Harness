"""The sandbox service: permission presets and the approval gate."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

from deeppyro.api import Service
from deeppyro.sandbox import PermissionGate


class SandboxPlugin(Service):
    """Builds one permission gate per workspace."""

    provide = "sandbox"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "sandbox"
    version = "0.1.0"
    description = "sandbox policy and approvals"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        cfg = ctx.service("config").value
        self._preset = cfg.agent.permissionPreset
        self._approval = cfg.agent.approvalPolicy
        self._workspaces: dict[str, PermissionGate] = {}

    def gate(
        self,
        workspace: str | Path,
        ask: Callable[[Any], Awaitable[bool] | bool] | None = None,
    ) -> PermissionGate:
        """Create a gate rooted at workspace (a fresh gate per call)."""
        # The hooks service loads after the sandbox, so resolve it lazily here
        # instead of injecting it (an absent service is fine: no hooks).
        return PermissionGate(
            self._preset,
            self._approval,
            Path(workspace),
            ask=ask,
            hooks=self.ctx.get("hooks"),
        )

    @property
    def preset(self) -> str:
        return self._preset

    @property
    def approval(self) -> str:
        return self._approval
