"""Builtin hooks plugin: provides the runtime hook service as service 'hooks'."""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.core.bus import EventBus
from deeppyro.hooks import HookService


class HooksPlugin(Plugin):
    """Expose one HookService so the agent loop and tool pipeline share listeners."""

    provide = "hooks"
    plugin_id = "hooks"
    version = "0.1.0"
    description = "runtime hook service for agent and tool pipeline interception"
    inject: ClassVar[list[str]] = []

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.bus = EventBus()
        self.service = HookService(self.bus)

    def setup(self, ctx: Any, config: Any) -> None:
        # One registry, two doors: the bus is name-agnostic (any catalog name),
        # the facade keeps the historical hook surface for in-tree subscribers.
        ctx.provide("bus", self.bus)
        ctx.provide("hooks", self.service)


__all__ = ["HookService", "HooksPlugin"]
