"""Filesystem helpers for skills tests."""

from __future__ import annotations

from pathlib import Path

from deeppyro.skills.discovery import SkillRoot
from deeppyro.skills.types import SOURCE_CUSTOM


def write_skill(
    root: Path,
    name: str,
    *,
    description: str | None = None,
    body: str = "Use this skill for the task.",
    extra: str = "",
    directory: str | None = None,
    frontmatter: bool = True,
) -> Path:
    """Write <root>/<directory or name>/SKILL.md and return the file path."""
    folder = Path(root) / (directory if directory is not None else name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    if frontmatter:
        summary = description if description is not None else "Description of " + name
        text = "---\nname: " + name + "\ndescription: " + summary + "\n"
        if extra:
            text += extra.rstrip("\n") + "\n"
        text += "---\n\n" + body.strip() + "\n"
    else:
        text = body.strip() + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def write_flat(root: Path, filename: str, text: str) -> Path:
    """Write a flat Markdown skill file and return its path."""
    Path(root).mkdir(parents=True, exist_ok=True)
    path = Path(root) / filename
    path.write_text(text, encoding="utf-8")
    return path


def make_project(base: Path) -> Path:
    """Create a fake project root with a .git directory."""
    project = Path(base) / "project"
    (project / ".git").mkdir(parents=True)
    return project


def explicit_root(path: Path, *, rank: int = 300, source: str = SOURCE_CUSTOM) -> SkillRoot:
    return SkillRoot(rank=rank, source=source, path=Path(path), label=source)
