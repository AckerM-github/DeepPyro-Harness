"""Sandbox policy: the standing file mode for one session.

Mirrors dsh-sandbox-policy. A policy is resolved per call from an optional
explicit mode, the last log-only sandbox/mode record of the session, and the
deployment default. The workspace root always comes from the session cwd, so a
session cannot be talked into writing somewhere else by configuration.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro.core.errors import ConfigError, SessionError
from deeppyro.sandbox.types import PRESETS, Preset

#: One log-only event carries every mode switch; replay derives the standing
#: mode from the last record.
SANDBOX_MODE_EVENT = "sandbox/mode"


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Resolved policy for one sandbox consumer call."""

    mode: Preset
    workspace_root: Path
    additional_writable_roots: tuple[Path, ...] = ()


class SandboxPolicyService:
    """Resolve and persist the DSH sandbox policy."""

    def __init__(
        self,
        default_mode: str = "workspace-write",
        workspace_root: str | Path | None = None,
        additional_writable_roots: tuple[str | Path, ...] = (),
    ) -> None:
        self._default_mode = _mode(default_mode)
        self._workspace_root = Path(workspace_root).expanduser() if workspace_root else None
        self._additional = tuple(
            Path(root).expanduser().resolve(strict=False) for root in additional_writable_roots
        )

    @property
    def default_mode(self) -> Preset:
        return self._default_mode

    def resolve(self, session: Any | None = None, mode: str | None = None) -> SandboxPolicy:
        """Resolve the policy for one call: explicit > session > default."""
        resolved = _mode(mode) if mode is not None else self.last_mode(session) or self._default_mode
        root = self.workspace_root(session)
        return SandboxPolicy(resolved, root, self._additional)

    def workspace_root(self, session: Any | None = None) -> Path:
        """Return the workspace root: session cwd, else the configured root."""
        header = getattr(session, "header", None)
        cwd = getattr(header, "cwd", None)
        if isinstance(cwd, str) and cwd.strip():
            return canonical_path(cwd)
        if self._workspace_root is not None:
            return canonical_path(self._workspace_root)
        return canonical_path(Path.cwd())

    def last_mode(self, session: Any | None = None) -> Preset | None:
        """Return the last sandbox/mode record of the session, if any."""
        for record in reversed(getattr(session, "records", ()) or ()):
            if getattr(record, "type", None) != SANDBOX_MODE_EVENT:
                continue
            data = getattr(record, "data", None) or {}
            mode = data.get("mode") if isinstance(data, dict) else None
            if mode in PRESETS:
                return mode  # type: ignore[return-value]
            raise SessionError(f"invalid sandbox/mode record: {mode!r}")
        return None

    def set_mode(self, session: Any, mode: str) -> Preset:
        """Persist one sandbox/mode event; a no-op switch writes nothing."""
        resolved = _mode(mode)
        if self.last_mode(session) == resolved:
            return resolved
        session.append(SANDBOX_MODE_EVENT, {"mode": resolved})
        return resolved


def canonical_path(path: str | Path) -> Path:
    """Resolve symlinks and dot segments without requiring the path to exist."""
    return Path(path).expanduser().resolve(strict=False)


def workspace_writable_roots(policy: SandboxPolicy) -> tuple[Path, ...]:
    """Workspace + configured additional roots (no platform temporary areas)."""
    if policy.mode != "workspace-write":
        return ()
    roots = [canonical_path(policy.workspace_root)]
    roots.extend(canonical_path(root) for root in policy.additional_writable_roots)
    return _dedupe(roots)


def writable_roots(policy: SandboxPolicy) -> tuple[Path, ...]:
    """Every directory a workspace-write call may modify.

    DSH adds /tmp and the platform temporary directory; the same roots are used
    here so a tool that writes a temporary file keeps working in workspace-write.
    """
    roots = list(workspace_writable_roots(policy))
    if policy.mode == "workspace-write":
        roots.append(canonical_path("/tmp"))
        roots.append(canonical_path(tempfile.gettempdir()))
    return _dedupe(roots)


def is_path_under(path: str | Path, root: str | Path) -> bool:
    """True when path is root or a descendant of root (case-folded on Windows)."""
    target = canonical_path(path)
    base = canonical_path(root)
    try:
        target.relative_to(base)
        return True
    except ValueError:
        return False


def sandbox_policy_text(policy: SandboxPolicy) -> str:
    """The exact DSH runtime-context paragraph for one policy."""
    if policy.mode == "read-only":
        return (
            "Current DSH file policy: read-only. Any available operation enforced by the DSH "
            "file sandbox cannot modify files in the standing mode. Do not refuse a required "
            "modification from this policy alone: try an available tool normally and follow "
            "any denial and escalation guidance it returns."
        )
    if policy.mode == "danger-full-access":
        return (
            "Current DSH file policy: danger-full-access. The DSH file sandbox does not "
            "restrict file modifications by available operations."
        )
    text = (
        "Current DSH file policy: workspace-write. Any available operation enforced by the "
        'DSH file sandbox may modify files under the session workspace: "'
        f'{policy.workspace_root}".'
    )
    additional = [str(root) for root in policy.additional_writable_roots]
    if additional:
        rendered = ", ".join(f'"{root}"' for root in additional)
        text += f" Additional writable directories: [{rendered}]."
    return text + " Some platform temporary areas may also be writable."


def approval_policy_text(policy: str) -> str:
    """The exact DSH runtime-context paragraph for one approval policy."""
    if policy == "never":
        return (
            "Approval prompts are disabled in this session: actions that require approval "
            "are rejected automatically - do not request sandbox escalation (do not set "
            "sandbox_permissions)."
        )
    return (
        "Approval prompts are enabled in this session: operations that require approval may "
        "ask through the configured answerers; without an available answerer, the request "
        "fails closed."
    )


def _mode(value: Any) -> Preset:
    if value not in PRESETS:
        raise ConfigError(f"invalid sandbox mode: {value!r}")
    return value  # type: ignore[return-value]


def _dedupe(paths: list[Path]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


__all__ = [
    "SANDBOX_MODE_EVENT",
    "SandboxPolicy",
    "SandboxPolicyService",
    "approval_policy_text",
    "canonical_path",
    "is_path_under",
    "sandbox_policy_text",
    "workspace_writable_roots",
    "writable_roots",
]
