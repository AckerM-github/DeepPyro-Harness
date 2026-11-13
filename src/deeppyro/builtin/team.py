"""Kernel plugin exposing the lead agent team and its tools."""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import TEAM_PRESET_ID, Plugin
from deeppyro.team.manager import TeamService
from deeppyro.tools.builtin.team import team_tools


class TeamPlugin(Plugin):
    """Provide the team service and register the agent-facing team tools."""

    plugin_id = "team"
    version = "0.1.0"
    description = "agent team: child members, messaging and the shared task board"
    inject: ClassVar[list[str]] = ["agents", "sessions", "sandbox", "tools"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.service: TeamService | None = None

    def setup(self, ctx: Any, config: Any) -> None:
        service = TeamService(
            agents=ctx.service("agents"),
            sessions=ctx.service("sessions"),
            sandbox=ctx.service("sandbox"),
        )
        self.service = service
        ctx.provide("team", service)
        # Team tools are part of the explicit team mode only: scoping the
        # contribution to the team preset keeps them out of a default-mode
        # agent's tool registry (and out of the host-wide tool list).
        ctx.register_tool(lambda _config, _workspace: team_tools(), scope=TEAM_PRESET_ID)


__all__ = ["TeamPlugin"]
