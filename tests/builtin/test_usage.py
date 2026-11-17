"""Tests for the in-memory token/cost accounting service skeleton."""

from __future__ import annotations

import pytest

from deeppyro.builtin.usage import UsageService
from deeppyro.config import Config, ModelConfig, ModelCost, ModelProfile, ProviderConfig
from deeppyro.kernel.app import create_app
from deeppyro.kernel.context import RuntimeContext
from deeppyro.kernel.manifest import PluginSpec


def _config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )


def _profile() -> ModelProfile:
    return ModelProfile(
        id="m",
        provider="local",
        context_window=32768,
        cost=ModelCost(input_per_mtok=1.0, output_per_mtok=2.0, cached_input_per_mtok=0.5),
    )


def test_usage_service_records_tokens_and_costs() -> None:
    service = UsageService(RuntimeContext.create_root())
    profile = _profile()
    record = service.record(profile, input_tokens=1_000_000, output_tokens=500_000, cached_tokens=2_000_000)
    assert (record.provider, record.model) == ("local", "m")
    assert record.cost == pytest.approx(3.0)
    totals = service.totals()
    assert totals.requests == 1
    assert (totals.input_tokens, totals.output_tokens, totals.cached_tokens) == (
        1_000_000,
        500_000,
        2_000_000,
    )
    assert service.records() == (record,)
    assert service.by_model()["m"].cost == pytest.approx(3.0)
    assert service.summary()["totals"]["requests"] == 1

    service.record(provider="local", model="m", input_tokens=10)
    assert service.totals().requests == 2
    assert service.totals().cost == pytest.approx(3.0)

    service.reset()
    assert service.totals().requests == 0
    assert service.records() == ()
    assert service.by_model() == {}


def test_usage_service_rejects_negative_tokens() -> None:
    service = UsageService(RuntimeContext.create_root())
    with pytest.raises(ValueError, match="input_tokens"):
        service.record(input_tokens=-1)
    with pytest.raises(ValueError, match="output_tokens"):
        service.record(output_tokens=-1)
    with pytest.raises(ValueError, match="cached_tokens"):
        service.record(cached_tokens=-1)


def test_usage_totals_copy_is_independent() -> None:
    service = UsageService(RuntimeContext.create_root())
    service.record(provider="local", model="m", input_tokens=5)
    copy = service.by_model()["m"]
    copy.input_tokens = 999
    assert service.by_model()["m"].input_tokens == 5


async def test_usage_service_mounts_through_the_v3_plugin_path() -> None:
    app = await create_app(
        _config(),
        plugins=(PluginSpec(id="usage", name="deeppyro.builtin.usage:UsageService"),),
    )
    try:
        service = app.require(UsageService)
        assert isinstance(service, UsageService)
        service.record(None, provider="local", model="m", input_tokens=12)
        assert service.totals().input_tokens == 12
    finally:
        await app.stop()
