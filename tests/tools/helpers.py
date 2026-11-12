"""Shared test helpers for the tool tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from deeppyro.core.protocols import ToolContext
from deeppyro.sandbox import PermissionGate

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
        permission = PermissionGate("danger-full-access", "always", cwd)
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
