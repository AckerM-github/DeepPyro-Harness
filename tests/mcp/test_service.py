"""McpService lifecycle: partial failures, disabled rows, name clashes, bus events."""

from __future__ import annotations

import logging

from deeppyro.mcp.service import McpService, parse_servers
from deeppyro.mcp.types import McpServerConfig, McpToolInfo


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict, *, scope=None) -> None:
        self.events.append((name, payload))


class FakeClient:
    def __init__(self, config, *, tools=(), error=None) -> None:
        self.config = config
        self._tools = list(tools)
        self.error = error
        self.closed = 0
        self.connected = 0

    async def connect(self):
        if self.error is not None:
            raise self.error
        self.connected += 1
        return list(self._tools)

    async def call_tool(self, name, arguments=None):
        return {"content": [{"type": "text", "text": name}]}

    async def close(self):
        self.closed += 1


def server(name: str, **kwargs) -> McpServerConfig:
    return McpServerConfig(name=name, command="fake", **kwargs)


def tool(name: str) -> McpToolInfo:
    return McpToolInfo(name=name, description=f"{name} tool")


async def test_one_failing_server_does_not_hide_the_others() -> None:
    clients: dict[str, FakeClient] = {}

    def factory(config: McpServerConfig) -> FakeClient:
        if config.name == "broken":
            client = FakeClient(config, error=RuntimeError("handshake failed"))
        else:
            client = FakeClient(config, tools=[tool("echo")])
        clients[config.name] = client
        return client

    bus = FakeBus()
    service = McpService([server("good"), server("broken")], bus=bus, client_factory=factory)
    statuses = await service.start()

    assert [item.name for item in statuses] == ["good", "broken"]
    assert statuses[0].ok and [item.name for item in statuses[0].tools] == ["echo"]
    assert not statuses[1].ok and "handshake failed" in statuses[1].error
    assert clients["broken"].closed == 1
    assert [adapter.name for adapter in service.adapters()] == ["mcp__good__echo"]
    assert bus.events[0][0] == "mcp/connected"
    assert bus.events[0][1] == {"server": "good", "tools": ["mcp__good__echo"]}


async def test_disabled_servers_are_skipped() -> None:
    seen: list[str] = []

    def factory(config: McpServerConfig) -> FakeClient:
        seen.append(config.name)
        return FakeClient(config, tools=[tool("echo")])

    service = McpService([server("off", enabled=False), server("on")], client_factory=factory)
    statuses = await service.start()

    assert seen == ["on"]
    assert [item.name for item in statuses] == ["on"]


async def test_duplicate_tool_names_are_dropped() -> None:
    def factory(config: McpServerConfig) -> FakeClient:
        return FakeClient(config, tools=[tool("echo")])

    service = McpService([server("a b"), server("a_b")], client_factory=factory)
    statuses = await service.start()

    names = [adapter.name for adapter in service.adapters()]
    assert names == ["mcp__a_b__echo"]
    assert all(item.ok for item in statuses)


async def test_close_emits_disconnect_events() -> None:
    bus = FakeBus()
    clients: list[FakeClient] = []

    def factory(config: McpServerConfig) -> FakeClient:
        client = FakeClient(config, tools=[tool("echo")])
        clients.append(client)
        return client

    service = McpService([server("one")], bus=bus, client_factory=factory)
    await service.start()
    await service.close()

    assert clients[0].closed == 1
    assert [event for event, _ in bus.events] == ["mcp/connected", "mcp/disconnected"]
    assert service.adapters() == []
    assert service.status() == []


async def test_reload_connects_again() -> None:
    created: list[FakeClient] = []

    def factory(config: McpServerConfig) -> FakeClient:
        client = FakeClient(config, tools=[tool("echo")])
        created.append(client)
        return client

    service = McpService([server("one")], client_factory=factory)
    await service.start()
    statuses = await service.reload()

    assert len(created) == 2
    assert statuses[0].ok
    assert len(service.adapters()) == 1


def test_parse_servers_skips_invalid_rows(caplog) -> None:
    raw = {
        "servers": [
            {"name": "stdio", "command": "run-me"},
            {"name": "missing-command", "transport": "stdio"},
            {"name": "", "command": "x"},
            {"name": "remote", "transport": "http", "url": "https://example.test/mcp"},
            "not-a-table",
        ]
    }
    with caplog.at_level(logging.WARNING):
        configs = parse_servers(raw, logger=logging.getLogger("deeppyro.mcp"))

    assert [config.name for config in configs] == ["stdio", "remote"]
    assert "mcp server configuration skipped" in caplog.text


def test_parse_servers_accepts_a_bare_list_and_none() -> None:
    assert [config.name for config in parse_servers([{"name": "one", "command": "c"}])] == ["one"]
    assert parse_servers(None) == []
