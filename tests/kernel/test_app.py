from pathlib import Path

import pytest

from deeppyro.config import Config
from deeppyro.kernel.app import KernelBootError, create_app
from deeppyro.kernel.manifest import PluginSpec


def _config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "m"},
            "providers": {
                "local": {"api": "openai", "baseURL": "http://127.0.0.1:8000/v1", "models": [{"id": "m"}]}
            },
        }
    )


async def test_create_app_loads_plugins_and_services() -> None:
    app = await create_app(
        _config(),
        plugins=(
            PluginSpec(id="counter", name="tests.kernel.plugins_dummy:CounterPlugin", config={"start": 5}),
            PluginSpec(id="consumer", name="tests.kernel.plugins_dummy:ConsumerPlugin"),
        ),
    )
    try:
        assert app.service("counter").n == 5
        assert app.service("counter").inc() == 6
        assert app.service("config").value.agent.model == "m"
        assert app.started["counter"] == "ACTIVE"
    finally:
        await app.stop()


async def test_create_app_applies_patch_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "dpyro.toml"
    manifest.write_text(
        """
[[plugins]]
id = "counter"
config = { start = 9 }

[[plugins]]
id = "consumer"
disabled = true
""",
        encoding="utf-8",
    )
    app = await create_app(
        _config(),
        plugins=(
            PluginSpec(id="counter", name="tests.kernel.plugins_dummy:CounterPlugin"),
            PluginSpec(id="consumer", name="tests.kernel.plugins_dummy:ConsumerPlugin"),
        ),
        manifest_paths=(str(manifest),),
    )
    try:
        assert app.service("counter").n == 9
        assert app.started == {"counter": "ACTIVE"}
        assert "consumer" not in app.started
    finally:
        await app.stop()


async def test_stop_removes_services() -> None:
    app = await create_app(
        _config(),
        plugins=(PluginSpec(id="counter", name="tests.kernel.plugins_dummy:CounterPlugin"),),
    )
    await app.stop()
    assert not app.has_service("counter")


async def test_plugin_failure_raises_boot_error() -> None:
    with pytest.raises(KernelBootError):
        await create_app(
            _config(),
            plugins=(PluginSpec(id="boom", name="tests.kernel.plugins_dummy:ExplodingPlugin"),),
        )
