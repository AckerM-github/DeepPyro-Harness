"""Sandbox presets, approval policies, and decision records.

The names here are frozen by `docs/spec/INTERFACES.md` section 6: other
workstreams construct :class:`Decision` / :class:`ApprovalRequest` and read
their fields, so keep the field names and defaults stable.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

Preset = Literal["read-only", "workspace-write", "danger-full-access"]
Approval = Literal["ask", "never", "always"]

PRESETS: tuple[Preset, ...] = ("read-only", "workspace-write", "danger-full-access")
APPROVALS: tuple[Approval, ...] = ("ask", "never", "always")


@dataclasses.dataclass
class Decision:
    """Outcome of one sandbox policy check.

    `needs_approval` marks a check that the preset itself does not forbid but
    that must be approved by the user before it may run (for example writing
    outside the workspace in `workspace-write` mode). In that case
    `allowed` stays `False` until :meth:`PermissionGate.request` returns an
    approving decision.
    """

    allowed: bool
    reason: str = ""
    needs_approval: bool = False


@dataclasses.dataclass
class ApprovalRequest:
    """One approval prompt shown to the user before a gated action runs."""

    kind: str
    summary: str
    detail: str = ""
    tool: str | None = None
