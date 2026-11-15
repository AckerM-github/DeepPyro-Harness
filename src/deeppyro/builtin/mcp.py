"""Bundled MCP client plugin: connect configured servers and bridge their tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, ClassVar

from deeppyro.api.plugin import Service
from deeppyro.mcp.service import McpService, parse_servers, servers_from_config
from deeppyro.mcp.types import McpServerConfig, McpServerStatus


class McpPlugin(Service):
    """Connect to the configured MCP servers and expose their tools."""

    provide = "mcp"
    plugin_id = "mcp"
    version = "0.1.0"
    description = "MCP client: connects to configured servers and bridges their tools"
    inject: ClassVar[list[str]] = ["config", "tools"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.logger = getattr(ctx, "logger", logging.getLogger("deeppyro.plugin.mcp"))
        self.service = McpService(
            parse_servers(servers_from_config(ctx, config), logger=self.logger),
            bus=_bus(ctx),
            logger=self.logger,
        )

    async def setup(self, ctx: Any, config: Any) -> None:
        """Connect now; a failing server only logs a warning."""

        ctx.effect(lambda: self._disposer(), "mcp: close servers")
        await self.service.start()
        if self.service.adapters():
            ctx.register_tool(lambda _config, _workspace: list(self.service.adapters()))

    def _disposer(self) -> Any:
        def dispose() -> None:
            _schedule_close(self.service)

        return dispose

    def status(self) -> list[McpServerStatus]:
        return self.service.status()

    async def reload(self, configs: list[McpServerConfig] | None = None) -> list[McpServerStatus]:
        return await self.service.reload(configs)

    async def close(self) -> None:
        await self.service.close()


def _bus(ctx: Any) -> Any:
    try:
        return ctx.get("bus")
    except Exception:  # noqa: BLE001 - the plugin still works without a bus
        return None


def _schedule_close(service: McpService) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(service.close())
    task.add_done_callback(lambda finished: finished.exception())


__all__ = ["McpPlugin"]
