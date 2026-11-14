"""Shared test helpers for the tool tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.core.protocols import ToolContext
from deeppyro.sandbox import PermissionGate
from deeppyro.sandbox.policy import SandboxPolicy

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SESSIONS = FIXTURES / "sessions"


def make_context(
    cwd: Path,
    *,
    permission: PermissionGate | None = None,
    services: dict[str, Any] | None = None,
    call_id: str = "call_test",
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> ToolContext:
    """Build a ToolContext with a permissive gate and optional service overrides."""
    if permission is None:
        permission = PermissionGate("danger-full-access", "never", cwd)
    log = (
        (lambda event, payload: events.append((event, payload)))
        if events is not None
        else (lambda event, payload: None)
    )
    return ToolContext(
        cwd=Path(cwd),
        session_id="session-test",
        turn=0,
        step=0,
        call_id=call_id,
        cancel=asyncio.Event(),
        log=log,
        permission=permission,
        services=dict(services or {}),
    )


def write_file(root: Path, name: str, content: str, *, encoding: str = "utf-8") -> Path:
    """Create a file (and parents) under root and return its path."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding, newline="")
    return path


class ConfiningSandbox:
    """Structural stand-in for the sandbox provider seam in tool tests."""

    def __init__(
        self,
        enforcement: str = "partial",
        *,
        denial_signatures: tuple[str, ...] = (),
        runner_failure_rules: tuple[Any, ...] = (),
        temp_root: Path | None = None,
    ) -> None:
        self.enforcement = enforcement
        self.policies: list[SandboxPolicy] = []
        self.denial_signatures = denial_signatures
        self.runner_failure_rules = runner_failure_rules
        self.temp_root = temp_root

    def confine(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        signal: object | None = None,
    ) -> SimpleNamespace:
        self.policies.append(policy)
        return SimpleNamespace(
            argv=list(argv),
            enforcement=self.enforcement,
            denial_signatures=self.denial_signatures,
            runner_failure_rules=self.runner_failure_rules,
            temp_root=self.temp_root,
        )


def load_real_tool_calls() -> dict[str, list[dict[str, Any]]]:
    """Extract real decoded tool-call arguments per tool name from the fixtures."""
    wanted = {
        "read",
        "write",
        "edit",
        "glob",
        "grep",
        "pwsh",
        "web_search",
        "web_fetch",
        "todo_write",
        "present",
    }
    found: dict[str, list[dict[str, Any]]] = {}
    if not SESSIONS.is_dir():
        return found
    for path in sorted(SESSIONS.glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if record.get("type") != "tool/call":
                    continue
                data = record.get("data", {})
                name = data.get("name")
                if name not in wanted:
                    continue
                try:
                    args = json.loads(data.get("arguments") or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(args, dict):
                    found.setdefault(name, []).append(args)
    return found
