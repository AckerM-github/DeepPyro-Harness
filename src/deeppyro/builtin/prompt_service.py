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
        revision: int | None = None,
    ) -> Callable[[], None]:
        """Register one section; the returned callable removes it.

        A non-None scope reserves the section for the named agent preset; a
        non-None revision pins it to one immutable preset revision. The same
        name may exist in different compositions (scope, revision), but one
        composition cannot register a name twice.
        """
        if not name:
            raise PluginDependencyError("prompt section name must not be empty")
        duplicate = next(
            (
                section
                for section in self._sections
                if section.name == name and section.scope == scope and section.revision == revision
            ),
            None,
        )
        if duplicate is not None:
            raise PluginDependencyError(
                f"prompt section {name!r} is already registered by {duplicate.plugin_id!r}"
            )
        section = PromptSection(
            name=name,
            order=int(order),
            renderer=renderer,
            plugin_id=plugin_id,
            scope=scope,
            revision=revision,
        )
        self._sections.append(section)

        def remove() -> None:
            try:
                self._sections.remove(section)
            except ValueError:
                pass

        return remove

    @staticmethod
    def _visible(section: PromptSection, scope: str | None, revision: int | None) -> bool:
        """Whether a section belongs to the composition for scope/revision."""
        if section.scope is None:
            return True
        if section.scope != scope:
            return False
        if section.revision is None:
            return True
        return revision is not None and section.revision == revision

    def sections(
        self,
        *,
        scope: str | None = None,
        revision: int | None = None,
    ) -> list[PromptSection]:
        """Sections visible to an agent: global ones plus those scoped to it."""
        visible = [section for section in self._sections if self._visible(section, scope, revision)]
        return sorted(visible, key=lambda section: (section.order, section.name))

    def build(
        self,
        tools: list[Any],
        prompt_context: Any,
        *,
        scope: str | None = None,
        revision: int | None = None,
    ) -> str:
        """Render every visible section; fall back to the core prompt."""
        sections = self.sections(scope=scope, revision=revision)
        if not sections:
            from deeppyro.prompt.prompt import build_system_prompt

            return build_system_prompt(tools, prompt_context)
        from deeppyro.prompt.prompt import render_sections

        ordered = [(section.order, section.name, section.renderer) for section in sections]
        return render_sections(tools, prompt_context, ordered)
