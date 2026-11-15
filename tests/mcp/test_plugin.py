"""The bundled McpPlugin wires the service into a plugin context."""

from __future__ import annotations

from typing import Any

from deeppyro.builtin.mcp import McpPlugin
from deeppyro.mcp.service import McpService
from deeppyro.mcp.types import McpServerConfig, McpToolInfo


class StubReflect:
    def __init__(self) -> None:
        self.provided: list[tuple[str, Any]] = []

    def provide(self, name: str, service: Any, check: Any = None) -> None:
        self.provided.append((name, service))


class StubContext:
    """Just enough context for a Service plugin: reflect.provide, effect, register_tool."""

    def __init__(self) -> None:
        self.raw = self
        self.reflect = StubReflect()
        self.registered: list[Any] = []
        self.effects: list[tuple[Any, str]] = []

    def get(self, name: str) -> Any:
        raise KeyError(name)

    def effect(self, execute: Any, label: str | None = None) -> Any:
        self.effects.append((execute, label or ""))
        return execute()

    def register_tool(self, factory: Any, **kwargs: Any) -> None:
        self.registered.append(factory)


class FakeClient:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.closed = 0

    async def connect(self):
        return [McpToolInfo(name="echo", description="Echo")]

    async def call_tool(self, name, arguments=None):
        return {"content": [{"type": "text", "text": name}]}

    async def close(self) -> None:
        self.closed += 1


async def test_plugin_declares_the_service_and_injects_config_and_tools() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)

    assert plugin.provide == "mcp"
    assert plugin.plugin_id == "mcp"
    assert list(plugin.inject) == ["config", "tools"]
    assert [name for name, _ in ctx.reflect.provided] == ["mcp"]


async def test_setup_registers_the_bridged_tools_and_exposes_status() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)
    clients: list[FakeClient] = []

    def factory(config: McpServerConfig) -> FakeClient:
        client = FakeClient(config)
        clients.append(client)
        return client

    plugin.service = McpService([McpServerConfig(name="fake", command="fake-server")], client_factory=factory)
    await plugin.setup(ctx, None)

    assert ctx.effects and ctx.effects[0][1] == "mcp: close servers"
    assert len(ctx.registered) == 1
    tools = ctx.registered[0](None, None)
    assert [tool.name for tool in tools] == ["mcp__fake__echo"]
    assert [status.name for status in plugin.status()] == ["fake"]
    assert plugin.status()[0].ok

    await plugin.close()
    assert clients[0].closed == 1


async def test_setup_with_no_servers_registers_nothing() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)
    await plugin.setup(ctx, None)

    assert ctx.registered == []
    assert plugin.status() == []


async def test_reload_replaces_the_adapters() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)
    plugin.service = McpService([], client_factory=FakeClient)
    await plugin.setup(ctx, None)

    statuses = await plugin.reload([McpServerConfig(name="later", command="fake-server")])

    assert [status.name for status in statuses] == ["later"]
    assert [adapter.name for adapter in plugin.service.adapters()] == ["mcp__later__echo"]
    await plugin.close()
