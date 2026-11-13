"""Boot-level tests: inspect.providers() exposes the model catalog summary.

The inspect payload is what the TUI/CLI renders, so these tests pin the shape the UI
layer consumes (list of {provider, id, name, inputModalities, ...} rows), including the
case where discovery has already populated the registry cache.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx

from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel.app import App
from deeppyro.kernel.bundle import boot
from deeppyro.kernel.manifest import PluginSpec

INSPECT_TARGET = "deeppyro.builtin.inspect:InspectPlugin"
LOCAL_LISTING_URL = "http://127.0.0.1:8000/v1/models"


def make_config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m", name="Model M", contextWindow=32768)],
            )
        },
        agent={"provider": "local", "model": "m"},
    )


@pytest.fixture()
async def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[App]:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    kernel = await boot(make_config(), extra_plugins=[PluginSpec(id="inspect", name=INSPECT_TARGET)])
    try:
        yield kernel
    finally:
        await kernel.stop()


async def test_inspect_providers_include_configured_models(app: App) -> None:
    providers = {row["name"]: row for row in app.service("inspect").providers()}
    assert "local" in providers
    assert providers["local"]["models"] == [
        {
            "provider": "local",
            "id": "m",
            "name": "Model M",
            "inputModalities": ["text"],
            "contextWindow": 32768,
        }
    ]


@respx.mock
async def test_inspect_providers_include_discovered_models(app: App) -> None:
    respx.get(LOCAL_LISTING_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "m", "name": "Listed M"}, {"id": "extra", "name": "Extra"}]},
        )
    )
    listed = await app.service("llm").list_models("local")
    assert [info.id for info in listed] == ["m", "extra"]

    providers = {row["name"]: row for row in app.service("inspect").providers()}
    rows = providers["local"]["models"]
    assert [row["id"] for row in rows] == ["m", "extra"]
    by_id = {row["id"]: row for row in rows}
    assert by_id["m"]["name"] == "Listed M"
    # The configured context window survives when the listing omits it.
    assert by_id["m"]["contextWindow"] == 32768
    assert "contextWindow" not in by_id["extra"]


@respx.mock
async def test_llm_service_model_info(app: App) -> None:
    respx.get(LOCAL_LISTING_URL).mock(return_value=httpx.Response(404, text="not found"))
    llm = app.service("llm")
    info = await llm.model_info("local", "m")
    assert info is not None
    assert info.name == "Model M"
    assert await llm.model_info("local", "nope") is None


def test_inspect_catalog_rows_are_defensive() -> None:
    # A service without catalog_snapshot, or one that raises, contributes no models
    # instead of making inspection fail.
    from deeppyro.builtin.inspect import _catalog_rows

    assert _catalog_rows(object(), "local") == []

    class Boom:
        def catalog_snapshot(self, provider: str) -> None:
            raise RuntimeError("boom")

    assert _catalog_rows(Boom(), "local") == []
