"""The sandbox execution seam and the local Windows backend.

Mirrors dsh-sandbox + dsh-sandbox-local. confine() either returns an argv that
runs under OS enforcement, or raises SandboxUnavailableError: it never falls back
to an unconfined command. danger-full-access is the only mode that runs the
original argv, and it reports enforcement="full".

On Windows the backend is an ACL restricted-token runner (partial enforcement:
file writes are confined, network and reads are not). Other platforms currently
have no usable backend, so read-only and workspace-write fail closed.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro.core.errors import DeeppyroError
from deeppyro.sandbox.policy import SandboxPolicy, canonical_path

SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
SANDBOX_UNSUPPORTED_POLICY = "SANDBOX_UNSUPPORTED_POLICY"
SANDBOX_INVALID_ROOT = "SANDBOX_INVALID_ROOT"

ESCALATION_TARGETS: tuple[str, ...] = ("workspace-write", "danger-full-access")
WIDER_MODES: dict[str, tuple[str, ...]] = {
    "read-only": ("workspace-write", "danger-full-access"),
    "workspace-write": ("danger-full-access",),
}

WINDOWS_DENIAL_SIGNATURES: tuple[str, ...] = (
    "access is denied",
    "access to the path",
    "permission denied",
    "operation not permitted",
)


class SandboxError(DeeppyroError):
    """Base class for sandbox backend errors."""

    code = "SANDBOX_ERROR"


class SandboxUnavailableError(SandboxError):
    """No usable backend: the consumer must not run the command unconfined."""

    code = SANDBOX_UNAVAILABLE

    def __init__(self, mode: str, detail: str | None = None) -> None:
        message = (
            f'sandbox mode "{mode}" is requested but no sandbox backend is usable on this host; '
            "refusing to run the command unconfined. Install bubblewrap or run a Landlock-enforcing "
            "kernel (Linux), ensure sandbox-exec is usable (macOS), or ensure the ACL restricted-token "
            "runner can start (Windows) - otherwise switch the consumer to danger-full-access."
        )
        if detail:
            message += f" Runner failure: {detail}"
        super().__init__(message)
        self.mode = mode
        self.detail = detail


class SandboxUnsupportedPolicyError(SandboxError):
    """The backend cannot express this policy (for example extra roots on Windows)."""

    code = SANDBOX_UNSUPPORTED_POLICY


class SandboxInvalidRootError(SandboxError):
    """An additional writable root is not a physical existing directory."""

    code = SANDBOX_INVALID_ROOT


@dataclass(frozen=True, slots=True)
class RunnerFailureRule:
    """How to recognise a broken backend runner in a command failure."""

    allowed_exit_codes: tuple[int, ...] = ()
    fatal_signatures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Confinement:
    """An argv ready to spawn plus the enforcement facts to report."""

    argv: tuple[str, ...]
    enforcement: str = "full"
    denial_signatures: tuple[str, ...] = ()
    runner_failure_rules: tuple[RunnerFailureRule, ...] = ()
    temp_root: Path | None = None


def sandbox_denial_marker(mode: str) -> str:
    """The exact DSH marker appended to a denied command's output."""
    return f"[sandbox: file access denied under {mode} mode]"


def escalation_hint_marker(subject: str) -> str:
    """The exact DSH hint appended when a one-shot escalation is available."""
    return (
        f"[sandbox: escalation available - retry this exact {subject} once with "
        "sandbox_permissions (the narrowest wider mode that suffices) + justification; "
        "the approval prompt asks the user]"
    )


def sandbox_permissions_description(subject: str) -> str:
    """The tool-schema description for the sandbox_permissions property."""
    return (
        f"The narrowest wider sandbox mode for a one-shot retry of the exact {subject} the "
        "sandbox just denied; the retry asks the user for approval."
    )


def validate_escalation_args(args: dict[str, Any]) -> tuple[str | None, str | None]:
    """Validate the paired sandbox_permissions/justification tool arguments.

    Returns (mode, justification) or raises ValueError with the DSH wording.
    """
    requested = args.get("sandbox_permissions")
    justification = args.get("justification")
    if requested is None:
        if justification is not None:
            raise ValueError(
                "invalid escalation: justification is only valid together with sandbox_permissions"
            )
        return None, None
    if not isinstance(requested, str):
        raise ValueError("invalid escalation: sandbox_permissions must be a string")  # noqa: TRY004
    if requested not in ESCALATION_TARGETS:
        raise ValueError(f'invalid escalation target: "{requested}"')
    if not isinstance(justification, str) or not justification.strip():
        raise ValueError("invalid escalation: sandbox_permissions requires a justification")
    return requested, justification.strip()


def classify_runner_failure(
    exit_code: int | None,
    output: str,
    rules: tuple[RunnerFailureRule, ...],
) -> str | None:
    """Return the failure detail when the runner itself failed, else None."""
    if exit_code is None or exit_code == 0:
        return None
    lowered = output.casefold()
    for rule in rules:
        if rule.allowed_exit_codes and exit_code not in rule.allowed_exit_codes:
            continue
        for signature in rule.fatal_signatures:
            if signature.casefold() in lowered:
                return signature
    return None


def matches_denial(output: str, signatures: tuple[str, ...]) -> bool:
    lowered = output.casefold()
    return any(signature.casefold() in lowered for signature in signatures)


class LocalSandboxProvider:
    """confine() for Windows ACL, or fail closed when no backend exists."""

    def confine(
        self,
        argv: list[str] | tuple[str, ...],
        policy: SandboxPolicy,
        *,
        signal: Any | None = None,
        temp_root: str | Path | None = None,
    ) -> Confinement:
        if policy.mode == "danger-full-access":
            return Confinement(tuple(str(arg) for arg in argv), enforcement="full")
        if os.name != "nt":
            raise SandboxUnavailableError(policy.mode, "no POSIX sandbox backend is available")
        if policy.additional_writable_roots:
            raise SandboxUnsupportedPolicyError(
                "Windows ACL confinement does not support additional writable directories; "
                "remove them or use danger-full-access"
            )
        return self._windows(argv, policy, temp_root=temp_root)

    def _windows(
        self,
        argv: list[str] | tuple[str, ...],
        policy: SandboxPolicy,
        *,
        temp_root: str | Path | None = None,
    ) -> Confinement:
        from deeppyro.sandbox import windows_acl_runner as runner

        workspace = canonical_path(policy.workspace_root)
        temp = _private_temp_root(temp_root)
        runner_argv = [
            *runner.self_command(),
            "--workspace",
            str(workspace),
            "--temp",
            str(temp),
            "--mode",
            policy.mode,
            "--",
            *[str(arg) for arg in argv],
        ]
        return Confinement(
            tuple(runner_argv),
            enforcement="partial",
            denial_signatures=WINDOWS_DENIAL_SIGNATURES,
            runner_failure_rules=(RunnerFailureRule((127,), ("windows-acl-run: ",)),),
            temp_root=temp,
        )


def _private_temp_root(temp_root: str | Path | None) -> Path:
    if temp_root is not None:
        path = Path(temp_root)
        path.mkdir(parents=True, exist_ok=True)
        return path
    base = Path(tempfile.gettempdir()) / f"deeppyro-{uuid.uuid4().hex[:16]}"
    base.mkdir(parents=True, exist_ok=True)
    return base


__all__ = [
    "ESCALATION_TARGETS",
    "SANDBOX_INVALID_ROOT",
    "SANDBOX_UNAVAILABLE",
    "SANDBOX_UNSUPPORTED_POLICY",
    "WIDER_MODES",
    "Confinement",
    "LocalSandboxProvider",
    "RunnerFailureRule",
    "SandboxError",
    "SandboxInvalidRootError",
    "SandboxUnavailableError",
    "SandboxUnsupportedPolicyError",
    "classify_runner_failure",
    "escalation_hint_marker",
    "matches_denial",
    "sandbox_denial_marker",
    "sandbox_permissions_description",
    "validate_escalation_args",
]
