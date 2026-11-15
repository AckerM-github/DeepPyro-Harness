"""MCP tool adapters: naming, schema passthrough, content flattening."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext
from deeppyro.mcp.bridge import McpToolAdapter, flatten_content, mcp_tool_name, sanitize_name
from deeppyro.mcp.jsonrpc import JsonRpcError
from deeppyro.mcp.transport import McpTransportError
from deeppyro.mcp.types import McpServerConfig, McpToolInfo
from deeppyro.tools.base import make_parameters


class FakeClient:
    def __init__(self, result=None, *, error=None) -> None:
        self.config = McpServerConfig(name="my server", command="fake")
        self.result = result or {"content": []}
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if self.error is not None:
            raise self.error
        return self.result


def context() -> ToolContext:
    return ToolContext(
        cwd=Path("."),
        session_id="s",
        turn=1,
        step=1,
        call_id="c",
        cancel=asyncio.Event(),
        log=lambda *args: None,
    )


async def test_names_are_sanitized() -> None:
    assert sanitize_name("my server!") == "my_server_"
    assert sanitize_name("   ") == "tool"
    assert mcp_tool_name("my server", "do.thing/x") == "mcp__my_server__do_thing_x"


async def test_description_fallback_and_schema_passthrough() -> None:
    schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    adapter = McpToolAdapter(FakeClient(), McpToolInfo(name="echo", input_schema=schema))
    assert adapter.name == "mcp__my_server__echo"
    assert adapter.description == "echo from MCP server my server"
    assert adapter.parameters == schema


async def test_missing_schema_becomes_an_empty_object() -> None:
    adapter = McpToolAdapter(FakeClient(), McpToolInfo(name="noargs"))
    assert adapter.parameters == make_parameters({})


async def test_content_is_flattened_for_the_model() -> None:
    result = {
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "image", "mimeType": "image/png", "data": "..."},
            {"type": "resource", "resource": {"uri": "file:///tmp/a.txt"}},
        ]
    }
    adapter = McpToolAdapter(FakeClient(result), McpToolInfo(name="mixed"))
    out = await adapter.run({}, context())
    assert "hello" in out.text
    assert "[image: image/png]" in out.text
    assert "[resource: file:///tmp/a.txt]" in out.text
    assert out.meta == {"mcp": {"server": "my server", "tool": "mixed"}}


async def test_arguments_are_forwarded() -> None:
    client = FakeClient({"content": [{"type": "text", "text": "ok"}]})
    adapter = McpToolAdapter(client, McpToolInfo(name="echo"))
    await adapter.run({"text": "hi"}, context())
    assert client.calls == [("echo", {"text": "hi"})]


async def test_is_error_raises_a_tool_error() -> None:
    client = FakeClient({"content": [{"type": "text", "text": "kaput"}], "isError": True})
    adapter = McpToolAdapter(client, McpToolInfo(name="boom"))
    with pytest.raises(ToolError) as excinfo:
        await adapter.run({}, context())
    assert "kaput" in str(excinfo.value)


async def test_transport_errors_become_tool_errors() -> None:
    adapter = McpToolAdapter(FakeClient(error=McpTransportError("down")), McpToolInfo(name="echo"))
    with pytest.raises(ToolError) as excinfo:
        await adapter.run({}, context())
    assert "down" in str(excinfo.value)


async def test_json_rpc_errors_become_tool_errors() -> None:
    adapter = McpToolAdapter(FakeClient(error=JsonRpcError(-32603, "inner")), McpToolInfo(name="echo"))
    with pytest.raises(ToolError) as excinfo:
        await adapter.run({}, context())
    assert "inner" in str(excinfo.value)


async def test_flatten_content_edge_cases() -> None:
    assert flatten_content({}) == "(no content)"
    assert flatten_content({"content": []}) == "(no content)"
    assert flatten_content({"content": [{"type": "audio"}]}) == "[audio content]"
    assert flatten_content({"content": [{"type": "weird"}]}) == "[weird content]"
