"""Data types for the default subagent layer."""

from __future__ import annotations

from dataclasses import dataclass

MAX_LIVE_RUNS = 4
DEFAULT_CONTEXT_WINDOW = 32768


@dataclass(slots=True)
class SubagentResult:
    """Final reply of one one-shot child agent run."""

    child_id: str
    session_id: str
    label: str
    provider: str
    model: str
    text: str = ""
    is_error: bool = False
    error: str = ""


__all__ = ["DEFAULT_CONTEXT_WINDOW", "MAX_LIVE_RUNS", "SubagentResult"]
