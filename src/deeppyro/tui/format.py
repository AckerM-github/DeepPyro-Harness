"""Small formatting helpers shared by TUI widgets."""

from __future__ import annotations


def short_session_id(session_id: str | None) -> str:
    """Return the first 8 uuid characters of a session id (prefix stripped)."""
    if not session_id:
        return "-"
    bare = str(session_id).removeprefix("session-")
    return bare[:8] or "-"


def compact_text(text: str, *, max_lines: int = 3, max_chars: int = 300) -> str:
    """Collapse a possibly huge payload into a short single-block preview."""
    if not text:
        return ""
    lines = text.splitlines()
    preview = "\n".join(lines[:max_lines]).strip()
    truncated = len(lines) > max_lines or len(text) > max_chars
    if len(preview) > max_chars:
        preview = preview[: max_chars - 1] + "…"
    elif truncated:
        preview += " …"
    return preview


def format_tokens(count: int) -> str:
    """Render a token count compactly: 987, 4.3k, 1.2M."""
    value = int(count or 0)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def context_percent(used: int, window: int) -> int:
    """Context occupancy as an integer percentage (0 when unknown)."""
    if window <= 0:
        return 0
    return max(0, min(100, round(100 * int(used or 0) / window)))


__all__ = ["compact_text", "context_percent", "format_tokens", "short_session_id"]
