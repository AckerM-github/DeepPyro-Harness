"""Sandbox policy seam: presets, approvals, the permission gate, and execution confinement."""

from __future__ import annotations

from deeppyro.sandbox.approval import APPROVAL_POLICY_EVENT, ApprovalService
from deeppyro.sandbox.gate import DANGEROUS_PATTERNS, PermissionGate
from deeppyro.sandbox.permission import PERMISSION_PRESET_EVENT, PermissionPresetsService
from deeppyro.sandbox.policy import (
    SANDBOX_MODE_EVENT,
    SandboxPolicy,
    SandboxPolicyService,
    approval_policy_text,
    sandbox_policy_text,
)
from deeppyro.sandbox.provider import (
    SANDBOX_INVALID_ROOT,
    SANDBOX_UNAVAILABLE,
    SANDBOX_UNSUPPORTED_POLICY,
    Confinement,
    LocalSandboxProvider,
    SandboxInvalidRootError,
    SandboxUnavailableError,
    SandboxUnsupportedPolicyError,
)
from deeppyro.sandbox.types import (
    APPROVALS,
    CUSTOM_PERMISSION_PRESET,
    DEFAULT_PERMISSION_PRESET,
    DEFAULT_PERMISSION_PRESETS,
    OUTCOMES,
    PRESETS,
    Approval,
    ApprovalRequest,
    Decision,
    PermissionPresetSpec,
    Preset,
)

__all__ = [
    "APPROVALS",
    "APPROVAL_POLICY_EVENT",
    "CUSTOM_PERMISSION_PRESET",
    "DANGEROUS_PATTERNS",
    "DEFAULT_PERMISSION_PRESET",
    "DEFAULT_PERMISSION_PRESETS",
    "OUTCOMES",
    "PERMISSION_PRESET_EVENT",
    "PRESETS",
    "SANDBOX_INVALID_ROOT",
    "SANDBOX_MODE_EVENT",
    "SANDBOX_UNAVAILABLE",
    "SANDBOX_UNSUPPORTED_POLICY",
    "Approval",
    "ApprovalRequest",
    "ApprovalService",
    "Confinement",
    "Decision",
    "LocalSandboxProvider",
    "PermissionGate",
    "PermissionPresetSpec",
    "PermissionPresetsService",
    "Preset",
    "SandboxInvalidRootError",
    "SandboxPolicy",
    "SandboxPolicyService",
    "SandboxUnavailableError",
    "SandboxUnsupportedPolicyError",
    "approval_policy_text",
    "sandbox_policy_text",
]
