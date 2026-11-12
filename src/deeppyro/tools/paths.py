"""Path resolution and display helpers shared by the built-in tools."""

from __future__ import annotations

from pathlib import Path

from deeppyro.core.errors import ToolError


def resolve_path(cwd: Path, value: str | Path) -> Path:
    """Resolve a tool-supplied path against the session working directory.

    Both relative and absolute paths are accepted; each is expanded and
    resolved so the value in output/metadata is canonical.
    """
    path = Path(value).expanduser()
    base = Path(cwd)
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve(strict=False)
    except OSError as exc:  # pragma: no cover - only exotic platforms
        raise ToolError(f"cannot resolve path {value!r}: {exc}") from exc


def display_path(path: Path, cwd: Path) -> str:
    """Render a path relative to the session cwd when possible."""
    try:
        return str(path.relative_to(Path(cwd)))
    except ValueError:
        return str(path)
