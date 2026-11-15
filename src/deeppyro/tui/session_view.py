"""Readable session rows shared by ``/sessions`` and ``/resume``.

The store exposes facts; this module decides how much of them a person needs to
recognise a conversation without opening it: title, how long ago it moved, how
many turns it holds, and a one-line preview of the first message.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from deeppyro.session.metadata import EMPTY_TITLE
from deeppyro.tui.format import short_session_id

_DAY = 86400


def relative_time(timestamp: Any, *, now: float | None = None) -> str:
    """Human distance from ``timestamp`` (epoch seconds or milliseconds)."""
    value = _seconds(timestamp)
    if value is None:
        return "-"
    delta = max(0.0, (time.time() if now is None else float(now)) - value)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < _DAY:
        return f"{int(delta // 3600)}h ago"
    if delta < 7 * _DAY:
        return f"{int(delta // _DAY)}d ago"
    return time.strftime("%Y-%m-%d", time.localtime(value))


def _seconds(timestamp: Any) -> float | None:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Session headers store epoch milliseconds; records store milliseconds too.
    return value / 1000.0 if value > 1e11 else value


def title_of(info: Any) -> str:
    return str(getattr(info, "title", None) or EMPTY_TITLE)


def preview_of(info: Any) -> str:
    return str(getattr(info, "preview", None) or "")


def stamp_of(info: Any) -> Any:
    updated = getattr(info, "updatedAt", 0) or 0
    return updated or getattr(info, "createdAt", 0)


def picker_items(sessions: Sequence[Any]) -> list[tuple[str, str, str]]:
    """(value, label, note) triplets for PickerScreen; every field is searchable."""
    items: list[tuple[str, str, str]] = []
    for info in sessions:
        session_id = str(getattr(info, "id", "") or "")
        label = f"{title_of(info)}  \u00b7  {relative_time(stamp_of(info))}"
        turns = getattr(info, "turns", 0) or 0
        if turns:
            label += f"  \u00b7  {turns}t"
        note = "  ".join(part for part in (preview_of(info), str(getattr(info, "cwd", "") or "")) if part)
        items.append((session_id, label, note))
    return items


def listing_lines(sessions: Sequence[Any], *, now: float | None = None) -> list[str]:
    """Numbered lines grouped by workspace, one per session."""
    lines: list[str] = []
    current: str | None = None
    for index, info in enumerate(sessions, start=1):
        cwd = str(getattr(info, "cwd", "") or "")
        if cwd != current:
            current = cwd
            lines.append(f"{cwd or '(no workspace)'}")
        turns = getattr(info, "turns", 0) or 0
        parts = [f"  {index:>2}. {short_session_id(getattr(info, 'id', ''))}", title_of(info)]
        parts.append(relative_time(stamp_of(info), now=now))
        if turns:
            parts.append(f"{turns}t")
        line = "  ".join(parts)
        preview = preview_of(info)
        if preview:
            line += f"  \u2014  {preview}"
        lines.append(line)
    return lines


def matches(info: Any, query: str) -> bool:
    """True when the query appears in the id, title, preview or workspace."""
    needle = query.strip().lower()
    if not needle:
        return True
    haystacks = (
        str(getattr(info, "id", "") or ""),
        title_of(info),
        preview_of(info),
        str(getattr(info, "cwd", "") or ""),
    )
    return any(needle in value.lower() for value in haystacks)
