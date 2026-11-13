"""Read-only skills subsystem.

Public surface: discovery roots and scanning, the SkillRegistry service that
the bundled plugin exposes, frontmatter parsing, and the prompt/catalog
renderers used by the skill tool and the skills prompt section.
"""

from __future__ import annotations

from .discovery import SkillRoot, SkillScan, discover_roots, find_project_root, scan_skills
from .errors import SkillError, SkillNotFoundError, SkillParseError
from .frontmatter import (
    FRONTMATTER_DELIMITER,
    SKILL_NAME_RE,
    SkillFile,
    fallback_skill_name,
    first_paragraph,
    is_valid_skill_name,
    parse_skill_file,
    parse_skill_text,
)
from .registry import SkillRegistry, catalog_description, escape_attr, escape_text, render_skill_content
from .types import (
    BUNDLED_RANK,
    CUSTOM_RANK,
    PROJECT_AGENTS_RANK,
    PROJECT_DSH_RANK,
    SKILL_SOURCE_RANKS,
    SKILLS_SERVICE_NAME,
    SOURCE_BUNDLED,
    SOURCE_CUSTOM,
    SOURCE_PROJECT_AGENTS,
    SOURCE_PROJECT_DSH,
    SOURCE_USER_DSH,
    USER_DSH_RANK,
    SkillCatalog,
    SkillConfig,
    SkillDefinition,
    SkillSource,
    SkillSummary,
)

__all__ = [
    "BUNDLED_RANK",
    "CUSTOM_RANK",
    "FRONTMATTER_DELIMITER",
    "PROJECT_AGENTS_RANK",
    "PROJECT_DSH_RANK",
    "SKILLS_SERVICE_NAME",
    "SKILL_NAME_RE",
    "SKILL_SOURCE_RANKS",
    "SOURCE_BUNDLED",
    "SOURCE_CUSTOM",
    "SOURCE_PROJECT_AGENTS",
    "SOURCE_PROJECT_DSH",
    "SOURCE_USER_DSH",
    "USER_DSH_RANK",
    "SkillCatalog",
    "SkillConfig",
    "SkillDefinition",
    "SkillError",
    "SkillFile",
    "SkillNotFoundError",
    "SkillParseError",
    "SkillRegistry",
    "SkillRoot",
    "SkillScan",
    "SkillSource",
    "SkillSummary",
    "catalog_description",
    "discover_roots",
    "escape_attr",
    "escape_text",
    "fallback_skill_name",
    "find_project_root",
    "first_paragraph",
    "is_valid_skill_name",
    "parse_skill_file",
    "parse_skill_text",
    "render_skill_content",
    "scan_skills",
]
