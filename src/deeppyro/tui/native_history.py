"""History helpers for native sessions.

The v4 history helpers read raw session records. Native sessions fold schema 1
events, so this module reuses the same record-based helpers through the native
record view. Shapes and semantics stay identical, which keeps the thinking
screen and the rendered history code unchanged.
"""

from __future__ import annotations

from typing import Any

from deeppyro.tui import history as _history
from deeppyro.tui.native_facts import native_view


def _history_session(session: Any) -> Any:
    """Project one native session/handle as a v4-history-readable session."""
    return native_view(session)


def reasoning_from_session(session: Any, *, last_turns: int | None = 1) -> list[Any]:
    """Reasoning turns for a native session, newest-last."""
    return _history.reasoning_from_session(_history_session(session), last_turns=last_turns)


def reasoning_map(session: Any) -> dict[int, Any]:
    """Reasoning turns keyed by turn number."""
    return _history.reasoning_map(_history_session(session))


def processes_from_session(session: Any, *, last_turns: int | None = None) -> list[Any]:
    """Turn process entries (steps and tool calls) for a native session."""
    return _history.processes_from_session(_history_session(session), last_turns=last_turns)


def process_map(session: Any) -> dict[int, Any]:
    """Turn process entries keyed by turn number."""
    return _history.process_map(_history_session(session))


def is_ptc_mode_message(message: Any) -> bool:
    """Native sessions never carry PTC mode messages."""
    return _history.is_ptc_mode_message(message)


__all__ = [
    "is_ptc_mode_message",
    "process_map",
    "processes_from_session",
    "reasoning_from_session",
    "reasoning_map",
]
