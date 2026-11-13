"""Bundled skills plugin: discovery service, skill tool and prompt section."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from deeppyro.api.plugin import Service
from deeppyro.core.errors import ConfigError
from deeppyro.skills.registry import SkillRegistry
from deeppyro.skills.types import SkillCatalog, SkillConfig, SkillDefinition, SkillSummary
from deeppyro.tools.builtin.skill import skill_tools

#: Prompt section order: after instructions (30) and before tools (40).
SKILLS_PROMPT_ORDER = 35
SKILLS_PROMPT_SECTION = "skills"


class SkillsPlugin(Service):
    """Read-only skill discovery plus the skill loading tool."""

    provide = "skills"
    plugin_id = "skills"
    version = "0.1.0"
    description = "read-only skill discovery and the skill loading tool"
    inject: ClassVar[list[str]] = ["config", "tools", "prompt"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        skills_config = _resolve_config(ctx, config)
        self._skills_config = skills_config
        self._registry = SkillRegistry(skills_config)

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.register_tool(lambda _config, _workspace: skill_tools(self))
        ctx.register_prompt_section(SKILLS_PROMPT_SECTION, SKILLS_PROMPT_ORDER, self._render_section)

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    @property
    def skills_config(self) -> SkillConfig:
        return self._skills_config

    def list(self, cwd: str | Path | None = None) -> list[SkillSummary]:
        return self._registry.list(cwd)

    def get(self, name: str, cwd: str | Path | None = None) -> SkillDefinition | None:
        return self._registry.get(name, cwd)

    def read(self, name: str, cwd: str | Path | None = None) -> SkillDefinition:
        return self._registry.read(name, cwd)

    def snapshot(self, cwd: str | Path | None = None) -> SkillCatalog:
        return self._registry.snapshot(cwd)

    def render_catalog(
        self,
        cwd: str | Path | None = None,
        *,
        max_entries: int | None = None,
        description_max_length: int | None = None,
    ) -> str:
        return self._registry.render_catalog(
            cwd,
            max_entries=max_entries,
            description_max_length=description_max_length,
        )

    def _render_section(self, tools: list[Any], prompt_context: Any) -> str:
        try:
            return self._registry.render_catalog(prompt_context.cwd)
        except Exception as exc:  # noqa: BLE001 - prompt assembly must never fail
            self.ctx.logger.warning(f"skills prompt section failed: {exc}")
            return ""


def _resolve_config(ctx: Any, config: Any) -> SkillConfig:
    """Resolve settings from the bundle row config, else the [skills] table.

    The row config wins when non-empty so a bundle can pin skill roots; the
    global [skills] table is the normal user-facing knob.
    """
    if isinstance(config, SkillConfig):
        return config
    if isinstance(config, BaseModel):
        return _validated(config.model_dump())
    if isinstance(config, dict) and config:
        return _validated(config)
    config_service = ctx.service("config")
    raw = getattr(config_service.value, "skills", None)
    if raw is None:
        return SkillConfig()
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    return _validated(raw)


def _validated(raw: Any) -> SkillConfig:
    if not isinstance(raw, dict):
        raise ConfigError("the [skills] configuration must be a table")
    try:
        return SkillConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"invalid [skills] configuration: {exc}") from exc


__all__ = ["SKILLS_PROMPT_ORDER", "SKILLS_PROMPT_SECTION", "SkillsPlugin"]
