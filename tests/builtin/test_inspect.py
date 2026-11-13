"""Boot-level tests for the inspect service (Layer D)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel.app import App
from deeppyro.kernel.bundle import boot
from deeppyro.kernel.manifest import PluginSpec
from deeppyro.plugins import save_user_manifest

DEMO_TARGET = "tests.builtin.demo_plugins:DemoPlugin"
SETTINGS_TARGET = "deeppyro.builtin.settings:SettingsPlugin"
INSPECT_TARGET = "deeppyro.builtin.inspect:InspectPlugin"
TOOLS_TARGET = "deeppyro.tools.builtin.plugin_tools:PluginToolsPlugin"

BROKEN_BUNDLE = """
[bundle]
id = "broken"
name = "Broken Bundle"
version = "0.1.0"
api_version = 1
order = 900
critical = false

[[plugins]]
id = "exploding"
plugin = "tests.builtin.demo_plugins:ExplodingPlugin"
"""


def make_config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )


@pytest.fixture()
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[App]:
    home = tmp_path / "home"
    monkeypatch.setenv("DEEPPYRO_HOME", str(home))
    save_user_manifest(
        home / "plugins.toml",
        [PluginSpec(id="demo-plugin", name=DEMO_TARGET, config={"apiKey": "hunter2"})],
    )
    broken = home / "bundles" / "broken"
    broken.mkdir(parents=True)
    (broken / "bundle.toml").write_text(BROKEN_BUNDLE, encoding="utf-8")
    kernel = await boot(
        make_config(),
        extra_plugins=[
            PluginSpec(id="settings", name=SETTINGS_TARGET),
            PluginSpec(id="inspect", name=INSPECT_TARGET),
            PluginSpec(id="plugin-tools", name=TOOLS_TARGET),
        ],
    )
    try:
        yield kernel
    finally:
        await kernel.stop()


async def test_services_reports_live_services(app: App) -> None:
    rows = app.service("inspect").services()
    by_name = {row["name"]: row for row in rows}

    assert {"config", "llm", "sessions", "sandbox", "tools", "prompt", "commands", "agents"} <= set(by_name)
    assert {"settings", "inspect"} <= set(by_name)
    assert by_name["config"]["state"] == "active"
    assert by_name["llm"]["provider"] == "deeppyro.builtin.llm.LlmPlugin"
    assert by_name["inspect"]["provider"].endswith("InspectPlugin")


async def test_plugins_joins_composition_and_runtime(app: App) -> None:
    rows = app.service("inspect").plugins()
    by_id = {row["id"]: row for row in rows}

    assert {"llm", "agents", "tools-builtin", "settings", "inspect"} <= set(by_id)
    assert by_id["llm"]["state"] == "active"
    assert by_id["llm"]["critical"] is True
    assert by_id["settings"]["state"] == "active"
    assert by_id["demo-plugin"]["state"] == "active"
    exploding = by_id["exploding"]
    assert exploding["state"] == "failed"
    assert "exploding plugin failed on purpose" in exploding["error"]


async def test_tools_lists_builtin_and_contributed_tools(app: App, tmp_path: Path) -> None:
    rows = app.service("inspect").tools(tmp_path)
    names = {row["name"] for row in rows}

    assert {"read", "write", "edit", "glob", "grep", "pwsh"} <= names
    assert {"plugin_manager", "plugin_inspect", "settings_read", "settings_write"} <= names
    plugin_manager = next(row for row in rows if row["name"] == "plugin_manager")
    assert plugin_manager["plugin_id"] == "plugin-tools"
    read = next(row for row in rows if row["name"] == "read")
    assert read["plugin_id"] == "tools-builtin"


async def test_prompt_sections_commands_and_providers(app: App) -> None:
    inspect = app.service("inspect")

    sections = inspect.prompt_sections()
    names = [section["name"] for section in sections]
    assert "identity" in names
    assert [section["order"] for section in sections] == sorted(section["order"] for section in sections)

    commands = {row["name"]: row for row in inspect.commands()}
    assert {"help", "new", "model", "plugins"} <= set(commands)
    assert commands["help"]["aliases"] == ["?", "h"]

    providers = {row["name"]: row for row in inspect.providers()}
    assert "local" in providers


async def test_config_schema_for_plugins(app: App) -> None:
    inspect = app.service("inspect")

    demo = inspect.config_schema("demo-plugin")
    assert demo["target"] == DEMO_TARGET
    assert "apiKey" in demo["schema"]["properties"]
    assert demo["apply"] == "next-boot"

    llm = inspect.config_schema("llm")
    assert llm["target"] == "deeppyro.builtin.llm:LlmPlugin"
    assert llm["schema"] is None


async def test_config_schema_reports_volatile_fields(app: App) -> None:
    demo = app.service("inspect").config_schema("demo-plugin")

    assert demo["volatile_fields"] == ["temperature"]


async def test_events_lists_wire_record_types(app: App) -> None:
    from deeppyro.core.wire import MESSAGE_PROJECTION_TYPES, RECORD_TYPES, SURFACE_EVENT_TYPES

    rows = app.service("inspect").events()
    types = [row["type"] for row in rows]

    assert len(types) == len(set(types))
    assert set(RECORD_TYPES) <= set(types)
    assert all(set(row) == {"type", "kind", "surface", "projection"} for row in rows)
    tool_result = next(row for row in rows if row["type"] == "tool/result")
    assert tool_result["kind"] == "tool"
    assert tool_result["surface"] is True
    assert tool_result["projection"] is False
    for record_type in SURFACE_EVENT_TYPES:
        assert next(row for row in rows if row["type"] == record_type)["surface"] is True
    for record_type in MESSAGE_PROJECTION_TYPES:
        assert next(row for row in rows if row["type"] == record_type)["projection"] is True


async def test_slots_and_theme_are_optional(app: App) -> None:
    inspect = app.service("inspect")

    slots = inspect.slots()
    assert isinstance(slots, list)
    assert all({"kind", "id", "plugin_id", "order", "title"} == set(row) for row in slots)

    theme = inspect.theme()
    assert isinstance(theme["tokens"], dict)
    assert isinstance(theme["note"], str)


async def test_guide_covers_extension_workflow(app: App) -> None:
    guide = app.service("inspect").guide()

    assert "插件开发指南" in guide
    assert "bundle.toml" in guide
    assert "volatile" in guide
    assert "plugin_create" in guide
    assert "plugin_manager" in guide
    assert "settings_write" in guide


async def test_plugins_merges_mounted_loader_state(app: App) -> None:
    rows = app.service("inspect").plugins()
    by_id = {row["id"]: row for row in rows}

    assert all("loader_state" in row for row in rows)
    assert by_id["demo-plugin"]["loader_state"] == "active"
    assert by_id["exploding"]["loader_state"] == "failed"


async def test_plugins_merges_synthetic_mounted_rows(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    from deeppyro.builtin.inspect import InspectPlugin

    def fake_mounted(self: InspectPlugin) -> dict[str, dict[str, str]]:
        return {
            "llm": {"state": "restart-required", "error": "config changed"},
            "phantom-row": {"state": "failed", "error": "boom"},
        }

    monkeypatch.setattr(InspectPlugin, "_mounted_plugins", fake_mounted)

    rows = app.service("inspect").plugins()
    by_id = {row["id"]: row for row in rows}

    assert by_id["llm"]["loader_state"] == "restart-required"
    assert by_id["llm"]["error"] == "config changed"
    assert by_id["phantom-row"]["loader_state"] == "failed"
    assert by_id["phantom-row"]["error"] == "boom"
    assert by_id["phantom-row"]["state"] == "configured"
