"""The tools service and the built-in tool set plugin."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

from deeppyro.api import Plugin, Service, ToolContribution
from deeppyro.tools import ToolRegistry, builtin_tools

logger = logging.getLogger("deeppyro.tools")


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
        revision: int | None = None,
    ) -> Callable[[], None]:
        """Register a tool factory; the returned callable removes it.

        A non-None scope reserves the tools for one agent preset. A non-None
        revision pins them to one immutable preset revision; None keeps the
        legacy behaviour of matching every revision of the scope.
        """
        contribution = ToolContribution(
            factory=factory,
            scope=scope,
            plugin_id=plugin_id,
            revision=revision,
        )
        self._contributions.append(contribution)

        def remove() -> None:
            try:
                self._contributions.remove(contribution)
            except ValueError:
                pass

        return remove

    def catalog(self) -> list[ToolContribution]:
        return list(self._contributions)

    @staticmethod
    def _visible(contribution: ToolContribution, scope: str | None, revision: int | None) -> bool:
        """Whether a contribution is part of the composition for scope/revision.

        Global contributions are always visible. Scoped contributions are
        visible only inside their preset; a revision-pinned contribution needs
        an exact revision match, while a revision-less one keeps matching every
        revision (backward compatibility for plugins written before revisions).
        """
        if contribution.scope is None:
            return True
        if contribution.scope != scope:
            return False
        if contribution.revision is None:
            return True
        return revision is not None and contribution.revision == revision

    def registry_for(
        self,
        workspace: str,
        *,
        scope: str | None = None,
        revision: int | None = None,
    ) -> ToolRegistry:
        """Build a registry of every tool enabled for the given composition.

        Duplicate tool names are tolerated: the first registration wins, so a
        preset realm tool can intentionally shadow a host tool (or vice versa)
        without breaking the whole registry.
        """
        tools: list[Any] = []
        seen: set[str] = set()
        for contribution in self._contributions:
            if not self._visible(contribution, scope, revision):
                continue
            produced = contribution.factory(self._config, workspace)
            if produced is None:
                continue
            candidates = produced if isinstance(produced, (list, tuple)) else [produced]
            for tool in candidates:
                name = getattr(tool, "name", "")
                if name and name in seen:
                    logger.debug(
                        "ignoring duplicate tool %r from plugin %r",
                        name,
                        contribution.plugin_id,
                    )
                    continue
                if name:
                    seen.add(name)
                tools.append(tool)
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
