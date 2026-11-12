"""Bridge between tools and the sandbox permission gate.

Tools get the gate through `ToolContext.permission`. Sessions that do not
configure one fall back to a permissive gate (the session layer decides
whether a gate exists at all); the built-in tool tests use an explicit gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.core.errors import PermissionDenied, ToolError
from deeppyro.core.protocols import ToolContext
from deeppyro.sandbox import ApprovalRequest, Decision, PermissionGate
from deeppyro.sandbox.types import Preset


def gate_for(ctx: ToolContext) -> PermissionGate:
    """Return the context's gate or a permissive fallback gate."""
    gate = getattr(ctx, "permission", None)
    if gate is None:
        return PermissionGate("danger-full-access", "always", Path(ctx.cwd))
    return gate  # type: ignore[return-value]


def _deny(decision: Decision, action: str) -> PermissionDenied:
    reason = decision.reason or "denied by the permission gate"
    return PermissionDenied(f"{action} denied: {reason}")


def _effective_preset(gate: PermissionGate, args: dict[str, Any]) -> Preset:
    """Validate a one-shot escalation request and return the mode to use."""
    requested = args.get("sandbox_permissions")
    if requested is None:
        return gate.preset
    if not isinstance(requested, str):
        raise ToolError("sandbox_permissions must be a string")
    justification = args.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        raise ToolError("justification is required when sandbox_permissions is set")
    try:
        gate.validate_escalation(requested)
    except Exception as exc:
        raise ToolError(str(exc)) from exc
    return requested  # type: ignore[return-value]


async def _approve_escalation(gate: PermissionGate, ctx: ToolContext, args: dict[str, Any]) -> bool:
    """Ask for approval of a one-shot escalation; return True when one happened."""
    requested = args.get("sandbox_permissions")
    if requested is None:
        return False
    justification = str(args.get("justification") or "")
    summary = f"Allow this {args.get('_action', 'operation')} to run with {requested} for one call?"
    req = ApprovalRequest(kind="sandbox-escalation", summary=summary, detail=justification)
    decision = await gate.request(req)
    if not decision.allowed:
        raise PermissionDenied(f"sandbox escalation denied: {decision.reason}")
    return True


async def check_write(ctx: ToolContext, args: dict[str, Any], path: Path, *, action: str = "write") -> None:
    """Authorize a write to `path` (honouring a one-shot escalation)."""
    gate = gate_for(ctx)
    _effective_preset(gate, args)
    escalated = await _approve_escalation(gate, ctx, args)
    if escalated:
        return
    decision = gate.check_write(path)
    if decision.allowed:
        return
    if decision.needs_approval:
        approved = await gate.request(
            ApprovalRequest(
                kind="write-outside-workspace",
                summary="Allow this write outside the workspace?",
                detail=str(path),
            )
        )
        if approved.allowed:
            return
        raise PermissionDenied(f"{action} denied: {approved.reason}")
    raise _deny(decision, action)


async def check_command(ctx: ToolContext, args: dict[str, Any], command: str) -> None:
    """Authorize a shell command (honouring a one-shot escalation)."""
    gate = gate_for(ctx)
    _effective_preset(gate, args)
    escalated = await _approve_escalation(gate, ctx, args)
    if escalated:
        return
    decision = gate.check_command(command)
    if decision.allowed:
        return
    if decision.needs_approval:
        approved = await gate.request(
            ApprovalRequest(
                kind="command-approval",
                summary="Allow this shell command?",
                detail=command,
            )
        )
        if approved.allowed:
            return
        raise PermissionDenied(f"command denied: {approved.reason}")
    raise _deny(decision, "command")


def check_read(ctx: ToolContext, path: Path) -> None:
    """Authorize a read (all presets allow reads)."""
    decision = gate_for(ctx).check_read(path)
    if not decision.allowed:
        raise _deny(decision, "read")
