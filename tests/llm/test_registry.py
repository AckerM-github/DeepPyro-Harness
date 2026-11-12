"""Provider construction and registry caching tests."""

from __future__ import annotations

import pytest

from deeppyro.config import Config, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.core.protocols import LlmProvider
from deeppyro.llm import LlmRegistry, OpenAICompatProvider, create_provider


def provider_config(**kwargs: object) -> ProviderConfig:
    data = {
        "api": "openai-completions",
        "baseURL": "http://127.0.0.1:8000/v1",
        "models": [{"id": "deepseek-v4-pro-qwen3.5-9b", "contextWindow": 32768}],
    }
    data.update(kwargs)
    return ProviderConfig.model_validate(data)


def test_create_provider_builds_openai_compat_provider() -> None:
    provider = create_provider("local", provider_config(apiKey="secret"))
    assert isinstance(provider, OpenAICompatProvider)
    assert isinstance(provider, LlmProvider)
    assert provider.name == "local"
    assert provider.base_url == "http://127.0.0.1:8000/v1"
    assert provider.api_key == "secret"
    assert provider.model_ctx == 32768
    assert provider.timeout_s == 600.0


def test_create_provider_accepts_openai_alias() -> None:
    provider = create_provider("p", provider_config(api="openai"))
    assert isinstance(provider, OpenAICompatProvider)


def test_create_provider_rejects_unknown_api() -> None:
    with pytest.raises(ConfigError, match="unsupported api"):
        create_provider("p", provider_config(api="anthropic-messages"))


def test_create_provider_without_models_has_no_context_window() -> None:
    provider = create_provider("p", provider_config(models=[]))
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model_ctx is None


def test_create_provider_model_ctx_uses_largest_window() -> None:
    config = provider_config(
        models=[
            {"id": "small", "contextWindow": 8192},
            {"id": "big", "contextWindow": 65536},
            {"id": "unknown"},
        ]
    )
    provider = create_provider("p", config)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model_ctx == 65536


def test_create_provider_reads_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_TEST_KEY", "env-secret")
    provider = create_provider("p", provider_config(apiKeyEnv="DEEPPYRO_TEST_KEY"))
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.api_key == "env-secret"


def test_create_provider_honours_timeout() -> None:
    provider = create_provider("p", provider_config(), timeout_s=12.5)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.timeout_s == 12.5


def _config() -> Config:
    return Config(
        providers={
            "local": provider_config(),
            "deepseek": ProviderConfig.model_validate(
                {
                    "api": "openai-completions",
                    "baseURL": "https://api.deepseek.com/v1",
                    "models": [{"id": "deepseek-chat", "contextWindow": 65536}],
                }
            ),
        }
    )


def test_registry_caches_provider_instances() -> None:
    registry = LlmRegistry(_config())
    first = registry.get("deepseek")
    second = registry.get("deepseek")
    assert first is second
    assert "deepseek" in registry
    assert "nope" not in registry
    assert set(registry.providers) == {"deepseek"}


def test_registry_unknown_provider_raises_config_error() -> None:
    registry = LlmRegistry(_config())
    with pytest.raises(ConfigError, match="unknown provider"):
        registry.get("missing")


async def test_registry_aclose_closes_and_forgets() -> None:
    registry = LlmRegistry(_config())
    provider = registry.get("local")
    assert isinstance(provider, OpenAICompatProvider)
    provider._ensure_client()
    assert provider._client is not None
    await registry.aclose()
    assert provider._client is None
    assert provider._closed is True
    assert registry.providers == {}
    # idempotent
    await registry.aclose()


async def test_registry_aclose_raises_first_error_after_closing_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = LlmRegistry(_config())
    good = registry.get("local")
    bad = registry.get("deepseek")
    closed: list[str] = []

    async def close_good() -> None:
        closed.append("good")

    async def close_bad() -> None:
        closed.append("bad")
        raise RuntimeError("close failed")

    monkeypatch.setattr(good, "aclose", close_good)
    monkeypatch.setattr(bad, "aclose", close_bad)
    with pytest.raises(RuntimeError, match="close failed"):
        await registry.aclose()
    assert sorted(closed) == ["bad", "good"]
