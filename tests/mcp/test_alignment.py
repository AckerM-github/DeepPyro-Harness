"""DSH alignment: config fields, identity naming, instructions, resources, recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext
from deeppyro.mcp.bridge import identity_hash, public_tool_name, render_resource_result
from deeppyro.mcp.resources import resource_tools
from deeppyro.mcp.service import McpConfigError, McpService, parse_servers
from deeppyro.mcp.transport import scrubbed_env
from deeppyro.mcp.types import McpServerConfig, McpServerStatus, McpToolInfo


class FakeClient:
    def __init__(self, config: McpServerConfig, *, tools=(), resources=False, instructions="", error=None):
        self.config = config
        self._tools = list(tools)
        self.capabilities = {"resources": {}} if resources else {}
        self.instructions = instructions
        self.error = error
        self.closed = 0
        self.calls: list[tuple[str, dict]] = []
        self.fail_next = False

    @property
    def supports_resources(self) -> bool:
        return "resources" in self.capabilities

    async def connect(self):
        if self.error is not None:
            raise self.error
        return list(self._tools)

    async def call_tool(self, name, arguments=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("socket closed")
        self.calls.append((name, dict(arguments or {})))
        return {"content": [{"type": "text", "text": name}]}

    async def list_resources(self, cursor=None):
        return {"resources": [{"uri": "file:///a.txt", "name": "a"}], "nextCursor": "n1"}

    async def list_resource_templates(self, cursor=None):
        return {"resourceTemplates": [{"uriTemplate": "file:///{path}", "name": "files"}]}

    async def read_resource(self, uri):
        return {"contents": [{"uri": uri, "mimeType": "text/plain", "text": "hi"}]}

    async def close(self):
        self.closed += 1


def config(name: str = "fake", **kwargs: Any) -> McpServerConfig:
    return McpServerConfig(name=name, command="fake-server", **kwargs)


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


# -- configuration ---------------------------------------------------------
def test_dsh_field_names_and_transport_spellings_are_accepted() -> None:
    stdio = McpServerConfig.model_validate({"serverName": "web", "command": "run", "toolCallTimeoutMs": 1500})
    assert stdio.name == "web" and stdio.call_timeout == 1.5
    http = McpServerConfig.model_validate(
        {"serverName": "api", "transport": "streamable-http", "url": "https://example.test/mcp"}
    )
    assert http.streamable_http and http.transport == "streamable-http"


@pytest.mark.parametrize("name", ["", "has space", "x" * 33, "bad/slash"])
def test_server_names_follow_the_dsh_pattern(name: str) -> None:
    with pytest.raises(ValueError):
        McpServerConfig(name=name, command="run")


def test_duplicate_server_names_fail_loudly() -> None:
    raw = {"servers": [{"name": "twice", "command": "a"}, {"name": "twice", "command": "b"}]}
    with pytest.raises(McpConfigError):
        parse_servers(raw)


async def test_fail_on_startup_error_rejects_the_plugin() -> None:
    def factory(cfg: McpServerConfig) -> FakeClient:
        return FakeClient(cfg, error=RuntimeError("handshake failed"))

    service = McpService([config("strict", fail_on_startup_error=True)], client_factory=factory)
    with pytest.raises(RuntimeError):
        await service.start()


# -- naming ----------------------------------------------------------------
def test_lossy_names_carry_a_stable_identity_hash() -> None:
    first = public_tool_name("web", "read page")
    assert first == public_tool_name("web", "read page")
    assert first.endswith(identity_hash("web", "read page"))
    assert first != public_tool_name("web", "read_page")
    assert public_tool_name("web", "read_page") == "mcp__web__read_page"


# -- environment -----------------------------------------------------------
def test_stdio_environment_drops_secrets_and_host_namespaces() -> None:
    import os

    os.environ["SOME_TOKEN"] = "x"
    os.environ["DSH_HOME"] = "y"
    os.environ["PLAIN"] = "z"
    env = scrubbed_env({"EXTRA": "1"})
    assert "SOME_TOKEN" not in env and "DSH_HOME" not in env
    assert env["PLAIN"] == "z" and env["EXTRA"] == "1"


# -- instructions ----------------------------------------------------------
async def test_instructions_reach_the_service_for_the_prompt() -> None:
    def factory(cfg: McpServerConfig) -> FakeClient:
        return FakeClient(cfg, instructions="Use the graph API first.")

    service = McpService([config("docs")], client_factory=factory)
    await service.start()

    assert service.instructions() == {"docs": "Use the graph API first."}


async def test_oversized_instructions_are_dropped() -> None:
    def factory(cfg: McpServerConfig) -> FakeClient:
        return FakeClient(cfg, instructions="x" * 64)

    service = McpService([config("docs", max_instruction_bytes=16)], client_factory=factory)
    await service.start()

    assert service.instructions() == {}


# -- resources -------------------------------------------------------------
async def test_resource_tools_read_through_the_service() -> None:
    def factory(cfg: McpServerConfig) -> FakeClient:
        return FakeClient(cfg, resources=True)

    service = McpService([config("docs")], client_factory=factory)
    await service.start()
    tools = {tool.name: tool for tool in resource_tools(service)}

    listed = await tools["list_mcp_resources"].run({"server": "docs"}, context())
    assert listed.text.startswith("MCP server: docs")
    assert json.loads(listed.text.split("\n", 1)[1])["nextCursor"] == "n1"

    read = await tools["read_mcp_resource"].run({"server": "docs", "uri": "file:///a.txt"}, context())
    assert read.text.startswith("MCP server: docs")
    assert "hi" in read.text


async def test_resource_tools_reject_unknown_servers() -> None:
    service = McpService([config("docs")], client_factory=FakeClient)
    await service.start()
    tools = {tool.name: tool for tool in resource_tools(service)}

    with pytest.raises(ToolError) as excinfo:
        await tools["read_mcp_resource"].run({"server": "nope", "uri": "x"}, context())
    assert "configured servers" in str(excinfo.value)


def test_resource_rendering_strips_binary_blobs() -> None:
    rendered = render_resource_result("docs", {"contents": [{"uri": "u", "blob": "AAAA"}]})
    assert "MCP server: docs" in rendered
    assert "[binary resource: 4 base64 characters; available to programmatic callers]" in rendered
    assert "AAAA" not in rendered


async def test_servers_without_the_resources_capability_answer_empty() -> None:
    service = McpService([config("plain")], client_factory=lambda cfg: FakeClient(cfg))
    await service.start()

    assert await service.list_resources("plain") == {"resources": []}


# -- recovery --------------------------------------------------------------
async def test_a_failed_call_starts_a_reconnect_with_backoff() -> None:
    delays: list[float] = []
    clients: list[FakeClient] = []

    def factory(cfg: McpServerConfig) -> FakeClient:
        client = FakeClient(cfg, tools=[McpToolInfo(name="echo")])
        clients.append(client)
        return client

    async def sleep(delay: float) -> None:
        delays.append(delay)

    service = McpService([config("flaky")], client_factory=factory, sleep=sleep)
    await service.start()
    clients[0].fail_next = True

    with pytest.raises(RuntimeError):
        await service.call_tool("flaky", "echo", {})

    for _ in range(20):
        await asyncio.sleep(0)
        if len(clients) > 1:
            break
    await asyncio.sleep(0)

    assert delays and delays[0] == pytest.approx(0.5)
    assert len(clients) == 2
    assert [adapter.name for adapter in service.adapters()] == ["mcp__flaky__echo"]


async def test_the_attempt_budget_removes_the_tools() -> None:
    clients: list[FakeClient] = []

    def factory(cfg: McpServerConfig) -> FakeClient:
        client = FakeClient(cfg, error=RuntimeError("still down"))
        clients.append(client)
        return client

    async def sleep(_delay: float) -> None:
        return None

    service = McpService(
        [config("dead", reconnect={"max_attempts": 2, "initial_delay_ms": 1, "max_delay_ms": 2})],
        client_factory=factory,
        sleep=sleep,
    )
    await service.start()
    state = service.state("dead")
    assert state is not None and not state.ok

    await service._recover(state)
    assert state.adapters == [] and state.tools == []
    assert service.status()[0].ok is False


async def test_disabled_reconnect_keeps_the_error_without_retrying() -> None:
    service = McpService(
        [config("quiet", reconnect={"enabled": False})],
        client_factory=lambda cfg: FakeClient(cfg, tools=[McpToolInfo(name="echo")]),
    )
    await service.start()
    state = service.state("quiet")
    assert state is not None
    service._mark_lost(state)

    assert state.client is None
    assert service.status()[0].reconnecting is False


def test_status_payload_is_json_friendly() -> None:
    status = McpServerStatus(name="docs", ok=True, instructions="hello", resources=True)
    payload = status.to_dict()
    assert payload["instructions"] == "hello" and payload["resources"] is True and payload["tools"] == []
