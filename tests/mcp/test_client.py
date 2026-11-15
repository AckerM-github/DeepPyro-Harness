"""McpClient handshake, pagination and error propagation (no network)."""

from __future__ import annotations

import pytest

from deeppyro.mcp.client import McpClient
from deeppyro.mcp.jsonrpc import METHOD_NOT_FOUND, JsonRpcError, McpResponse
from deeppyro.mcp.transport import McpTimeoutError
from deeppyro.mcp.types import FALLBACK_PROTOCOL_VERSION, LATEST_PROTOCOL_VERSION, McpServerConfig


class FakeTransport:
    """In-memory transport: answers from a ``{method: handler}`` table."""

    def __init__(self, handlers: dict) -> None:
        self.handlers = handlers
        self.requests: list[dict] = []
        self.notifications: list[dict] = []
        self.started = 0
        self.closed = 0

    async def start(self) -> None:
        self.started += 1

    async def request(self, message: dict, *, timeout: float) -> McpResponse:
        self.requests.append(message)
        handler = self.handlers.get(message["method"])
        if handler is None:
            return McpResponse(id=message["id"], error=JsonRpcError(METHOD_NOT_FOUND, "method not found"))
        value = handler(message)
        if isinstance(value, BaseException):
            raise value
        return McpResponse(id=message["id"], result=value)

    async def notify(self, message: dict, *, timeout: float = 5.0) -> None:
        self.notifications.append(message)

    async def close(self) -> None:
        self.closed += 1


def config() -> McpServerConfig:
    return McpServerConfig(name="fake", command="fake-server")


def initialize_result(message: dict) -> dict:
    return {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "fake", "version": "1.0.0"},
    }


async def test_handshake_order_and_cached_tools() -> None:
    transport = FakeTransport(
        {
            "initialize": initialize_result,
            "tools/list": lambda message: {"tools": [{"name": "echo", "description": "Echo"}]},
        }
    )
    client = McpClient(config(), transport=transport)

    tools = await client.connect()

    assert transport.started == 1
    assert [message["method"] for message in transport.requests] == ["initialize", "tools/list"]
    assert transport.requests[0]["params"]["protocolVersion"] == LATEST_PROTOCOL_VERSION
    assert transport.requests[0]["params"]["capabilities"] == {}
    assert transport.requests[0]["params"]["clientInfo"]["name"] == "deeppyro"
    assert transport.notifications[0]["method"] == "notifications/initialized"
    assert [tool.name for tool in tools] == ["echo"]
    assert client.protocol_version == LATEST_PROTOCOL_VERSION
    assert client.server_info["name"] == "fake"
    assert client.connected


async def test_protocol_version_falls_back_when_rejected() -> None:
    def initialize(message: dict) -> dict:
        version = message["params"]["protocolVersion"]
        if version == LATEST_PROTOCOL_VERSION:
            return JsonRpcError(-32602, "unsupported protocol version")
        return {"protocolVersion": version, "serverInfo": {"name": "old"}}

    transport = FakeTransport({"initialize": initialize, "tools/list": lambda message: {"tools": []}})
    client = McpClient(config(), transport=transport)

    await client.connect()

    versions = [message["params"]["protocolVersion"] for message in transport.requests[:2]]
    assert versions == [LATEST_PROTOCOL_VERSION, FALLBACK_PROTOCOL_VERSION]
    assert client.protocol_version == FALLBACK_PROTOCOL_VERSION


async def test_tools_are_paginated() -> None:
    pages = {
        "": {"tools": [{"name": "one"}], "nextCursor": "page-2"},
        "page-2": {"tools": [{"name": "two"}, {"name": "three"}]},
    }

    def tools_list(message: dict) -> dict:
        return pages[message["params"].get("cursor", "")]

    transport = FakeTransport({"initialize": initialize_result, "tools/list": tools_list})
    client = McpClient(config(), transport=transport)

    tools = await client.connect()

    assert [tool.name for tool in tools] == ["one", "two", "three"]
    cursors = [
        message["params"].get("cursor") for message in transport.requests if message["method"] == "tools/list"
    ]
    assert cursors == [None, "page-2"]


async def test_input_schema_and_annotations_are_kept() -> None:
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    tool = {
        "name": "echo",
        "description": "Echo",
        "inputSchema": schema,
        "annotations": {"readOnlyHint": True},
    }
    transport = FakeTransport(
        {"initialize": initialize_result, "tools/list": lambda message: {"tools": [tool]}}
    )
    client = McpClient(config(), transport=transport)

    tools = await client.connect()

    assert tools[0].input_schema == schema
    assert tools[0].annotations == {"readOnlyHint": True}


async def test_call_tool_returns_the_raw_result() -> None:
    payload = {"content": [{"type": "text", "text": "hello"}], "isError": False}
    transport = FakeTransport(
        {
            "initialize": initialize_result,
            "tools/list": lambda message: {"tools": [{"name": "echo"}]},
            "tools/call": lambda message: payload,
        }
    )
    client = McpClient(config(), transport=transport)
    await client.connect()

    result = await client.call_tool("echo", {"text": "hi"})

    assert result == payload
    call = transport.requests[-1]
    assert call["params"] == {"name": "echo", "arguments": {"text": "hi"}}


async def test_transport_failures_propagate() -> None:
    transport = FakeTransport(
        {
            "initialize": initialize_result,
            "tools/list": lambda message: {"tools": [{"name": "echo"}]},
            "tools/call": lambda message: McpTimeoutError("no answer"),
        }
    )
    client = McpClient(config(), transport=transport)
    await client.connect()

    with pytest.raises(McpTimeoutError):
        await client.call_tool("echo")


async def test_close_releases_the_transport() -> None:
    transport = FakeTransport({"initialize": initialize_result, "tools/list": lambda message: {"tools": []}})
    client = McpClient(config(), transport=transport)
    await client.connect()
    await client.close()
    assert transport.closed == 1
    assert client.tools == []
