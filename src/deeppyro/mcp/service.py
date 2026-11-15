"""Lifecycle for the configured MCP servers and the tools they expose."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .bridge import McpToolAdapter
from .client import McpClient
from .types import McpServerConfig, McpServerStatus


def parse_servers(raw: Any, *, logger: logging.Logger | None = None) -> list[McpServerConfig]:
    """Validate ``[[mcp.servers]]`` rows, skipping the ones that are malformed."""

    entries = _server_entries(raw)
    configs: list[McpServerConfig] = []
    for entry in entries:
        try:
            configs.append(McpServerConfig.model_validate(entry))
        except Exception as exc:  # noqa: BLE001 - one bad row must not disable the rest
            if logger is not None:
                logger.warning("mcp server configuration skipped: %s", exc)
    return configs


def _server_entries(raw: Any) -> list[Any]:
    if isinstance(raw, Mapping):
        servers = raw.get("servers")
        return list(servers) if isinstance(servers, (list, tuple)) else []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


class McpService:
    """Owns the MCP client sessions; one failing server never fails the rest."""

    def __init__(
        self,
        configs: Iterable[McpServerConfig],
        *,
        bus: Any = None,
        logger: logging.Logger | None = None,
        client_factory: Callable[[McpServerConfig], McpClient] | None = None,
    ) -> None:
        self._configs = list(configs)
        self._bus = bus
        self._logger = logger or logging.getLogger("deeppyro.mcp")
        self._client_factory = client_factory or self._default_client
        self._clients: dict[str, McpClient] = {}
        self._adapters: list[McpToolAdapter] = []
        self._status: dict[str, McpServerStatus] = {}
        self._names: set[str] = set()

    def _default_client(self, config: McpServerConfig) -> McpClient:
        return McpClient(config, logger=self._logger)

    @property
    def servers(self) -> list[McpServerConfig]:
        return list(self._configs)

    def adapters(self) -> list[McpToolAdapter]:
        return list(self._adapters)

    def status(self) -> list[McpServerStatus]:
        return [self._status[config.name] for config in self._configs if config.name in self._status]

    async def start(self) -> list[McpServerStatus]:
        """Connect every enabled server concurrently."""

        enabled = [config for config in self._configs if config.enabled]
        if enabled:
            await asyncio.gather(*(self._start_server(config) for config in enabled))
        return self.status()

    async def _start_server(self, config: McpServerConfig) -> None:
        client = self._client_factory(config)
        try:
            tools = await client.connect()
        except Exception as exc:  # noqa: BLE001 - a broken server must not break boot
            self._logger.warning("mcp server %s failed: %s", config.name, exc)
            self._status[config.name] = McpServerStatus(
                name=config.name,
                ok=False,
                error=str(exc) or type(exc).__name__,
            )
            with contextlib.suppress(Exception):
                await client.close()
            return
        adapters: list[McpToolAdapter] = []
        for tool in tools:
            adapter = McpToolAdapter(client, tool, server=config.name)
            if adapter.name in self._names:
                self._logger.warning(
                    "mcp tool %s collides with an already registered tool; skipped", adapter.name
                )
                continue
            self._names.add(adapter.name)
            adapters.append(adapter)
        self._clients[config.name] = client
        self._adapters.extend(adapters)
        self._status[config.name] = McpServerStatus(name=config.name, ok=True, tools=list(tools))
        await self._emit(
            "mcp/connected",
            {"server": config.name, "tools": [adapter.name for adapter in adapters]},
        )

    async def reload(self, configs: Iterable[McpServerConfig] | None = None) -> list[McpServerStatus]:
        """Drop every session and connect again (optionally with a new config list)."""

        await self.close()
        if configs is not None:
            self._configs = list(configs)
        return await self.start()

    async def close(self) -> None:
        for name, client in list(self._clients.items()):
            with contextlib.suppress(Exception):
                await client.close()
            await self._emit("mcp/disconnected", {"server": name, "tools": []})
        self._clients.clear()
        self._adapters.clear()
        self._names.clear()
        self._status.clear()

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._bus is None:
            return
        with contextlib.suppress(Exception):
            await self._bus.emit(event, payload)


def servers_from_config(ctx: Any, config: Any) -> Any:
    """Resolve the ``mcp`` table from the bundle row or the kernel config."""

    if isinstance(config, (list, tuple)) and config:
        return config
    if isinstance(config, Mapping) and config:
        return config
    try:
        service = ctx.service("config")
    except Exception:  # noqa: BLE001 - the plugin still loads without a config service
        return None
    value = getattr(service, "value", None)
    if value is None:
        return None
    return getattr(value, "mcp", None)


__all__: Sequence[str] = ["McpService", "parse_servers", "servers_from_config"]
