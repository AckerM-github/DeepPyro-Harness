"""Bridge between tools and the DSH sandbox services.

The gate still owns the small synchronous check API, but the DSH-aligned flow is:
try the operation under the current mode, and only after the sandbox actually
denied it may the model retry once with sandbox_permissions + justification.
The retry is approved through the approval service; the escalated mode applies
to exactly that one call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.core.errors import PermissionDenied, ToolError
from deeppyro.core.protocols import ToolContext
from deeppyro.sandbox import ApprovalRequest, PermissionGate
from deeppyro.sandbox.policy import SandboxPolicy, is_path_under, writable_roots
from deeppyro.sandbox.provider import (
    WIDER_MODES,
    escalation_hint_marker,
    sandbox_denial_marker,
    validate_escalation_args,
)
from deeppyro.sandbox.types import Preset


def gate_for(ctx: ToolContext) -> PermissionGate:
    """Return the context's gate or a permissive fallback gate."""
    gate = getattr(ctx, "permission", None)
    if gate is None:
        return PermissionGate("danger-full-access", "never", Path(ctx.cwd))
    return gate  # type: ignore[return-value]


async def prepare_escalation(
    ctx: ToolContext,
    args: dict[str, Any],
    *,
    subject: str,
    tool: str | None = None,
) -> Preset:
    """Validate and approve a one-shot sandbox_permissions retry.

    Returns the mode this one call should use. The current mode is returned when
    no escalation was requested (or when the target equals the current mode,
    which DSH treats as a no-op rather than an approval prompt).
    """
    gate = gate_for(ctx)
    try:
        requested, justification = validate_escalation_args(args)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    current = gate.policy.mode
    if requested is None or requested == current:
        return current
    try:
        gate.validate_escalation(requested)
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    request = ApprovalRequest(
        kind="sandbox-escalation",
        summary=f'Allow this {subject} to run with "{requested}" for one call?',
        detail=justification or "",
        tool=tool,
        reason=f"escalate sandbox to {requested}: {justification or ''}".strip(),
    )
    decision = await gate.request(request)
    if not decision.allowed:
        if decision.reason == "approval was cancelled":
            raise PermissionDenied(f'approval for escalating to "{requested}" was cancelled')
        raise PermissionDenied(
            f'the user rejected escalating this {subject} to "{requested}"; it stays denied, '
            "so stop and explain instead of working around it"
        )
    return requested  # type: ignore[return-value]


async def check_write(
    ctx: ToolContext,
    args: dict[str, Any],
    path: Path,
    *,
    action: str = "write",
    tool: str | None = None,
) -> None:
    """Authorize a write strictly by the DSH file policy (no prompt)."""
    gate = gate_for(ctx)
    mode = await prepare_escalation(ctx, args, subject="write", tool=tool)
    policy = gate.policy
    if mode != policy.mode:
        policy = SandboxPolicy(mode=mode, workspace_root=policy.workspace_root)
    decision = _check_write_with(policy, path)
    if decision is True:
        return
    raise PermissionDenied(_denial_message(action, decision, policy.mode))


async def check_command(
    ctx: ToolContext, args: dict[str, Any], command: str, *, tool: str | None = None
) -> Preset:
    """Return the sandbox mode this shell call must run under.

    Commands are not pre-approved: the OS sandbox either confines them or the
    provider fails closed. Only an explicit escalation needs approval here.
    """
    return await prepare_escalation(ctx, args, subject="command", tool=tool)


def check_read(ctx: ToolContext, path: Path) -> None:
    """Authorize a read (all modes allow reads)."""
    decision = gate_for(ctx).check_read(path)
    if not decision.allowed:
        raise PermissionDenied(decision.reason or "read denied")


def _check_write_with(policy: SandboxPolicy, path: Path) -> bool | str:
    """True when allowed, else the DSH denial sentence."""
    target = path.expanduser().resolve(strict=False)
    if policy.mode == "danger-full-access":
        return True
    if policy.mode == "read-only":
        return f'cannot write "{target}": file access denied under read-only mode'
    if any(is_path_under(target, root) for root in writable_roots(policy)):
        return True
    return f'cannot write "{target}": file access denied under workspace-write mode'


def _denial_message(action: str, reason: str | bool, mode: str) -> str:
    text = reason if isinstance(reason, str) else f"{action} denied"
    marker = sandbox_denial_marker(mode)
    hint = ""
    if mode in WIDER_MODES:
        subject = action if action in ("write", "command") else "operation"
        hint = "\n" + escalation_hint_marker(subject)
    return f"{action} denied: {text}\n{marker}{hint}"


__all__ = ["check_command", "check_read", "check_write", "gate_for", "prepare_escalation"]
