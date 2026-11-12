"""Permission gate: preset policy, workspace boundary, and approval policy.

The gate is the single source of truth for "may this read/write/command run?".
Tools call the synchronous `check_*` methods before touching the outside
world and then either deny the call or - when `needs_approval` is set - await
:meth:`PermissionGate.request` so the approval policy and the interactive
callback decide.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from deeppyro.core.errors import ConfigError
from deeppyro.sandbox.types import (
    APPROVALS,
    PRESETS,
    Approval,
    ApprovalRequest,
    Decision,
    Preset,
)

# Commands that can destroy data outside the current task. Under
# `workspace-write` they require an explicit approval; `read-only` rejects
# every command and `danger-full-access` lets the preset itself decide.
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
    ("machine shutdown", re.compile(r"\b(?:shutdown|Restart-Computer|Stop-Computer|reboot)\b", re.IGNORECASE)),
    ("registry deletion", re.compile(r"\breg\s+delete\b", re.IGNORECASE)),
    ("cipher wipe", re.compile(r"\bcipher\s+/w\b", re.IGNORECASE)),
)

_PRESET_ORDER: dict[str, int] = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}


class PermissionGate:
    """Preset + approval policy for one agent session.

    `preset` decides what is allowed at all; `approval` decides what happens
    to an action the preset does not allow outright but that the user could
    still grant once:

    * `never`  - deny every `needs_approval` decision (including escalation).
    * `always` - allow every `needs_approval` decision without asking.
    * `ask`    - mark the decision `needs_approval` and let the caller await
      :meth:`request`, which invokes `ask` (or denies when it is `None`).
    """

    def __init__(
        self,
        preset: Preset,
        approval: Approval,
        workspace: Path,
        ask: Callable[[ApprovalRequest], Awaitable[bool]] | None = None,
    ) -> None:
        if preset not in PRESETS:
            raise ConfigError(f"invalid permission preset: {preset!r}")
        if approval not in APPROVALS:
            raise ConfigError(f"invalid approval policy: {approval!r}")
        self._preset: Preset = preset
        self._approval: Approval = approval
        self._workspace = Path(workspace).expanduser().resolve(strict=False)
        self._ask = ask
        # Every decision handed to an approval callback, kept for the session
        # log / UI. Not part of the frozen interface but cheap and useful.
        self.approvals: list[tuple[ApprovalRequest, Decision]] = []

    # -- introspection -----------------------------------------------------

    @property
    def preset(self) -> Preset:
        return self._preset

    @property
    def approval(self) -> Approval:
        return self._approval

    @property
    def workspace(self) -> Path:
        return self._workspace

    def is_within_workspace(self, path: Path) -> bool:
        """Return True when `path` (after resolving `..`/symlinks) is under the workspace."""
        try:
            Path(path).expanduser().resolve(strict=False).relative_to(self._workspace)
        except ValueError:
            return False
        return True

    # -- policy checks -----------------------------------------------------

    def check_read(self, path: Path) -> Decision:
        """Reads are allowed under every preset.

        A sandbox that hid the filesystem would prevent the agent from
        diagnosing the very problem it was asked about, and every preset in DSH
        allows reads.
        """
        return Decision(True)

    def check_write(self, path: Path) -> Decision:
        """Decide whether `path` may be written."""
        target = Path(path).expanduser()
        if self._preset == "danger-full-access":
            return Decision(True)
        if self._preset == "read-only":
            return Decision(False, reason=f"read-only preset forbids writing {target}")
        if self.is_within_workspace(target):
            return Decision(True)
        denied = Decision(False, reason=f"path is outside the workspace: {target}", needs_approval=True)
        return self._apply_approval(denied)

    def check_command(self, command: str) -> Decision:
        """Decide whether `command` may run in the shell."""
        if self._preset == "read-only":
            return Decision(False, reason="read-only preset forbids running shell commands")
        if self._preset == "danger-full-access":
            return Decision(True)
        label = self._dangerous_label(command)
        if label is None:
            return Decision(True)
        denied = Decision(False, reason=f"command looks destructive ({label}): {command}", needs_approval=True)
        return self._apply_approval(denied)

    def validate_escalation(self, target: str) -> None:
        """Validate a one-shot `sandbox_permissions` escalation target.

        Escalation may only move strictly wider than the current preset. The
        caller is responsible for asking for approval before honouring it.
        """
        if target not in _PRESET_ORDER:
            raise ConfigError(f"invalid sandbox mode: {target!r}")
        if _PRESET_ORDER[target] <= _PRESET_ORDER[self._preset]:
            raise ConfigError(f"sandbox mode {target!r} is not wider than the current preset {self._preset!r}")

    # -- approvals ---------------------------------------------------------

    async def request(self, req: ApprovalRequest) -> Decision:
        """Resolve one approval request according to the approval policy."""
        if self._approval == "never":
            decision = Decision(False, reason="approval policy is never")
        elif self._approval == "always":
            decision = Decision(True, reason="approval policy is always")
        elif self._ask is None:
            decision = Decision(False, reason="no interactive approval handler is available")
        else:
            try:
                granted = bool(await self._ask(req))
            except Exception as exc:  # noqa: BLE001 - a user callback must not crash the turn
                decision = Decision(False, reason=f"approval handler failed: {exc}")
            else:
                decision = (
                    Decision(True, reason="approved by the user")
                    if granted
                    else Decision(False, reason="rejected by the user")
                )
        self.approvals.append((req, decision))
        return decision

    # -- internals ---------------------------------------------------------

    def _apply_approval(self, denied: Decision) -> Decision:
        if self._approval == "never":
            return Decision(False, reason=f"{denied.reason}; approval policy is never")
        if self._approval == "always":
            return Decision(True, reason=f"{denied.reason}; approval policy is always")
        return denied

    @staticmethod
    def _dangerous_label(command: str) -> str | None:
        for label, pattern in DANGEROUS_PATTERNS:
            if pattern.search(command):
                return label
        return None
