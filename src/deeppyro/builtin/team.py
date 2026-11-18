"""Kernel plugin exposing the lead agent team and its tools."""

from __future__ import annotations

from typing import ClassVar

from deeppyro.api import TEAM_PRESET_ID, Plugin, PluginConfig, PluginContext
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.sandbox import SandboxPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.team.manager import TeamService
from deeppyro.tools.builtin.team import team_tools


class TeamPlugin(Plugin):
    """Provide the team service and register the agent-facing team tools."""

    plugin_id = "team"
    version = "0.3.0"
    description = "agent team: child members, messaging and the shared task board"
    inject: ClassVar[list[str]] = ["agents", "sessions", "sandbox", "tools"]

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.service: TeamService | None = None

    def setup(self) -> None:
        service = TeamService(
            agents=self.ctx.require(AgentsPlugin),
            sessions=self.ctx.require(SessionsPlugin),
            sandbox=self.ctx.require(SandboxPlugin),
        )
        self.service = service
        self.ctx.provide("team", service)
        # Team tools are part of the explicit team mode only: scoping the
        # contribution to the team preset keeps them out of a default-mode
        # agent's tool registry (and out of the host-wide tool list).
        self.ctx.register_tool(lambda _config, _workspace: team_tools(), scope=TEAM_PRESET_ID)


__all__ = ["TeamPlugin"]
