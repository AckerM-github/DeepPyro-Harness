"""Data types for the default subagent layer."""

from __future__ import annotations

from dataclasses import dataclass

MAX_LIVE_RUNS = 4
DEFAULT_CONTEXT_WINDOW = 32768


@dataclass(frozen=True, slots=True)
class SubagentActivation:
    """Handle returned as soon as a background child agent starts."""

    child_id: str
    session_id: str
    label: str
    provider: str
    model: str
    mode: str
    created_at: int


@dataclass(slots=True)
class SubagentResult:
    """Final reply of one child agent run."""

    child_id: str
    session_id: str
    label: str
    provider: str
    model: str
    text: str = ""
    is_error: bool = False
    error: str = ""
    mode: str = "spawn"


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "MAX_LIVE_RUNS",
    "SubagentActivation",
    "SubagentResult",
]
