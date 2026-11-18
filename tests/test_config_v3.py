"""snake_case config skeleton: canonical output, camelCase rejected."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro import config
from deeppyro.config import (
    AgentConfig,
    Config,
    ModelCapabilities,
    ModelConfig,
    ModelCost,
    ModelProfile,
    ProviderConfig,
)
from deeppyro.core.errors import ConfigError


def test_snake_case_toml_is_canonical(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        "[agent]\n"
        'provider = "local"\n'
        'model = "m"\n'
        "max_tokens = 1024\n"
        "max_steps = 4\n"
        "[providers.local]\n"
        'base_url = "http://127.0.0.1:8000/v1"\n'
        'api_key_env = "LOCAL_KEY"\n'
        'models = [{ id = "m", context_window = 4096 }]\n',
        encoding="utf-8",
    )
    cfg = config.load_config(p, create=False)
    assert (cfg.agent.max_tokens, cfg.agent.max_steps) == (1024, 4)
    provider = cfg.providers["local"]
    assert provider.base_url == "http://127.0.0.1:8000/v1"
    assert provider.api_key_env == "LOCAL_KEY"
    assert provider.models[0].context_window == 4096


def test_camel_case_toml_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        "[agent]\n"
        'provider = "local"\n'
        "maxTokens = 2048\n"
        "[providers.local]\n"
        'baseURL = "http://127.0.0.1:8000/v1"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        config.load_config(p, create=False)
    message = str(excinfo.value)
    assert "agent.maxTokens" in message
    assert "agent.max_tokens" in message
    assert "providers.local.baseURL" in message
    assert "providers.local.base_url" in message


def test_dumped_config_uses_snake_case() -> None:
    cfg = Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                api_key_env="LOCAL_KEY",
                models=[ModelConfig(id="m", context_window=4096)],
            )
        },
        agent={"max_tokens": 1024, "max_steps": 2},
    )
    dumped = cfg.model_dump()
    assert set(dumped["providers"]["local"]) >= {"base_url", "api_key_env"}
    assert dumped["providers"]["local"]["models"][0]["context_window"] == 4096
    assert dumped["agent"]["max_tokens"] == 1024
    assert dumped["agent"]["max_steps"] == 2
    assert "maxTokens" not in dumped["agent"]
    assert "baseURL" not in dumped["providers"]["local"]


def test_constructor_accepts_snake_case_only() -> None:
    provider = ProviderConfig(base_url="http://x/v1", api_key_env="K")
    assert (provider.base_url, provider.api_key_env) == ("http://x/v1", "K")
    assert AgentConfig(max_tokens=1, max_steps=2).max_tokens == 1

    for build in (
        lambda: ProviderConfig(baseURL="http://x/v1"),
        lambda: AgentConfig(maxTokens=1),
        lambda: ModelConfig(id="m", contextWindow=4096),
        lambda: ModelProfile(id="m", contextWindow=4096),
    ):
        with pytest.raises(ConfigError):
            build()


def test_default_toml_template_uses_snake_case() -> None:
    assert "max_tokens = 8192" in config.DEFAULT_TOML
    assert "base_url = " in config.DEFAULT_TOML
    assert "context_window = " in config.DEFAULT_TOML
    assert "maxTokens = 8192" not in config.DEFAULT_TOML
    assert "baseURL = " not in config.DEFAULT_TOML


def test_adapter_options_live_in_model_extra() -> None:
    provider = ProviderConfig(max_tokens=200, reasoning_effort="high")
    assert provider.model_extra == {"max_tokens": 200, "reasoning_effort": "high"}
    model = ModelConfig(id="m", max_tokens=100, reasoning_effort="low", input_modalities=["text"])
    assert model.model_extra == {
        "max_tokens": 100,
        "reasoning_effort": "low",
        "input_modalities": ["text"],
    }


def test_model_profile_carries_capabilities_context_and_cost() -> None:
    model = ModelConfig(
        id="m",
        context_window=65536,
        capabilities={"tools": True, "vision": True},
        cost={"input_per_mtok": 0.25, "output_per_mtok": 1.0},
    )
    profile = ModelProfile.from_model_config(model, provider="local")
    assert profile.id == "m"
    assert profile.provider == "local"
    assert profile.context_window == 65536
    assert profile.capabilities.vision is True
    assert profile.cost.input_per_mtok == 0.25
    tokens = profile.token_cost()
    assert tokens.input_per_mtok == 0.25
    assert tokens.output_per_mtok == 1.0
    assert profile.estimate_cost(input_tokens=1_000_000, output_tokens=1_000_000) == pytest.approx(1.25)


def test_model_profile_rejects_camel_case_context_window() -> None:
    with pytest.raises(ConfigError, match="context_window"):
        ModelProfile(id="m", contextWindow=4096)
    assert ModelCapabilities().tools is True
    assert ModelCost().currency == "USD"
