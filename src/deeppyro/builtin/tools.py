"""The tools service and the built-in tool set plugin."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, ClassVar

from deeppyro.api import Plugin, Service, ToolContribution
from deeppyro.tools import ToolRegistry, builtin_tools


class ToolsPlugin(Service):
    """Catalog of tool factories contributed by plugins."""

    provide = "tools"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "tools"
    version = "0.1.0"
    description = "tool contribution registry"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        cfg = ctx.service("config").value
        self._config = cfg
        self._contributions: list[ToolContribution] = []

    def add(
        self,
        factory: Callable[[Any, Any], Any],
        *,
        scope: str | None = None,
        plugin_id: str = "",
    ) -> Callable[[], None]:
        """Register a tool factory; the returned callable removes it."""
        contribution = ToolContribution(factory=factory, scope=scope, plugin_id=plugin_id)
        self._contributions.append(contribution)

        def remove() -> None:
            try:
                self._contributions.remove(contribution)
            except ValueError:
                pass

        return remove

    def catalog(self) -> list[ToolContribution]:
        return list(self._contributions)

    def registry_for(self, workspace: str, *, scope: str | None = None) -> ToolRegistry:
        """Build a registry of every tool enabled for the given workspace."""
        tools: list[Any] = []
        for contribution in self._contributions:
            if contribution.scope is not None and contribution.scope != scope:
                continue
            produced = contribution.factory(self._config, workspace)
            if produced is None:
                continue
            if isinstance(produced, (list, tuple)):
                tools.extend(produced)
            else:
                tools.append(produced)
        return ToolRegistry(tools=tools)

    def names(self, workspace: str) -> list[str]:
        return self.registry_for(workspace).names()


class BuiltinToolsPlugin(Plugin):
    """Registers the eleven built-in tools on the tools service."""

    inject: ClassVar[list[str]] = ["tools"]
    plugin_id = "tools-builtin"
    version = "0.1.0"
    description = "built-in read/write/edit/glob/grep/shell/web/todo/present tools"

    def setup(self, ctx: Any, config: Any) -> None:
        def factory(cfg: Any, workspace: str) -> Iterable[Any]:
            return builtin_tools(cfg, workspace)

        ctx.register_tool(factory)
