"""Permission gate: preset policy, workspace boundary, and approval policy.

The gate is the single source of truth for "may this read/write/command run?".
Tools call the synchronous `check_*` methods before touching the outside
world and then either deny the call or - when `needs_approval` is set - await
:meth:`PermissionGate.request` so the approval policy and the interactive
callback decide.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from deeppyro.core.errors import ConfigError, SessionError
from deeppyro.hooks.types import APPROVAL_ASKED, APPROVAL_DECIDED, APPROVAL_REQUEST
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
_APPROVAL_OUTCOMES = ("allow", "deny", "ask", "cancel")

logger = logging.getLogger(__name__)

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
        *,
        hooks: Any | None = None,
    ) -> None:
        if preset not in PRESETS:
            raise ConfigError(f"invalid permission preset: {preset!r}")
        if approval not in APPROVALS:
            raise ConfigError(f"invalid approval policy: {approval!r}")
        self._preset: Preset = preset
        self._approval: Approval = approval
        self._workspace = Path(workspace).expanduser().resolve(strict=False)
        self._ask = ask
        # Attached lazily by the sandbox plugin (hooks load after sandbox, so
        # the gate cannot inject the service). Observability only: a hook that
        # fails must never change the decision.
        self._hooks = hooks
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

    def set_approval(self, approval: Approval) -> None:
        """Switch the approval policy for this session (UI: always allow)."""
        if approval not in APPROVALS:
            raise ConfigError(f"invalid approval policy: {approval!r}")
        self._approval = approval

    def set_preset(self, preset: Preset) -> None:
        """Switch the sandbox preset for this session (UI: policy dropdown)."""
        if preset not in PRESETS:
            raise ConfigError(f"invalid permission preset: {preset!r}")
        self._preset = preset

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
        denied = Decision(
            False, reason=f"command looks destructive ({label}): {command}", needs_approval=True
        )
        return self._apply_approval(denied)

    def validate_escalation(self, target: str) -> None:
        """Validate a one-shot `sandbox_permissions` escalation target.

        Escalation may only move strictly wider than the current preset. The
        caller is responsible for asking for approval before honouring it.
        """
        if target not in _PRESET_ORDER:
            raise ConfigError(f"invalid sandbox mode: {target!r}")
        if _PRESET_ORDER[target] <= _PRESET_ORDER[self._preset]:
            raise ConfigError(
                f"sandbox mode {target!r} is not wider than the current preset {self._preset!r}"
            )

    # -- approvals ---------------------------------------------------------

    async def request(self, req: ApprovalRequest) -> Decision:
        """Resolve one approval request.

        When a hook service is attached the ``approval/request`` waterfall may
        decide first (``allow``/``deny``/``ask``/``cancel``); ``ask`` falls
        through to the policy below. ``approval/asked`` is emitted right before
        the interactive handler runs and ``approval/decided`` after every
        resolution, including ones refused without asking.
        """
        decision, via = await self._resolve(req)
        self.approvals.append((req, decision))
        await self._emit(
            APPROVAL_DECIDED,
            {"request": req, "allowed": decision.allowed, "reason": decision.reason, "via": via},
        )
        return decision

    async def _resolve(self, req: ApprovalRequest) -> tuple[Decision, str]:
        outcome = await self._ask_hook(req)
        if outcome == "allow":
            return Decision(True, reason="approved by approval plugin"), "hook"
        if outcome == "deny":
            return Decision(False, reason="denied by approval plugin"), "hook"
        if outcome == "cancel":
            return Decision(False, reason="approval canceled"), "hook"
        return await self._policy(req)

    async def _ask_hook(self, req: ApprovalRequest) -> str:
        hooks = self._hooks
        if hooks is None or not (hooks.has(APPROVAL_REQUEST) or bool(getattr(hooks, "observed", False))):
            return "ask"
        payload = {
            "request": req,
            "preset": self._preset,
            "policy": self._approval,
            "agent": None,
            "signal": None,
        }
        outcome = await hooks.waterfall(APPROVAL_REQUEST, payload, fallback="ask")
        if outcome is None:
            # A listener that vetoes the dispatch does not decide: fall back to
            # the configured policy instead of failing the tool call.
            outcome = "ask"
        if not isinstance(outcome, str) or outcome not in _APPROVAL_OUTCOMES:
            raise SessionError(
                f"approval/request listener returned {outcome!r}; "
                "expected one of 'allow', 'deny', 'ask', 'cancel'"
            )
        return outcome

    async def _policy(self, req: ApprovalRequest) -> tuple[Decision, str]:
        if self._approval == "never":
            return Decision(False, reason="approval policy is never"), "policy"
        if self._approval == "always":
            return Decision(True, reason="approval policy is always"), "policy"
        if self._ask is None:
            return Decision(False, reason="no interactive approval handler is available"), "policy"
        await self._emit(APPROVAL_ASKED, {"request": req, "policy": self._approval})
        try:
            granted = bool(await self._ask(req))
        except Exception as exc:  # noqa: BLE001 - a user callback must not crash the turn
            return Decision(False, reason=f"approval handler failed: {exc}"), "user"
        if granted:
            return Decision(True, reason="approved by the user"), "user"
        return Decision(False, reason="rejected by the user"), "user"

    async def _emit(self, name: str, payload: dict[str, Any]) -> None:
        hooks = self._hooks
        if hooks is None or not (hooks.has(name) or bool(getattr(hooks, "observed", False))):
            return
        try:
            await hooks.emit(name, payload)
        except Exception:
            logger.exception("hook %r failed while dispatching an approval event", name)

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
