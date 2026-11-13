"""Tests for the subagent kernel plugin wiring."""

from __future__ import annotations

from typing import Any

from deeppyro.builtin.subagent import SubagentPlugin
from deeppyro.kernel.manifest import resolve_plugin_name
from deeppyro.subagent import SubagentService
from deeppyro.tools.builtin.subagent import subagent_tools


class FakePluginContext:
    """Minimal PluginContext double for plugin setup."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.provided: dict[str, Any] = {}
        self.tool_factories: list[tuple[Any, Any]] = []

    def service(self, name: str) -> Any:
        return self._services[name]

    def provide(self, name: str, value: Any) -> None:
        self.provided[name] = value

    def register_tool(self, factory: Any, *, scope: Any = None) -> Any:
        self.tool_factories.append((factory, scope))
        return lambda: None


def make_context() -> FakePluginContext:
    return FakePluginContext(
        {
            "agents": object(),
            "sessions": object(),
            "sandbox": object(),
            "tools": object(),
        }
    )


def test_plugin_metadata() -> None:
    plugin = SubagentPlugin(make_context(), None)

    assert plugin.plugin_id == "subagent"
    assert plugin.version == "0.1.0"
    assert "subagent" in plugin.description
    assert plugin.inject == ["agents", "sessions", "sandbox", "tools"]


def test_plugin_provides_service_and_registers_tool() -> None:
    ctx = make_context()
    plugin = SubagentPlugin(ctx, None)

    plugin.setup(ctx, None)

    service = ctx.provided["subagent"]
    assert isinstance(service, SubagentService)
    assert plugin.service is service
    assert service.max_runs == 4
    assert len(ctx.tool_factories) == 1
    factory, scope = ctx.tool_factories[0]
    assert scope is None
    tools = factory(None, "C:/work")
    assert [tool.name for tool in tools] == ["subagent"]
    assert all(tool.parallel_safe is False for tool in tools)
    assert [tool.name for tool in subagent_tools()] == ["subagent"]


def test_plugin_target_is_importable() -> None:
    assert resolve_plugin_name("deeppyro.builtin.subagent:SubagentPlugin") is SubagentPlugin
