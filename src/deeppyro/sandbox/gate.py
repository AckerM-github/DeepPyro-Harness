"""Permission gate: the per-session sandbox and approval facade.

The gate keeps the small synchronous check API the tools already use
(check_read/check_write/check_command) but delegates policy to the DSH-aligned
services: SandboxPolicyService decides the standing mode and writable roots,
ApprovalService decides one-shot approvals. In DSH the filesystem consumer
(`dsh-fs-sandbox`) denies out-of-bound writes directly instead of prompting;
escalation is a separate, approval-gated retry driven by the tool layer.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deeppyro.core.errors import ConfigError
from deeppyro.sandbox.approval import ApprovalService
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.sandbox.policy import (
    SandboxPolicy,
    SandboxPolicyService,
    canonical_path,
    is_path_under,
    writable_roots,
)
from deeppyro.sandbox.provider import WIDER_MODES
from deeppyro.sandbox.types import (
    APPROVALS,
    PRESETS,
    Approval,
    ApprovalRequest,
    Decision,
    Preset,
)

logger = __import__("logging").getLogger(__name__)

DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive force delete", re.compile(r"\brm\s+-[a-z]*(?:rf|fr)[a-z]*\b", re.IGNORECASE)),
    (
        "recursive force delete",
        re.compile(r"\bRemove-Item\b[^\n]*(?:-Recurse\b|-r\b)[^\n]*(?:-Force\b|-fo\b)", re.IGNORECASE),
    ),
    ("recursive force delete", re.compile(r"\brmdir\s+/s\b", re.IGNORECASE)),
    ("disk format", re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE)),
    ("disk wipe", re.compile(r"\b(?:mkfs(?:\.\w+)?|diskpart|dd\s+if=)\b", re.IGNORECASE)),
    (
        "force push",
        re.compile(r"\bgit\s+push\b[^\n]*(?:--force-with-lease\b|--force\b|\s-f\b)", re.IGNORECASE),
    ),
    (
        "machine shutdown",
        re.compile(r"\b(?:shutdown|Restart-Computer|Stop-Computer|reboot)\b", re.IGNORECASE),
    ),
    ("registry deletion", re.compile(r"\breg\s+delete\b", re.IGNORECASE)),
    ("cipher wipe", re.compile(r"\bcipher\s+/w\b", re.IGNORECASE)),
)


class PermissionGate:
    """One session's current sandbox mode, approval policy and approvals."""

    def __init__(
        self,
        preset: Preset | str = "workspace-write",
        approval: Approval | str = "ask",
        workspace: str | Path | None = None,
        ask: Callable[[ApprovalRequest], Awaitable[bool] | bool] | None = None,
        *,
        hooks: Any | None = None,
        policy_service: SandboxPolicyService | None = None,
        approval_service: ApprovalService | None = None,
        permission_presets: PermissionPresetsService | None = None,
        session: Any | None = None,
    ) -> None:
        if preset not in PRESETS:
            raise ConfigError(f"invalid sandbox mode: {preset!r}")
        if approval not in APPROVALS:
            raise ConfigError(f"invalid approval policy: {approval!r}")
        self._preset: Preset = preset  # type: ignore[assignment]
        self._approval: Approval = approval  # type: ignore[assignment]
        self._workspace = canonical_path(workspace if workspace is not None else Path.cwd())
        self._policy_service = policy_service or SandboxPolicyService(
            default_mode=preset,
            workspace_root=self._workspace,
        )
        self._approval_service = approval_service or ApprovalService(
            policy=approval,
            answerer=ask,
            hooks=hooks,
        )
        self._permission_presets = permission_presets
        self._session = session
        self._hooks = hooks
        self.approvals: list[tuple[ApprovalRequest, Decision]] = []

    # -- binding -----------------------------------------------------------
    def bind_session(self, session: Any | None) -> None:
        self._session = session

    def bind_hooks(self, hooks: Any | None) -> None:
        self._hooks = hooks
        if hooks is not None:
            self._approval_service.bind_hooks(hooks)

    def bind_answerer(self, answerer: Callable[[ApprovalRequest], Awaitable[bool] | bool] | None) -> None:
        self._approval_service.bind_answerer(answerer)

    # -- introspection -----------------------------------------------------
    @property
    def preset(self) -> Preset:
        if self._permission_presets is not None and self._session is not None:
            current = self._permission_presets.current(self._session)
            if current != "custom":
                return current  # type: ignore[return-value]
            return self.policy.mode
        return self._preset

    @property
    def approval(self) -> Approval:
        return self._approval_service.effective_policy(self._session)

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy_service.resolve(self._session)

    @property
    def workspace(self) -> Path:
        return self._workspace

    def is_within_workspace(self, path: Path) -> bool:
        return is_path_under(path, self._workspace)

    # -- policy checks -----------------------------------------------------
    def check_read(self, path: Path) -> Decision:
        """Reads are never restricted by the sandbox."""
        return Decision(True)

    def check_write(self, path: Path) -> Decision:
        """Deny writes outside the current policy's writable roots."""
        policy = self.policy
        target = canonical_path(path)
        if policy.mode == "danger-full-access":
            return Decision(True)
        if policy.mode == "read-only":
            return Decision(False, reason=f'cannot write "{target}": file access denied under read-only mode')
        roots = writable_roots(policy)
        if any(is_path_under(target, root) for root in roots):
            return Decision(True)
        return Decision(
            False,
            reason=f'cannot write "{target}": file access denied under workspace-write mode',
        )

    def check_command(self, command: str) -> Decision:
        """Commands are allowed here; the OS sandbox confines them at spawn."""
        return Decision(True)

    def validate_escalation(self, target: str) -> None:
        """Validate a one-shot sandbox_permissions target against the current mode."""
        if target not in PRESETS:
            raise ConfigError(f"invalid sandbox mode: {target!r}")
        current = self.policy.mode
        if target == current:
            return
        if target not in WIDER_MODES.get(current, ()):
            raise ConfigError(
                f'sandbox escalation to "{target}" is not strictly wider than this call\'s '
                f'current "{current}" mode'
            )

    # -- approvals ---------------------------------------------------------
    async def request(
        self,
        req: ApprovalRequest,
        *,
        signal: Any | None = None,
        agent: Any | None = None,
    ) -> Decision:
        """Resolve one approval request through the DSH approval service."""
        outcome = await self._approval_service.request(
            req,
            session=self._session,
            agent=agent,
            signal=signal,
        )
        decision = _decision_for(outcome)
        self.approvals.append((req, decision))
        return decision


def _decision_for(outcome: str) -> Decision:
    if outcome == "allowed-once":
        return Decision(True, reason="approved for this call")
    if outcome == "cancelled":
        return Decision(False, reason="approval was cancelled")
    if outcome == "unavailable":
        return Decision(False, reason="no interactive approval handler is available")
    return Decision(False, reason="approval was rejected")


__all__ = ["DANGEROUS_PATTERNS", "PermissionGate"]
