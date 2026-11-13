"""Builtin hooks plugin: provides the runtime hook service as service 'hooks'."""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Plugin
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
        self.service = HookService()

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("hooks", self.service)


__all__ = ["HookService", "HooksPlugin"]
