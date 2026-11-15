"""A minimal MCP client: initialize, list tools, call tools."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from typing import Any

import deeppyro

from .jsonrpc import (
    JsonRpcError,
    McpProtocolError,
    RequestIds,
    notification_message,
    request_message,
)
from .transport import McpTimeoutError, McpTransportError, make_transport
from .types import FALLBACK_PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION, McpServerConfig, McpToolInfo

#: Guard against a server that keeps returning the same ``nextCursor``.
MAX_TOOL_PAGES = 100


class McpClient:
    """One MCP session with one server."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        transport: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(f"deeppyro.mcp.{config.name}")
        self._transport = transport if transport is not None else make_transport(config, logger=self.logger)
        self._ids = RequestIds()
        self._tools: list[McpToolInfo] = []
        self.protocol_version: str = ""
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self.instructions: str = ""

    @property
    def tools(self) -> list[McpToolInfo]:
        return list(self._tools)

    @property
    def connected(self) -> bool:
        return bool(self.protocol_version)

    async def connect(self) -> list[McpToolInfo]:
        """Start the transport, negotiate the protocol and cache ``tools/list``."""

        await self._transport.start()
        try:
            result = await self._initialize(LATEST_PROTOCOL_VERSION)
        except JsonRpcError as exc:
            self.logger.debug(
                "mcp %s rejected protocol %s (%s); retrying with %s",
                self.config.name,
                LATEST_PROTOCOL_VERSION,
                exc,
                FALLBACK_PROTOCOL_VERSION,
            )
            result = await self._initialize(FALLBACK_PROTOCOL_VERSION)
        self.protocol_version = str(result.get("protocolVersion") or FALLBACK_PROTOCOL_VERSION)
        server_info = result.get("serverInfo")
        self.server_info = dict(server_info) if isinstance(server_info, dict) else {}
        capabilities = result.get("capabilities")
        self.capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
        instructions = result.get("instructions")
        self.instructions = instructions.strip() if isinstance(instructions, str) else ""
        await self._transport.notify(
            notification_message("notifications/initialized"),
            timeout=self.config.call_timeout,
        )
        self._tools = await self.list_tools()
        return self.tools

    async def _initialize(self, protocol_version: str) -> dict[str, Any]:
        params = {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "deeppyro", "version": deeppyro.__version__},
        }
        result = await self._request("initialize", params, timeout=self.config.startup_timeout)
        if not isinstance(result, dict):
            raise McpProtocolError("initialize must return an object")
        return result

    async def list_tools(self) -> list[McpToolInfo]:
        """Fetch every tool, following ``nextCursor`` pagination."""

        tools: list[McpToolInfo] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params, timeout=self.config.call_timeout)
            if not isinstance(result, dict):
                raise McpProtocolError("tools/list must return an object")
            raw_tools = result.get("tools")
            if isinstance(raw_tools, list):
                tools.extend(_tool_info(item) for item in raw_tools if _valid_tool(item))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return tools

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Invoke ``tools/call`` and return the raw result object."""

        params = {"name": name, "arguments": dict(arguments or {})}
        result = await self._request("tools/call", params, timeout=self.config.call_timeout)
        if not isinstance(result, dict):
            raise McpProtocolError("tools/call must return an object")
        return result

    @property
    def supports_resources(self) -> bool:
        """The server declared the ``resources`` capability."""

        return "resources" in self.capabilities

    async def list_resources(self, cursor: str | None = None) -> dict[str, Any]:
        """One page of ``resources/list`` (the protocol page, not every page)."""

        params: dict[str, Any] = {"cursor": cursor} if cursor else {}
        result = await self._request("resources/list", params, timeout=self.config.call_timeout)
        if not isinstance(result, dict):
            raise McpProtocolError("resources/list must return an object")
        return result

    async def list_resource_templates(self, cursor: str | None = None) -> dict[str, Any]:
        """One page of ``resources/templates/list``."""

        params: dict[str, Any] = {"cursor": cursor} if cursor else {}
        result = await self._request("resources/templates/list", params, timeout=self.config.call_timeout)
        if not isinstance(result, dict):
            raise McpProtocolError("resources/templates/list must return an object")
        return result

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """``resources/read`` for one URI."""

        result = await self._request("resources/read", {"uri": uri}, timeout=self.config.call_timeout)
        if not isinstance(result, dict):
            raise McpProtocolError("resources/read must return an object")
        return result

    async def _request(self, method: str, params: Any, *, timeout: float) -> Any:
        message = request_message(self._ids.next(), method, params)
        response = await self._transport.request(message, timeout=timeout)
        return response.unwrap()

    async def close(self) -> None:
        self._tools = []
        with contextlib.suppress(McpTransportError, Exception):
            await self._transport.close()


def _valid_tool(item: Any) -> bool:
    return isinstance(item, dict) and bool(str(item.get("name") or "").strip())


def _tool_info(item: dict[str, Any]) -> McpToolInfo:
    schema = item.get("inputSchema")
    annotations = item.get("annotations")
    return McpToolInfo(
        name=str(item.get("name") or ""),
        description=str(item.get("description") or ""),
        input_schema=dict(schema) if isinstance(schema, dict) else {},
        annotations=dict(annotations) if isinstance(annotations, dict) else None,
    )


__all__ = ["MAX_TOOL_PAGES", "McpClient", "McpTimeoutError"]
