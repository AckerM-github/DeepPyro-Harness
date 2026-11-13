"""Boot-level tests for the settings service (Layer D)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from deeppyro.api.errors import PluginConfigError
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel.app import App
from deeppyro.kernel.bundle import boot
from deeppyro.kernel.manifest import PluginSpec
from deeppyro.plugins import PluginNotFoundError, SettingsConflict, read_manifest, save_user_manifest

DEMO_TARGET = "tests.builtin.demo_plugins:DemoPlugin"
SETTINGS_TARGET = "deeppyro.builtin.settings:SettingsPlugin"


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


def write_manifest(home: Path, *specs: PluginSpec) -> Path:
    path = home / "plugins.toml"
    save_user_manifest(path, list(specs))
    return path


@pytest.fixture()
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[App]:
    home = tmp_path / "home"
    monkeypatch.setenv("DEEPPYRO_HOME", str(home))
    write_manifest(
        home,
        PluginSpec(
            id="demo-plugin",
            name=DEMO_TARGET,
            config={"apiKey": "hunter2", "maxTokens": 1234},
        ),
    )
    kernel = await boot(
        make_config(),
        extra_plugins=[PluginSpec(id="settings", name=SETTINGS_TARGET)],
    )
    try:
        yield kernel
    finally:
        await kernel.stop()


async def test_describe_exposes_schema_and_masked_values(app: App) -> None:
    settings = app.service("settings")

    described = settings.describe("demo-plugin")

    assert described["plugin_id"] == "demo-plugin"
    assert described["target"] == DEMO_TARGET
    assert described["config_source"] == "manifest"
    assert described["apply"] == "next-boot"
    properties = described["schema"]["properties"]
    assert {"apiKey", "model", "maxTokens", "temperature", "token", "nested"} <= set(properties)
    assert described["values"]["apiKey"] == "***"
    assert described["values"]["maxTokens"] == "***"
    assert len(described["revision"]) == 16


async def test_describe_plugin_without_config(app: App) -> None:
    described = app.service("settings").describe("llm")
    assert described["schema"] is None
    assert described["values"] == {}


async def test_read_and_write_round_trip(app: App, tmp_path: Path) -> None:
    settings = app.service("settings")
    before = settings.read("demo-plugin")

    written = settings.write("demo-plugin", {"temperature": 0.9}, before["revision"])

    assert written["changed"] == ["temperature"]
    assert written["restart_required"] is True
    assert written["apply"] == "next-boot"
    assert written["revision"] != before["revision"]
    assert written["values"]["apiKey"] == "***"
    assert written["values"]["temperature"] == 0.9

    specs = read_manifest(tmp_path / "home" / "plugins.toml")
    demo = next(spec for spec in specs if spec.id == "demo-plugin")
    assert demo.config["apiKey"] == "hunter2"
    assert demo.config["temperature"] == 0.9
    assert demo.config["maxTokens"] == 1234


async def test_write_rejects_stale_revision(app: App) -> None:
    settings = app.service("settings")
    revision = settings.read("demo-plugin")["revision"]
    settings.write("demo-plugin", {"temperature": 0.5}, revision)

    with pytest.raises(SettingsConflict, match="stale settings revision"):
        settings.write("demo-plugin", {"temperature": 0.6}, revision)


async def test_write_validates_against_config_model(app: App) -> None:
    settings = app.service("settings")
    revision = settings.read("demo-plugin")["revision"]

    with pytest.raises(PluginConfigError, match="temperature"):
        settings.write("demo-plugin", {"temperature": "too-hot"}, revision)


async def test_write_masked_secret_keeps_stored_value(app: App) -> None:
    settings = app.service("settings")
    revision = settings.read("demo-plugin")["revision"]

    settings.write("demo-plugin", {"apiKey": "***", "model": "other-model"}, revision)

    described = app.service("settings").describe("demo-plugin")
    assert described["values"]["apiKey"] == "***"
    assert described["values"]["model"] == "other-model"


async def test_unknown_plugin_raises(app: App) -> None:
    with pytest.raises(PluginNotFoundError):
        app.service("settings").describe("does-not-exist")
