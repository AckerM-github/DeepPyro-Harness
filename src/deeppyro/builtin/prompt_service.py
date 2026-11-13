"""The prompt service: ordered system prompt sections."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from deeppyro.api import PluginDependencyError, PromptSection, Service


class PromptPlugin(Service):
    """Collects prompt sections and renders the system prompt."""

    provide = "prompt"
    plugin_id = "prompt"
    version = "0.1.0"
    description = "ordered system prompt sections"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self._sections: list[PromptSection] = []

    def add_section(
        self,
        name: str,
        order: int,
        renderer: Callable[[Any, Any], str],
        *,
        plugin_id: str = "",
        scope: str | None = None,
    ) -> Callable[[], None]:
        """Register one section; the returned callable removes it.

        A non-None scope reserves the section for the named agent preset.
        """
        if not name:
            raise PluginDependencyError("prompt section name must not be empty")
        if any(section.name == name for section in self._sections):
            owner = next(section.plugin_id for section in self._sections if section.name == name)
            raise PluginDependencyError(f"prompt section {name!r} is already registered by {owner!r}")
        section = PromptSection(
            name=name,
            order=int(order),
            renderer=renderer,
            plugin_id=plugin_id,
            scope=scope,
        )
        self._sections.append(section)

        def remove() -> None:
            try:
                self._sections.remove(section)
            except ValueError:
                pass

        return remove

    def sections(self, *, scope: str | None = None) -> list[PromptSection]:
        """Sections visible to an agent: global ones plus those scoped to it."""
        visible = [section for section in self._sections if section.scope is None or section.scope == scope]
        return sorted(visible, key=lambda section: (section.order, section.name))

    def build(self, tools: list[Any], prompt_context: Any, *, scope: str | None = None) -> str:
        """Render every visible section; fall back to the core prompt."""
        sections = self.sections(scope=scope)
        if not sections:
            from deeppyro.prompt.prompt import build_system_prompt

            return build_system_prompt(tools, prompt_context)
        from deeppyro.prompt.prompt import render_sections

        ordered = [(section.order, section.name, section.renderer) for section in sections]
        return render_sections(tools, prompt_context, ordered)
