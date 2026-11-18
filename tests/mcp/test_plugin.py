"""The bundled McpPlugin wires the service into a plugin context."""

from __future__ import annotations

from typing import Any

from deeppyro.builtin.mcp import McpPlugin
from deeppyro.mcp.service import McpService
from deeppyro.mcp.types import McpServerConfig, McpToolInfo


class StubContext:
    """Just enough context for a Service plugin: provide, effect, register_tool."""

    def __init__(self) -> None:
        self.raw = self
        self.provided: list[tuple[str, Any]] = []
        self.registered: list[Any] = []
        self.effects: list[tuple[Any, str]] = []
        self.sections: list[tuple[str, int, Any]] = []

    def provide(self, name: str, value: Any = None, check: Any = None) -> Any:
        self.provided.append((name, value))
        return value

    def service(self, name: str) -> Any:
        raise KeyError(name)

    def register_prompt_section(self, name: str, order: int, renderer: Any, **kwargs: Any) -> None:
        self.sections.append((name, order, renderer))

    def get(self, name: str) -> Any:
        raise KeyError(name)

    def effect(self, execute: Any, label: str | None = None) -> Any:
        # Production semantics: record the teardown callback; never run it now.
        self.effects.append((execute, label or ""))
        return execute

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
    assert list(plugin.inject) == ["config", "tools", "prompt"]
    assert [name for name, _ in ctx.provided] == ["mcp"]


async def test_setup_registers_the_bridged_tools_and_exposes_status() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)
    clients: list[FakeClient] = []

    def factory(config: McpServerConfig) -> FakeClient:
        client = FakeClient(config)
        clients.append(client)
        return client

    plugin.service = McpService([McpServerConfig(name="fake", command="fake-server")], client_factory=factory)
    await plugin.setup()

    assert ctx.effects and ctx.effects[0][1] == "mcp: close servers"
    assert len(ctx.registered) == 1
    tools = ctx.registered[0](None, None)
    assert [tool.name for tool in tools] == [
        "mcp__fake__echo",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
    ]
    assert [status.name for status in plugin.status()] == ["fake"]
    assert plugin.status()[0].ok

    await plugin.close()
    assert clients[0].closed == 1


async def test_setup_with_no_servers_registers_nothing() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)
    await plugin.setup()

    assert ctx.registered == []
    assert plugin.status() == []


async def test_reload_replaces_the_adapters() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)
    plugin.service = McpService([], client_factory=FakeClient)
    await plugin.setup()

    statuses = await plugin.reload([McpServerConfig(name="later", command="fake-server")])

    assert [status.name for status in statuses] == ["later"]
    assert [adapter.name for adapter in plugin.service.adapters()] == ["mcp__later__echo"]
    await plugin.close()


async def test_the_prompt_section_names_the_servers_and_their_instructions() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)

    class WithInstructions(FakeClient):
        instructions = "Call the graph endpoint first."

    plugin.service = McpService(
        [McpServerConfig(name="docs", command="fake")], client_factory=WithInstructions
    )
    await plugin.setup()

    assert [name for name, _, _ in ctx.sections] == ["mcp"]
    _name, order, renderer = ctx.sections[0]
    text = renderer(None, None)
    assert order == 40
    assert '"docs"' in text
    assert "list_mcp_resources" in text
    assert "### docs" in text and "Call the graph endpoint first." in text
    await plugin.close()


async def test_no_servers_means_no_prompt_section() -> None:
    ctx = StubContext()
    plugin = McpPlugin(ctx, None)
    await plugin.setup()

    assert ctx.sections == []
