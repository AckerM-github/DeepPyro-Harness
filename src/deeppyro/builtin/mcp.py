"""Bundled MCP client plugin: connect configured servers and bridge their tools."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, ClassVar

from deeppyro.api import PluginConfig, PluginContext
from deeppyro.api.plugin import Service
from deeppyro.attachments import AttachmentStore
from deeppyro.core.bus import EventBus
from deeppyro.mcp.resources import resource_tools
from deeppyro.mcp.service import McpConfigError, McpService, parse_servers, servers_from_config
from deeppyro.mcp.types import McpServerConfig, McpServerStatus

#: Prompt section order: after the goal, before the skills catalogue.
PROMPT_ORDER = 40

RESOURCE_HINT = (
    "Use list_mcp_resources, list_mcp_resource_templates, or read_mcp_resource "
    "with one of these names as the server argument: {names}."
)


class McpPlugin(Service):
    """Connect to the configured MCP servers and expose their tools."""

    provide = "mcp"
    plugin_id = "mcp"
    version = "0.2.0"
    description = "MCP client: connects to configured servers and bridges their tools"
    inject: ClassVar[list[str]] = ["config", "tools", "prompt"]

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.logger = getattr(ctx, "logger", logging.getLogger("deeppyro.plugin.mcp"))
        self.service = McpService(
            parse_servers(servers_from_config(ctx, config), logger=self.logger),
            bus=_bus(ctx),
            logger=self.logger,
            attachments=_service(ctx),
        )

    async def setup(self) -> None:
        """Connect now; a failing server only logs a warning unless it is fatal."""

        self.ctx.effect(lambda: self._disposer(), "mcp: close servers")
        await self.service.start()
        if self.service.adapters() or self.service.server_names():
            self.ctx.register_tool(
                lambda _config, _workspace: [*self.service.adapters(), *resource_tools(self.service)]
            )
        register = getattr(self.ctx, "register_prompt_section", None)
        if self.service.server_names() and register is not None:
            register("mcp", PROMPT_ORDER, self._render)

    def _render(self, tools: Any, context: Any) -> str:
        names = self.service.server_names()
        if not names:
            return ""
        lines = [f"## MCP\n\n{RESOURCE_HINT.format(names=_json(names))}"]
        for server, instructions in self.service.instructions().items():
            lines.append(f"### {server}\n\n{instructions}")
        return "\n\n".join(lines)

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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _service(ctx: PluginContext) -> Any:
    try:
        return ctx.get(AttachmentStore)
    except Exception:  # noqa: BLE001 - the plugin still works without attachments
        return None


def _bus(ctx: PluginContext) -> Any:
    try:
        return ctx.get(EventBus)
    except Exception:  # noqa: BLE001 - the plugin still works without a bus
        return None


def _schedule_close(service: McpService) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(service.close())
    task.add_done_callback(lambda finished: finished.exception())


__all__ = ["PROMPT_ORDER", "McpConfigError", "McpPlugin"]
