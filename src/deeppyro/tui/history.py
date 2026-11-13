"""Reasoning history helpers for the terminal UI.

Reasoning is already part of the durable session log: every assistant/message
stores its thinking parts inside the message content. This module rebuilds a
per-turn view for the collapsed progress line, the full-screen thinking viewer
and the history replay performed by /resume. No new storage format is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ReasoningStep:
    """One assistant step's reasoning inside a user turn."""

    step: int = 0
    text: str = ""


@dataclass(slots=True)
class TurnReasoning:
    """All reasoning recorded for one user turn."""

    turn: int = 0
    steps: list[ReasoningStep] = field(default_factory=list)
    finished: bool = False

    @property
    def has_reasoning(self) -> bool:
        return bool(self.steps)

    @property
    def sentence_count(self) -> int:
        from deeppyro.tui.widgets.thinking import split_sentences

        return sum(len(split_sentences(step.text)) for step in self.steps)

    def text_for(self, step: int) -> str:
        for item in self.steps:
            if item.step == step:
                return item.text
        return ""


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _part_text(part: Any) -> str:
    """Return the text of a reasoning content part, or an empty string."""
    if isinstance(part, dict):
        if part.get("type") == "reasoning":
            return str(part.get("text") or "")
        return ""
    if getattr(part, "type", "") == "reasoning":
        return str(getattr(part, "text", "") or "")
    return ""


def reasoning_text(message: Any) -> str:
    """Join every reasoning part of one assistant message."""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if not isinstance(content, list):
        return ""
    chunks = [_part_text(part) for part in content]
    return "".join(chunk for chunk in chunks if chunk)


def reasoning_from_session(session: Any, *, last_turns: int | None = 1) -> list[TurnReasoning]:
    """Rebuild reasoning grouped by user turn from durable session records.

    Args:
        session: an object with a records list (the Session or a fake).
        last_turns: keep only the newest N turns with reasoning; None keeps all.

    Returns:
        Turns in chronological order, each with assistant steps in order.
    """
    records = getattr(session, "records", None)
    if not isinstance(records, list):
        return []
    turns: list[TurnReasoning] = []
    current: TurnReasoning | None = None
    fallback_turn = 1
    for record in records:
        record_type = str(getattr(record, "type", ""))
        data = getattr(record, "data", None)
        if not isinstance(data, dict):
            if record_type == "turn/start":
                current = TurnReasoning(turn=fallback_turn)
                turns.append(current)
            continue
        if record_type == "turn/start":
            number = _as_int(data.get("turn"), fallback_turn)
            fallback_turn = number + 1
            current = TurnReasoning(turn=number)
            turns.append(current)
        elif record_type == "assistant/message":
            if current is None:
                current = TurnReasoning(turn=fallback_turn)
                turns.append(current)
            message = data.get("message")
            text = reasoning_text(message)
            if not text:
                continue
            step = _as_int(data.get("step"), len(current.steps) + 1)
            current.steps.append(ReasoningStep(step=step, text=text))
        elif record_type == "turn/end" and current is not None:
            current.finished = True
            current = None
    filtered = [turn for turn in turns if turn.steps]
    if last_turns is None:
        return filtered
    if last_turns <= 0:
        return []
    return filtered[-last_turns:]


def reasoning_map(session: Any) -> dict[int, TurnReasoning]:
    """Turn number -> reasoning for replay and /thinking lookups."""
    return {turn.turn: turn for turn in reasoning_from_session(session, last_turns=None)}


__all__ = [
    "ReasoningStep",
    "TurnReasoning",
    "reasoning_from_session",
    "reasoning_map",
    "reasoning_text",
]
