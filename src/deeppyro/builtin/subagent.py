"""Kernel plugin exposing the default subagent service and its tool."""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.subagent.manager import SubagentService
from deeppyro.tools.builtin.subagent import subagent_tools


class SubagentPlugin(Plugin):
    """Provide the subagent service and register the agent-facing subagent tool."""

    plugin_id = "subagent"
    version = "0.1.0"
    description = "default subagent: one-shot child agents for self-contained tasks"
    inject: ClassVar[list[str]] = ["agents", "sessions", "sandbox", "tools"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.service: SubagentService | None = None

    def setup(self, ctx: Any, config: Any) -> None:
        service = SubagentService(
            agents=ctx.service("agents"),
            sessions=ctx.service("sessions"),
            sandbox=ctx.service("sandbox"),
        )
        self.service = service
        ctx.provide("subagent", service)
        ctx.register_tool(lambda _config, _workspace: subagent_tools())


__all__ = ["SubagentPlugin"]
