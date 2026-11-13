"""Shared helpers for PTC tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deeppyro.core.protocols import ToolContext
from deeppyro.sandbox import PermissionGate


def make_context(
    cwd: Path,
    *,
    services: dict[str, Any] | None = None,
    permission: Any | None = None,
    cancel: asyncio.Event | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> ToolContext:
    """Build a ToolContext suitable for running run_code and nested tools."""
    log: list[tuple[str, dict[str, Any]]] = events if events is not None else []

    def record(kind: str, data: dict[str, Any]) -> None:
        log.append((kind, data))

    if permission is None:
        permission = PermissionGate("danger-full-access", "always", cwd)
    return ToolContext(
        cwd=cwd,
        session_id="session-ptc",
        turn=2,
        step=3,
        call_id="call_ptc_test",
        cancel=cancel if cancel is not None else asyncio.Event(),
        log=record,
        permission=permission,
        services=dict(services or {}),
    )
