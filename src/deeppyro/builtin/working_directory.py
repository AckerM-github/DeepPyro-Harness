"""Builtin working-directory plugin: provides the working_directory service.

The service owns the per-session committed cwd and emits durable
working-directory/change events plus one user-context notice. The host remains
responsible for binding record/notice callbacks per session and for injecting
the service into ToolContext.services.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin, PluginConfig, PluginContext
from deeppyro.core.bus import EventBus
from deeppyro.tools.builtin.working_directory import WorkingDirectoryTool
from deeppyro.working_directory import WorkingDirectoryService


def _service_config(config: PluginConfig) -> Any:
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
    version = "0.2.0"
    description = "per-session committed working directory and the working_directory tool"
    inject: ClassVar[list[str]] = ["sessions", "tools"]

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.service = WorkingDirectoryService(_service_config(config))

    def _bus(self, ctx: PluginContext) -> Any | None:
        try:
            return ctx.get(EventBus)
        except Exception:  # noqa: BLE001 - the plugin still works without a bus
            return None

    def setup(self) -> None:
        bus = self._bus(self.ctx)
        if bus is not None:
            self.service.configure(emit=bus.emit)
        self.ctx.provide("working_directory", self.service)
        self.ctx.register_tool(lambda _cfg, _workspace: [WorkingDirectoryTool()])


__all__ = ["WorkingDirectoryPlugin", "WorkingDirectoryService"]
