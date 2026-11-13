"""Read-only skill registry over filesystem roots.

The registry is a thin, stateless view: every list/get call rescans the roots,
which keeps skills live-editable during a session without a watcher. Catalogs
are small, so rescanning is cheaper than keeping a cache coherent.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .discovery import SkillRoot, discover_roots, scan_skills
from .errors import SkillNotFoundError, SkillParseError
from .frontmatter import is_valid_skill_name, parse_skill_file
from .types import SkillCatalog, SkillConfig, SkillDefinition, SkillSummary

_CATALOG_INTRO = (
    "A skill is a reusable set of task-specific instructions. "
    "The following skills are available in this session:"
)
_CATALOG_GUIDE = (
    "If the user names a skill, or the task clearly matches a skill's description, "
    "call the skill tool with the exact skill name before taking task actions. "
    "Load all applicable skills, then follow their full instructions. "
    "This catalog contains summaries only; do not infer or follow a skill's "
    "instructions until it has been loaded."
)
_CATALOG_DIRECT = (
    "A user may also invoke a skill directly; its <skill_content> block then appears "
    "in this conversation. Follow it, and do not call the skill tool again for that skill."
)


def escape_text(value: str) -> str:
    """Escape text for an XML-like instruction block."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape_attr(value: str) -> str:
    """Escape text for a double-quoted XML-like attribute."""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def catalog_description(value: str, max_length: int = 500) -> str:
    """Normalize whitespace and truncate a description for the catalog."""
    normalized = " ".join(value.split())
    limit = max(3, int(max_length))
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def render_skill_content(skill: SkillDefinition) -> str:
    """Render a loaded skill as the <skill_content> instruction block."""
    if skill.resource_base:
        resource = [
            f"Base directory for this skill: {escape_text(skill.resource_base)}",
            (
                "Resolve relative paths mentioned by this skill against the base directory "
                "before using them. Load referenced resources only as needed."
            ),
        ]
    else:
        resource = [
            f'Resources for this skill are managed by provider "{escape_text(skill.provider)}".',
            "Load referenced resources only as needed.",
        ]
    return "\n".join(
        [
            f'<skill_content name="{escape_attr(skill.name)}">',
            "<skill_resources>",
            *resource,
            "</skill_resources>",
            "",
            "<skill_instructions>",
            skill.content,
            "</skill_instructions>",
            "</skill_content>",
        ]
    )


class SkillRegistry:
    """Discover, load and render filesystem-backed skills."""

    def __init__(
        self,
        config: SkillConfig | None = None,
        *,
        roots: Sequence[SkillRoot] | None = None,
        home: Path | None = None,
    ) -> None:
        self._config = config if config is not None else SkillConfig()
        self._roots = tuple(roots) if roots is not None else None
        self._home = home

    @property
    def config(self) -> SkillConfig:
        return self._config

    def roots(self, cwd: str | Path | None = None) -> tuple[SkillRoot, ...]:
        if self._roots is not None:
            return self._roots
        base = Path(cwd) if cwd is not None else Path.cwd()
        return tuple(discover_roots(base, self._config, home=self._home))

    def snapshot(self, cwd: str | Path | None = None) -> SkillCatalog:
        scan = scan_skills(self.roots(cwd))
        return SkillCatalog(skills=scan.skills, errors=scan.errors)

    def list(self, cwd: str | Path | None = None) -> list[SkillSummary]:
        return list(self.snapshot(cwd).skills)

    def get(self, name: str, cwd: str | Path | None = None) -> SkillDefinition | None:
        if not is_valid_skill_name(name):
            return None
        summary = next(
            (item for item in self.snapshot(cwd).skills if item.name == name),
            None,
        )
        if summary is None:
            return None
        try:
            parsed = parse_skill_file(Path(summary.path))
        except SkillParseError:
            return None
        if parsed.name != summary.name:
            return None
        return SkillDefinition(
            name=summary.name,
            description=summary.description,
            when_to_use=summary.when_to_use,
            model_invocable=summary.model_invocable,
            user_invocable=summary.user_invocable,
            source=summary.source,
            provider=summary.provider,
            path=summary.path,
            resource_base=summary.resource_base,
            content=parsed.body,
        )

    def read(self, name: str, cwd: str | Path | None = None) -> SkillDefinition:
        definition = self.get(name, cwd)
        if definition is None:
            raise SkillNotFoundError(f"skill {name!r} is unknown or no longer available")
        return definition

    def render_catalog(
        self,
        cwd: str | Path | None = None,
        *,
        max_entries: int | None = None,
        description_max_length: int | None = None,
    ) -> str:
        """Render the model-facing catalog block, or an empty string."""
        skills = [skill for skill in self.snapshot(cwd).skills if skill.model_invocable]
        if not skills:
            return ""
        limit = int(max_entries if max_entries is not None else self._config.catalog_max_entries)
        if limit <= 0:
            return ""
        max_length = int(
            description_max_length
            if description_max_length is not None
            else self._config.catalog_description_max_length
        )
        entries = [
            f"- {skill.name}: {escape_text(catalog_description(skill.description, max_length))}"
            for skill in skills[:limit]
        ]
        lines = [
            "<system-reminder>",
            _CATALOG_INTRO,
            "",
            "<available_skills>",
            *entries,
            "</available_skills>",
            "",
            _CATALOG_GUIDE,
            _CATALOG_DIRECT,
            "</system-reminder>",
        ]
        return "\n".join(lines)


__all__ = [
    "SkillRegistry",
    "catalog_description",
    "escape_attr",
    "escape_text",
    "render_skill_content",
]
