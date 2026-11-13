"""Builtin working-directory plugin: provides the working_directory service.

The service owns the per-session committed cwd and emits durable
working-directory/change events plus one user-context notice. The host remains
responsible for binding record/notice callbacks per session and for injecting
the service into ToolContext.services.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.tools.builtin.working_directory import WorkingDirectoryTool
from deeppyro.working_directory import WorkingDirectoryService


def _service_config(config: Any) -> Any:
    if isinstance(config, Mapping):
        for key in ("working_directory", "workingDirectory"):
            value = config.get(key)
            if value is not None:
                return value
        return config
    for key in ("working_directory", "workingDirectory"):
        value = getattr(config, key, None)
        if value is not None:
            return value
    return config


class WorkingDirectoryPlugin(Plugin):
    """Expose the working-directory service and its tool."""

    provide = "working_directory"
    plugin_id = "working-directory"
    version = "0.1.0"
    description = "per-session committed working directory and the working_directory tool"
    inject: ClassVar[list[str]] = ["sessions", "tools"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.service = WorkingDirectoryService(_service_config(config))

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("working_directory", self.service)
        ctx.register_tool(lambda _cfg, _workspace: [WorkingDirectoryTool()])


__all__ = ["WorkingDirectoryPlugin", "WorkingDirectoryService"]
