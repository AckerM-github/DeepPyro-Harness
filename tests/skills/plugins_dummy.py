"""Minimal tools/prompt services so the skills plugin can boot in isolation.

The real services are covered by tests/tools and tests/builtin; these fakes
only record the registrations the skills plugin makes, which keeps this test
independent of unrelated builtin modules.
"""

from __future__ import annotations

from typing import Any

from deeppyro.api.plugin import Service


class FakeToolsPlugin(Service):
    provide = "tools"
    plugin_id = "fake-tools"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.tool_factories: list[Any] = []

    def add(
        self,
        factory: Any,
        *,
        scope: str | None = None,
        revision: Any = None,
        plugin_id: str = "",
    ) -> Any:
        self.tool_factories.append((factory, scope, revision, plugin_id))
        return lambda: None


class FakePromptPlugin(Service):
    provide = "prompt"
    plugin_id = "fake-prompt"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.sections: list[tuple[str, int, Any, str]] = []

    def add_section(
        self,
        name: str,
        order: int,
        renderer: Any,
        *,
        plugin_id: str = "",
        scope: str | None = None,
        revision: Any = None,
    ) -> Any:
        self.sections.append((name, order, renderer, plugin_id))
        return lambda: None


__all__ = ["FakePromptPlugin", "FakeToolsPlugin"]
