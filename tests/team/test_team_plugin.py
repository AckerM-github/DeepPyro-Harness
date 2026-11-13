"""Tests for the team kernel plugin wiring."""

from __future__ import annotations

from typing import Any

from deeppyro.builtin.team import TeamPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import PluginSpec, boot
from deeppyro.team import TeamService
from deeppyro.tools.builtin.team import team_tools

EXPECTED_TOOLS = [
    "team_spawn",
    "team_send",
    "team_wait",
    "team_cancel",
    "team_members",
    "team_task_create",
    "team_task_list",
    "team_task_get",
    "team_task_update",
]


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


def test_plugin_provides_team_service_and_registers_tools() -> None:
    ctx = FakePluginContext(
        {"agents": object(), "sessions": object(), "sandbox": object(), "tools": object()}
    )
    plugin = TeamPlugin(ctx, None)
    assert plugin.plugin_id == "team"
    assert plugin.inject == ["agents", "sessions", "sandbox", "tools"]

    plugin.setup(ctx, None)

    service = ctx.provided["team"]
    assert isinstance(service, TeamService)
    assert plugin.service is service
    assert len(ctx.tool_factories) == 1
    factory, scope = ctx.tool_factories[0]
    assert scope is None
    tools = factory(None, "C:/work")
    assert [tool.name for tool in tools] == EXPECTED_TOOLS
    assert all(tool.parallel_safe is False for tool in tools)
    assert [tool.name for tool in team_tools()] == EXPECTED_TOOLS


async def test_team_plugin_boots_in_kernel() -> None:
    config = Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )
    spec = PluginSpec(id="team", name="deeppyro.builtin.team:TeamPlugin", after="agents")
    app = await boot(config, extra_plugins=[spec])
    try:
        assert "team" in app.started
        service = app.service("team")
        assert isinstance(service, TeamService)
        names = app.service("tools").names("C:/work")
        assert [name for name in names if name.startswith("team_")] == EXPECTED_TOOLS
    finally:
        await app.stop()
