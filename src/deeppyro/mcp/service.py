"""Lifecycle for the configured MCP servers and the tools they expose."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .bridge import McpToolAdapter
from .client import McpClient
from .types import McpServerConfig, McpServerStatus, McpToolInfo


class McpConfigError(ValueError):
    """Two ``[[mcp.servers]]`` rows claim the same server name."""


def parse_servers(raw: Any, *, logger: logging.Logger | None = None) -> list[McpServerConfig]:
    """Validate ``[[mcp.servers]]`` rows, skipping the ones that are malformed."""

    entries = _server_entries(raw)
    configs: list[McpServerConfig] = []
    names: set[str] = set()
    for entry in entries:
        try:
            config = McpServerConfig.model_validate(entry)
        except Exception as exc:  # noqa: BLE001 - one bad row must not disable the rest
            if logger is not None:
                logger.warning("mcp server configuration skipped: %s", exc)
            continue
        if config.name in names:
            raise McpConfigError(f"mcp server name {config.name!r} is declared more than once")
        names.add(config.name)
        configs.append(config)
    return configs


def _server_entries(raw: Any) -> list[Any]:
    if isinstance(raw, Mapping):
        servers = raw.get("servers")
        return list(servers) if isinstance(servers, (list, tuple)) else []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


@dataclass(slots=True)
class _ServerState:
    """One configured server: its live client, its tool generation and its budget."""

    config: McpServerConfig
    client: McpClient | None = None
    tools: list[McpToolInfo] = field(default_factory=list)
    adapters: list[McpToolAdapter] = field(default_factory=list)
    instructions: str = ""
    error: str = ""
    reconnects: int = 0
    connected_at: float = 0.0
    task: asyncio.Task[Any] | None = None

    @property
    def ok(self) -> bool:
        return self.client is not None

    def status(self, *, reconnecting: bool = False) -> McpServerStatus:
        return McpServerStatus(
            name=self.config.name,
            ok=self.ok,
            error=self.error,
            tools=list(self.tools),
            instructions=self.instructions,
            resources=bool(self.client is not None and getattr(self.client, "supports_resources", False)),
            reconnecting=reconnecting,
        )


class McpService:
    """Owns the MCP client sessions; one failing server never fails the rest."""

    def __init__(
        self,
        configs: Iterable[McpServerConfig],
        *,
        bus: Any = None,
        logger: logging.Logger | None = None,
        client_factory: Callable[[McpServerConfig], McpClient] | None = None,
        attachments: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._configs = list(configs)
        names = [config.name for config in self._configs]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise McpConfigError(f"mcp server name {min(duplicates)!r} is declared more than once")
        self._bus = bus
        self._logger = logger or logging.getLogger("deeppyro.mcp")
        self._client_factory = client_factory or self._default_client
        self._attachments = attachments
        self._sleep = sleep or _sleep
        self._states: dict[str, _ServerState] = {
            config.name: _ServerState(config) for config in self._configs
        }
        self._closed = False

    def _default_client(self, config: McpServerConfig) -> McpClient:
        return McpClient(config, logger=self._logger)

    # -- introspection -----------------------------------------------------
    @property
    def servers(self) -> list[McpServerConfig]:
        return list(self._configs)

    def adapters(self) -> list[McpToolAdapter]:
        return [adapter for state in self._states.values() for adapter in state.adapters]

    def status(self) -> list[McpServerStatus]:
        if self._closed:
            return []
        return [
            self._states[config.name].status(reconnecting=self._reconnecting(self._states[config.name]))
            for config in self._configs
            if config.enabled
        ]

    def instructions(self) -> dict[str, str]:
        """Server instructions that should reach the system prompt."""

        return {
            name: state.instructions
            for name, state in self._states.items()
            if state.ok and state.instructions.strip()
        }

    def server_names(self) -> list[str]:
        return [config.name for config in self._configs if config.enabled]

    def state(self, name: str) -> _ServerState | None:
        return self._states.get(name)

    def _reconnecting(self, state: _ServerState) -> bool:
        return state.task is not None and not state.task.done()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> list[McpServerStatus]:
        """Connect every enabled server concurrently."""

        enabled = [self._states[config.name] for config in self._configs if config.enabled]
        if enabled:
            await asyncio.gather(*(self._connect(state, initial=True) for state in enabled))
        return self.status()

    async def _connect(self, state: _ServerState, *, initial: bool = False) -> bool:
        config = state.config
        client = self._client_factory(config)
        try:
            tools = await client.connect()
        except Exception as exc:
            state.error = str(exc) or type(exc).__name__
            self._logger.warning("mcp server %s failed: %s", config.name, state.error)
            with contextlib.suppress(Exception):
                await client.close()
            if initial and config.fail_on_startup_error:
                raise
            return False
        adapters = self._build_adapters(state, client, tools)
        if adapters is None:
            with contextlib.suppress(Exception):
                await client.close()
            return False
        previous = state.client
        if previous is not None and previous is not client:
            with contextlib.suppress(Exception):
                await previous.close()
        state.client = client
        state.tools = list(tools)
        state.adapters = adapters
        state.instructions = self._bounded_instructions(config, getattr(client, "instructions", ""))
        state.error = ""
        state.connected_at = time.monotonic()
        await self._emit(
            "mcp/connected",
            {"server": config.name, "tools": [adapter.name for adapter in adapters]},
        )
        return True

    def _bounded_instructions(self, config: McpServerConfig, instructions: str) -> str:
        if not instructions:
            return ""
        encoded = instructions.encode("utf-8")
        if len(encoded) <= config.max_instruction_bytes:
            return instructions
        self._logger.warning(
            "mcp server %s instructions are %d bytes, above maxInstructionBytes=%d; dropped",
            config.name,
            len(encoded),
            config.max_instruction_bytes,
        )
        return ""

    def _build_adapters(
        self, state: _ServerState, client: McpClient, tools: Sequence[McpToolInfo]
    ) -> list[McpToolAdapter] | None:
        """Build one generation of adapters, or reject it whole."""

        server = state.config.name
        taken = {
            adapter.name
            for other, other_state in self._states.items()
            if other != server
            for adapter in other_state.adapters
        }
        built: list[McpToolAdapter] = []
        seen: set[str] = set()
        for tool in tools:
            adapter = McpToolAdapter(
                client,
                tool,
                server=server,
                attachments=self._attachments,
                call=lambda name, args, _server=server: self.call_tool(_server, name, args),
            )
            if adapter.name in taken or adapter.name in seen:
                state.error = f"tool {adapter.name} collides with an already registered tool"
                self._logger.warning("mcp server %s: %s; generation rejected", server, state.error)
                return None
            seen.add(adapter.name)
            built.append(adapter)
        return built

    # -- calls -------------------------------------------------------------
    async def call_tool(
        self, server: str, name: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        state = self._states.get(server)
        if state is None:
            raise KeyError(f"unknown mcp server {server!r}")
        client = state.client
        if client is None:
            raise RuntimeError(f"mcp server {server!r} is not connected")
        try:
            return await client.call_tool(name, arguments)
        except Exception:
            self._mark_lost(state)
            raise

    async def list_resources(self, server: str, cursor: str | None = None) -> dict[str, Any]:
        return await self._resource_call(server, lambda client: client.list_resources(cursor))

    async def list_resource_templates(self, server: str, cursor: str | None = None) -> dict[str, Any]:
        return await self._resource_call(server, lambda client: client.list_resource_templates(cursor))

    async def read_resource(self, server: str, uri: str) -> dict[str, Any]:
        return await self._resource_call(server, lambda client: client.read_resource(uri))

    async def _resource_call(
        self, server: str, call: Callable[[McpClient], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        state = self._states.get(server)
        if state is None:
            raise KeyError(f"unknown mcp server {server!r}")
        client = state.client
        if client is None:
            raise RuntimeError(f"mcp server {server!r} is not connected")
        if not getattr(client, "supports_resources", False):
            return {"resources": []} if call.__name__ != "read" else {}
        try:
            return await call(client)
        except Exception:
            self._mark_lost(state)
            raise

    # -- recovery ----------------------------------------------------------
    def _mark_lost(self, state: _ServerState) -> None:
        if state.client is None:
            return
        self._logger.warning("mcp server %s connection lost", state.config.name)
        state.client = None
        state.error = state.error or "connection lost"
        if self._closed or not state.config.reconnect.enabled:
            return
        if self._reconnecting(state):
            return
        state.task = asyncio.get_running_loop().create_task(
            self._recover(state), name=f"mcp-reconnect-{state.config.name}"
        )

    async def _recover(self, state: _ServerState) -> None:
        reconnect = state.config.reconnect
        while not self._closed:
            if state.reconnects >= reconnect.max_attempts:
                state.adapters = []
                state.tools = []
                state.instructions = ""
                await self._emit("mcp/disconnected", {"server": state.config.name, "tools": []})
                self._logger.warning(
                    "mcp server %s: giving up after %d reconnect attempts",
                    state.config.name,
                    state.reconnects,
                )
                return
            state.reconnects += 1
            delay = (
                min(reconnect.initial_delay_ms * (2 ** (state.reconnects - 1)), reconnect.max_delay_ms) / 1000
            )
            await self._sleep(delay)
            if await self._connect(state):
                if time.monotonic() - state.connected_at >= reconnect.max_delay_ms / 1000:
                    state.reconnects = 0
                return

    # -- administration ----------------------------------------------------
    async def reload(self, configs: Iterable[McpServerConfig] | None = None) -> list[McpServerStatus]:
        """Drop every session and connect again (optionally with a new config list)."""

        await self.close()
        if configs is not None:
            self._configs = list(configs)
            self._states = {config.name: _ServerState(config) for config in self._configs}
        self._closed = False
        return await self.start()

    async def close(self) -> None:
        self._closed = True
        for name, state in list(self._states.items()):
            task = state.task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            state.task = None
            if state.client is not None:
                with contextlib.suppress(Exception):
                    await state.client.close()
                await self._emit("mcp/disconnected", {"server": name, "tools": []})
            state.client = None
            state.adapters = []
            state.tools = []
            state.instructions = ""

    async def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._bus is None:
            return
        with contextlib.suppress(Exception):
            await self._bus.emit(event, payload)


async def _sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def servers_from_config(ctx: Any, config: Any) -> Any:
    """Resolve the ``mcp`` table from the bundle row or the kernel config."""

    if isinstance(config, (list, tuple)) and config:
        return config
    if isinstance(config, Mapping) and config:
        return config
    try:
        from deeppyro.api.services import lookup_service
        from deeppyro.kernel.config_service import KernelConfig

        service = lookup_service(ctx, KernelConfig)
    except Exception:  # noqa: BLE001 - the plugin still loads without a config service
        return None
    value = getattr(service, "value", None)
    if value is None:
        return None
    return getattr(value, "mcp", None)


__all__: Sequence[str] = ["McpConfigError", "McpService", "parse_servers", "servers_from_config"]
