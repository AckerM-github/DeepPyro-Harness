"""Kernel plugin exposing the default subagent service and its tool."""

from __future__ import annotations

from typing import ClassVar

from deeppyro.api import Plugin, PluginConfig, PluginContext
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.sandbox import SandboxPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.subagent.manager import SubagentService
from deeppyro.tools.builtin.subagent import subagent_tools


class SubagentPlugin(Plugin):
    """Provide the subagent service and register the agent-facing subagent tool."""

    plugin_id = "subagent"
    version = "0.1.0"
    description = "default subagent: one-shot child agents for self-contained tasks"
    inject: ClassVar[list[str]] = ["agents", "sessions", "sandbox", "tools"]

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.service: SubagentService | None = None

    def setup(self) -> None:
        service = SubagentService(
            agents=self.ctx.require(AgentsPlugin),
            sessions=self.ctx.require(SessionsPlugin),
            sandbox=self.ctx.require(SandboxPlugin),
        )
        self.service = service
        self.ctx.provide("subagent", service)
        self.ctx.register_tool(lambda _config, _workspace: subagent_tools())


__all__ = ["SubagentPlugin"]
