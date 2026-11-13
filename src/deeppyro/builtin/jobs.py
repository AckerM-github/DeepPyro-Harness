"""Kernel jobs plugin: provides the jobs service and job control tools.

The runtime attaches the service to ToolContext.services so tools can start
and inspect background work. The plugin itself is stateless beyond the
registry; durable lifecycle records are appended through the record callback
that the starting tool passes in (normally the agent session log).
"""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.jobs import JobsService
from deeppyro.tools.builtin.jobs import job_tools


class JobsPlugin(Plugin):
    """Exposes the background job registry as the jobs service."""

    provide = "jobs"
    plugin_id = "jobs"
    version = "0.1.0"
    description = "background job registry and job control tools"
    inject: ClassVar[list[str]] = ["tools"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.service = JobsService()

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("jobs", self.service)
        ctx.register_tool(lambda _cfg, _workspace: job_tools())


__all__ = ["JobsPlugin", "JobsService"]
