"""Data types for the read-only skills subsystem.

A skill is a directory containing SKILL.md (or a flat Markdown file) with a
small YAML frontmatter block. Discovery turns files into SkillSummary values;
loading adds the command body and produces a SkillDefinition. The catalog is
the model-facing projection of the current discovery scan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SkillSource = Literal["project-dsh", "project-agents", "custom", "user-dsh", "bundled"]

PROJECT_DSH_RANK = 100
PROJECT_AGENTS_RANK = 200
CUSTOM_RANK = 300
USER_DSH_RANK = 400
BUNDLED_RANK = 600

SOURCE_PROJECT_DSH: SkillSource = "project-dsh"
SOURCE_PROJECT_AGENTS: SkillSource = "project-agents"
SOURCE_CUSTOM: SkillSource = "custom"
SOURCE_USER_DSH: SkillSource = "user-dsh"
SOURCE_BUNDLED: SkillSource = "bundled"

#: Lower rank wins when two roots provide a skill with the same name.
SKILL_SOURCE_RANKS: dict[SkillSource, int] = {
    SOURCE_PROJECT_DSH: PROJECT_DSH_RANK,
    SOURCE_PROJECT_AGENTS: PROJECT_AGENTS_RANK,
    SOURCE_CUSTOM: CUSTOM_RANK,
    SOURCE_USER_DSH: USER_DSH_RANK,
    SOURCE_BUNDLED: BUNDLED_RANK,
}

#: Default service name provided by the bundled SkillsPlugin.
SKILLS_SERVICE_NAME = "skills"


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """Invocation-neutral skill metadata, without the body."""

    name: str
    description: str
    when_to_use: str = ""
    model_invocable: bool = True
    user_invocable: bool = True
    source: SkillSource = SOURCE_PROJECT_DSH
    provider: str = "filesystem"
    path: str = ""
    resource_base: str = ""


@dataclass(frozen=True, slots=True)
class SkillDefinition(SkillSummary):
    """A loaded skill: metadata plus the Markdown body."""

    content: str = ""


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """One discovery observation plus non-fatal diagnostics.

    Entries are sorted by name. Errors contain parse failures and duplicate
    warnings; discovery never raises for a single bad file.
    """

    skills: tuple[SkillSummary, ...] = ()
    errors: tuple[str, ...] = ()


class SkillConfig(BaseModel):
    """The [skills] configuration table.

    The filesystem provider is deliberately small in P1: no watcher, no
    provider registry. Roots follow the DSH rank order; a later provider layer
    can replace this model without changing the registry API.
    """

    model_config = ConfigDict(extra="allow")

    include_default_roots: bool = True
    custom_skill_dirs: list[str] = Field(default_factory=list)
    #: Empty means deeppyro_home()/skills (usually ~/.dpro/skills).
    user_skills_dir: str = ""
    #: Empty in P1: no bundled skill assets ship with DeepPyro yet.
    bundled_skill_dir: str = ""
    catalog_max_entries: int = 500
    catalog_description_max_length: int = 500
    watch: bool = False


__all__ = [
    "BUNDLED_RANK",
    "CUSTOM_RANK",
    "PROJECT_AGENTS_RANK",
    "PROJECT_DSH_RANK",
    "SKILLS_SERVICE_NAME",
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
    "SkillSource",
    "SkillSummary",
]
