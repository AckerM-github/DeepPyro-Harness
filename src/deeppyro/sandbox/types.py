"""Sandbox modes, permission presets, approval policies, and decision records.

The names here are frozen by docs/spec/INTERFACES.md section 6: other workstreams
construct Decision / ApprovalRequest and read their fields, so keep the field
names and defaults stable.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

#: DSH sandbox modes. A mode limits file effects; it never changes the host
#: kernel, network, or the process identity beyond the OS sandbox backend.
Preset = Literal["read-only", "workspace-write", "danger-full-access"]
#: DSH approval policies. "always" is deliberately absent: upstream DSH only
#: knows "ask" and "never".
Approval = Literal["ask", "never"]

PRESETS: tuple[Preset, ...] = ("read-only", "workspace-write", "danger-full-access")
APPROVALS: tuple[Approval, ...] = ("ask", "never")

#: DSH approval outcomes. "allowed-once" grants exactly one call; there is no
#: persistent allow.
OUTCOMES: tuple[str, ...] = ("allowed-once", "rejected", "cancelled", "unavailable")


@dataclasses.dataclass
class Decision:
    """Outcome of one sandbox policy check.

    needs_approval marks a check that the preset itself does not forbid but
    that must be approved by the user before it may run. In the DSH-aligned
    design most checks are decided directly by the sandbox backend; the flag is
    kept for the approval bridge (Decision is part of the frozen interface).
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
    call_id: str | None = None
    reason: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class PermissionPresetSpec:
    """One named permission preset: a sandbox mode plus an approval policy."""

    name: str
    sandbox: Preset
    approval: Approval
    description: str = ""


#: The DSH base presets (dsh-base cordis.patch.yml). "custom" is a derived,
#: read-only state and is never a switch target.
DEFAULT_PERMISSION_PRESETS: tuple[PermissionPresetSpec, ...] = (
    PermissionPresetSpec(
        name="read-only",
        sandbox="read-only",
        approval="ask",
        description="Read files and run commands without modifying anything.",
    ),
    PermissionPresetSpec(
        name="workspace-write",
        sandbox="workspace-write",
        approval="ask",
        description=(
            "Write inside the workspace and permitted temporary directories; wider retries require approval."
        ),
    ),
    PermissionPresetSpec(
        name="danger-full-access",
        sandbox="danger-full-access",
        approval="never",
        description="Full file access without approval prompts.",
    ),
)
DEFAULT_PERMISSION_PRESET = "workspace-write"
CUSTOM_PERMISSION_PRESET = "custom"
