"""Small reasoning-text helpers shared by the TUI process widgets.

Reasoning is noisy: the conversation shows exactly one line at a time - the
newest sentence of the current step - so a turn reads like a live progress line
rather than a wall of text. These helpers only split and trim text; widgets live
in agent_process.py and screens/thinking.py.
"""

from __future__ import annotations

#: Maximum characters of the live sentence visible while streaming.
STREAMING_LINE_CHARS = 120

_ENDINGS = frozenset("。！？!?；;")


def split_sentences(text: str) -> list[str]:
    """Split reasoning into sentences (CJK/ASCII terminators and newlines)."""
    sentences: list[str] = []
    buffer = ""
    for char in text:
        if char == "\n":
            piece = buffer.strip()
            if piece:
                sentences.append(piece)
            buffer = ""
            continue
        buffer += char
        if char in _ENDINGS:
            piece = buffer.strip()
            if piece:
                sentences.append(piece)
            buffer = ""
    tail = buffer.strip()
    if tail:
        sentences.append(tail)
    return sentences


def latest_sentence(text: str) -> str:
    """Return the newest sentence, or an empty string for empty reasoning."""
    sentences = split_sentences(text)
    if not sentences:
        return ""
    return " ".join(sentences[-1].split())


__all__ = [
    "STREAMING_LINE_CHARS",
    "latest_sentence",
    "split_sentences",
]
