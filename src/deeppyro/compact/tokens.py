"""Heuristic token accounting used by compaction decisions."""

from __future__ import annotations

from typing import Any

from deeppyro.core.wire import Message

CHARS_PER_TOKEN = 3.5
MESSAGE_OVERHEAD = 4.0


def _part_field(part: Any, name: str, default: Any = "") -> Any:
    if isinstance(part, dict):
        return part.get(name, default)
    return getattr(part, name, default)


def _text_length(part: Any) -> int:
    kind = _part_field(part, "type")
    if kind in ("text", "reasoning"):
        return len(str(_part_field(part, "text")))
    if kind == "tool-call":
        return len(str(_part_field(part, "name"))) + len(str(_part_field(part, "arguments", "")))
    return 0


def estimate_text_tokens(text: str) -> float:
    """Estimate tokens for plain text using the frozen 3.5 chars/token ratio."""
    if not text:
        return 0.0
    return len(text) / CHARS_PER_TOKEN


def estimate_message_tokens(message: Message) -> float:
    """Estimate one wire message: characters / 3.5 plus a fixed per-message overhead."""
    chars = 0
    for part in message.content:
        chars += _text_length(part)
    return chars / CHARS_PER_TOKEN + MESSAGE_OVERHEAD


def estimate_messages_tokens(messages: list[Message]) -> float:
    """Estimate a message list (the model-facing context size)."""
    return sum(estimate_message_tokens(message) for message in messages)
