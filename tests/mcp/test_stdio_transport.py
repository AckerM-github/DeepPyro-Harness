"""Stdio transport against a real (fake) MCP server subprocess."""

from __future__ import annotations

import sys

import pytest

from deeppyro.mcp.client import McpClient
from deeppyro.mcp.jsonrpc import notification_message, request_message
from deeppyro.mcp.transport import McpTimeoutError, McpTransportError, StdioTransport
from deeppyro.mcp.types import McpServerConfig

SERVER = r"""
import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the arguments back",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {
        "name": "add",
        "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    },
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if MODE == "crash":
        sys.exit(3)
    if MODE == "slow":
        time.sleep(5)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1.0.0"},
                },
            }
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        arguments = message.get("params", {}).get("arguments", {})
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": json.dumps(arguments)}], "isError": False},
            }
        )
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
"""


def config(mode: str = "normal") -> McpServerConfig:
    return McpServerConfig(name="fake", command=sys.executable, args=["-c", SERVER, mode])


async def test_request_and_notification_round_trip() -> None:
    transport = StdioTransport(config())
    await transport.start()
    try:
        response = await transport.request(
            request_message(1, "initialize", {"protocolVersion": "2025-06-18"}), timeout=10
        )
        assert response.unwrap()["serverInfo"]["name"] == "fake"
        await transport.notify(notification_message("notifications/initialized"))
        tools = await transport.request(request_message(2, "tools/list"), timeout=10)
        assert [tool["name"] for tool in tools.unwrap()["tools"]] == ["echo", "add"]
        called = await transport.request(
            request_message(3, "tools/call", {"name": "echo", "arguments": {"text": "hi"}}), timeout=10
        )
        assert called.unwrap()["content"][0]["text"] == '{"text": "hi"}'
    finally:
        await transport.close()


async def test_error_response_is_returned_not_raised() -> None:
    transport = StdioTransport(config())
    await transport.start()
    try:
        response = await transport.request(request_message(1, "nope"), timeout=10)
        assert not response.ok
        assert response.error is not None and response.error.code == -32601
    finally:
        await transport.close()


async def test_pending_request_fails_when_the_server_exits() -> None:
    transport = StdioTransport(config("crash"))
    await transport.start()
    try:
        with pytest.raises(McpTransportError):
            await transport.request(request_message(1, "initialize"), timeout=10)
    finally:
        await transport.close()


async def test_slow_server_times_out() -> None:
    transport = StdioTransport(config("slow"))
    await transport.start()
    try:
        with pytest.raises(McpTimeoutError):
            await transport.request(request_message(1, "initialize"), timeout=0.2)
    finally:
        await transport.close()


async def test_missing_executable_is_a_transport_error() -> None:
    transport = StdioTransport(McpServerConfig(name="gone", command="dpyro-im-missing-binary"))
    with pytest.raises(McpTransportError):
        await transport.start()
    await transport.close()


async def test_client_handshake_over_stdio() -> None:
    client = McpClient(config())
    try:
        tools = await client.connect()
        assert [tool.name for tool in tools] == ["echo", "add"]
        assert client.protocol_version == "2025-06-18"
        assert client.server_info["name"] == "fake"
        result = await client.call_tool("echo", {"text": "hello"})
        assert result["content"][0]["text"] == '{"text": "hello"}'
    finally:
        await client.close()
