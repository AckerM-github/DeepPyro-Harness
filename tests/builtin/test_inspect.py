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
