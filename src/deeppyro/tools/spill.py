"""Truncation and spill-to-file handling for tool output.

DSH keeps full tool output out of the model context: the model gets a
head/tail window plus a marker, and the complete text is saved to a spill file
so it can be retrieved later. Spill failures are non-fatal - a successful tool
call must never become an error only because its output could not be saved.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

DEFAULT_MAX_OUTPUT_CHARS = 30_000
"""Maximum characters of tool output kept inline for the model."""


def truncate_text(
    text: str,
    limit: int,
    *,
    head_chars: int | None = None,
) -> tuple[str, int]:
    """Truncate `text` keeping head and tail, returning (text, omitted chars).

    The middle is replaced by a marker of the form
    `... [truncated N chars] ...`. When the text fits, it is returned
    unchanged with zero omitted characters.
    """
    if limit <= 0 or len(text) <= limit:
        return text, 0
    head = limit // 2 if head_chars is None else head_chars
    head = max(0, min(head, limit))
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    if omitted <= 0:
        return text, 0
    marker = f"... [truncated {omitted} chars] ..."
    truncated = f"{text[:head]}{marker}{text[len(text) - tail :] if tail else ''}"
    return truncated, omitted


def spill_text(spill_dir: Path, call_id: str, tool_name: str, text: str) -> str | None:
    """Write `text` into `spill_dir` and return the file path, or None on failure."""
    try:
        target_dir = Path(spill_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{call_id or 'call'}-{tool_name or 'tool'}.txt"
        target = target_dir / filename
        target.write_text(text, encoding="utf-8", errors="replace")
        return str(target)
    except OSError:
        return None


def spill_dir_from_services(services: dict[str, object]) -> Path | None:
    """Read the optional spill directory override from session services."""
    value = services.get("spill_dir")
    if isinstance(value, (str, Path)):
        return Path(value)
    return None


def resolve_limit(services: dict[str, object], default: int = DEFAULT_MAX_OUTPUT_CHARS) -> int:
    """Read the optional output-character limit from session services."""
    value = services.get("max_output_chars")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def default_spill_dir(cwd: Path) -> Path:
    """Return the conventional spill directory for a workspace."""
    return Path(cwd) / ".deeppyro" / "spill"


def cleanup_spill_dir(path: Path) -> None:
    """Best-effort removal of a spill directory (used by tests/tools)."""
    with contextlib.suppress(OSError):
        if path.is_dir():
            for child in path.iterdir():
                with contextlib.suppress(OSError):
                    child.unlink()
            path.rmdir()
