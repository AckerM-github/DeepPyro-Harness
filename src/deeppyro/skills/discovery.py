"""Filesystem discovery of skills.

Discovery is intentionally simple: the context cwd determines the project
root, roots are ranked, and every root contributes direct child directories
with a SKILL.md plus direct child Markdown files. A malformed file never
aborts the scan: the error is collected and the next candidate is tried.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from deeppyro import paths

from .errors import SkillParseError
from .frontmatter import parse_skill_file
from .types import (
    BUNDLED_RANK,
    CUSTOM_RANK,
    PROJECT_AGENTS_RANK,
    PROJECT_DSH_RANK,
    SOURCE_BUNDLED,
    SOURCE_CUSTOM,
    SOURCE_PROJECT_AGENTS,
    SOURCE_PROJECT_DSH,
    SOURCE_USER_DSH,
    USER_DSH_RANK,
    SkillConfig,
    SkillSource,
    SkillSummary,
)


@dataclass(frozen=True, slots=True)
class SkillRoot:
    """One directory that may contain skill candidates."""

    rank: int
    source: SkillSource
    path: Path
    label: str = ""


@dataclass(frozen=True, slots=True)
class SkillScan:
    """The result of scanning a tuple of roots."""

    skills: tuple[SkillSummary, ...] = ()
    errors: tuple[str, ...] = ()
    roots: tuple[SkillRoot, ...] = ()


def find_project_root(cwd: str | Path) -> Path:
    """Find the nearest ancestor containing .git, else the cwd itself."""
    candidate = Path(cwd).expanduser()
    try:
        candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError):  # pragma: no cover - platform edge
        pass
    for current in (candidate, *candidate.parents):
        try:
            if (current / ".git").exists():
                return current
        except OSError:  # pragma: no cover - permission edge
            continue
    return candidate


def discover_roots(
    cwd: str | Path,
    config: SkillConfig | None = None,
    *,
    home: Path | None = None,
) -> list[SkillRoot]:
    """Compute the ordered root list for one working directory.

    Rank order is the DSH precedence order: project .dsh, project .agents,
    custom directories, user home, bundled assets. Duplicate resolved paths
    keep their first (lowest-rank) occurrence.
    """
    cfg = config if config is not None else SkillConfig()
    project = find_project_root(cwd)
    roots: list[SkillRoot] = []
    seen: set[Path] = set()

    def add(rank: int, source: SkillSource, path: Path) -> None:
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):  # pragma: no cover - platform edge
            resolved = Path(path)
        if resolved in seen:
            return
        seen.add(resolved)
        roots.append(SkillRoot(rank=rank, source=source, path=resolved, label=source))

    if cfg.include_default_roots:
        add(PROJECT_DSH_RANK, SOURCE_PROJECT_DSH, project / ".dsh" / "skills")
        add(PROJECT_AGENTS_RANK, SOURCE_PROJECT_AGENTS, project / ".agents" / "skills")

    for raw in cfg.custom_skill_dirs:
        if not raw.strip():
            continue
        custom = Path(raw).expanduser()
        if not custom.is_absolute():
            custom = project / custom
        add(CUSTOM_RANK, SOURCE_CUSTOM, custom)

    if cfg.user_skills_dir.strip():
        user_dir = Path(cfg.user_skills_dir).expanduser()
    else:
        base = Path(home) if home is not None else paths.deeppyro_home()
        user_dir = base / "skills"
    add(USER_DSH_RANK, SOURCE_USER_DSH, user_dir)

    if cfg.bundled_skill_dir.strip():
        add(BUNDLED_RANK, SOURCE_BUNDLED, Path(cfg.bundled_skill_dir).expanduser())

    roots.sort(key=lambda item: (item.rank, str(item.path)))
    return roots


def scan_skills(roots: Sequence[SkillRoot]) -> SkillScan:
    """Scan roots and return summaries sorted by skill name.

    Lower rank wins when two candidates share a name; the losing candidate is
    recorded as a non-fatal warning. Unreadable roots and malformed files are
    recorded in errors as well.
    """
    ordered = tuple(sorted(roots, key=lambda item: (item.rank, str(item.path))))
    found: dict[str, SkillSummary] = {}
    errors: list[str] = []
    for root in ordered:
        candidates, root_errors = _candidate_files(root)
        errors.extend(root_errors)
        for candidate in candidates:
            try:
                parsed = parse_skill_file(candidate)
            except SkillParseError as exc:
                errors.append(str(exc))
                continue
            summary = SkillSummary(
                name=parsed.name,
                description=parsed.description,
                when_to_use=parsed.when_to_use,
                model_invocable=parsed.model_invocable,
                user_invocable=parsed.user_invocable,
                source=root.source,
                provider="filesystem",
                path=str(candidate),
                resource_base=str(candidate.parent),
            )
            existing = found.get(parsed.name)
            if existing is not None:
                errors.append(
                    f"duplicate skill {parsed.name!r} at {candidate} ignored; "
                    f"already provided by {existing.path}"
                )
                continue
            found[parsed.name] = summary
    skills = tuple(sorted(found.values(), key=lambda item: item.name))
    return SkillScan(skills=skills, errors=tuple(errors), roots=ordered)


def _candidate_files(root: SkillRoot) -> tuple[list[Path], list[str]]:
    """List direct child candidates of one root in deterministic order."""
    if not root.path.is_dir():
        return [], []
    try:
        entries = sorted(root.path.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return [], [f"cannot read skill root {root.path}: {exc}"]
    candidates: list[Path] = []
    errors: list[str] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                candidate = entry / "SKILL.md"
                if candidate.is_file():
                    candidates.append(candidate)
            elif entry.is_file() and entry.suffix.lower() == ".md":
                candidates.append(entry)
        except OSError as exc:  # pragma: no cover - permission edge
            errors.append(f"cannot inspect skill entry {entry}: {exc}")
    return candidates, errors


__all__ = [
    "SkillRoot",
    "SkillScan",
    "discover_roots",
    "find_project_root",
    "scan_skills",
]
