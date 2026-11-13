"""Provider construction, registry caching and model-catalog tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from deeppyro.config import Config, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.core.protocols import LlmProvider
from deeppyro.llm import (
    AnthropicProvider,
    LlmRegistry,
    ModelInfo,
    OpenAICompatProvider,
    create_provider,
)
from deeppyro.llm.errors import CODE_SERVER


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


def anthropic_config(**kwargs: object) -> ProviderConfig:
    data: dict[str, object] = {
        "api": "anthropic-messages",
        "baseURL": "https://api.anthropic.com",
        "apiKey": "anthropic-key",
        "models": [
            {
                "id": "claude-sonnet-4-5",
                "contextWindow": 200000,
                "maxTokens": 64000,
                "reasoningEffort": "medium",
            }
        ],
    }
    data.update(kwargs)
    return ProviderConfig.model_validate(data)


def test_create_provider_builds_anthropic_provider() -> None:
    provider = create_provider("claude", anthropic_config())
    assert isinstance(provider, AnthropicProvider)
    assert isinstance(provider, LlmProvider)
    assert provider.name == "claude"
    assert provider.messages_url == "https://api.anthropic.com/v1/messages"
    assert provider.api_key == "anthropic-key"
    assert provider.model_ctx == 200000
    assert provider.model_max_tokens == 64000
    assert provider.reasoning_effort == "medium"
    assert provider.timeout_s == 600.0


def test_create_provider_accepts_anthropic_alias() -> None:
    provider = create_provider("claude", anthropic_config(api="anthropic"))
    assert isinstance(provider, AnthropicProvider)


def test_create_provider_anthropic_defaults_to_thinking_off() -> None:
    provider = create_provider("claude", anthropic_config(models=[{"id": "claude-haiku"}]))
    assert isinstance(provider, AnthropicProvider)
    assert provider.reasoning_effort == "off"
    assert provider.model_max_tokens is None


def test_create_provider_anthropic_reads_provider_level_extras() -> None:
    config = anthropic_config(
        models=[{"id": "claude-haiku"}],
        maxTokens=4096,
        reasoningEffort="high",
    )
    provider = create_provider("claude", config)
    assert isinstance(provider, AnthropicProvider)
    assert provider.model_max_tokens == 4096
    assert provider.reasoning_effort == "high"


def test_create_provider_anthropic_requires_explicit_base_url() -> None:
    config = ProviderConfig(api="anthropic-messages", apiKey="key")
    assert "baseURL" not in config.model_fields_set
    with pytest.raises(ConfigError, match="baseURL") as info:
        create_provider("claude", config)
    assert "https://api.anthropic.com" in str(info.value)


def test_create_provider_rejects_unknown_api() -> None:
    with pytest.raises(ConfigError, match="unsupported api"):
        create_provider("p", provider_config(api="gizmo"))


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


# --------------------------------------------------------------------------
# model catalog discovery
# --------------------------------------------------------------------------

LOCAL_LISTING_URL = "http://127.0.0.1:8000/v1/models"
DEEPSEEK_LISTING_URL = "https://api.deepseek.com/v1/models"


def listing(*entries: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": list(entries)})


def ids(models: list[ModelInfo]) -> list[str]:
    return [model.id for model in models]


def test_registry_configured_models_and_snapshot_without_discovery() -> None:
    registry = LlmRegistry(_config())
    configured = registry.configured_models("local")
    assert ids(configured) == ["deepseek-v4-pro-qwen3.5-9b"]
    assert configured[0].context_window == 32768
    assert registry.catalog_snapshot("local") == configured
    # Unknown providers are empty rather than an error: inspect unions config and plugins.
    assert registry.configured_models("missing") == []
    assert registry.catalog_snapshot("missing") == []


@respx.mock
async def test_registry_list_models_merges_listing_over_configured_catalog() -> None:
    route = respx.get(LOCAL_LISTING_URL).mock(
        return_value=listing(
            {"id": "deepseek-v4-pro-qwen3.5-9b", "name": "Local Qwen", "context_length": 65536},
            {"id": "listed-only", "name": "Listed Only"},
        )
    )
    registry = LlmRegistry(_config())
    models = await registry.list_models("local")

    assert ids(models) == ["deepseek-v4-pro-qwen3.5-9b", "listed-only"]
    assert models[0].name == "Local Qwen"
    assert models[0].context_window == 65536
    assert models[1].input_modalities == ("text",)
    assert registry.catalog_snapshot("local") == models
    assert registry.last_catalog_error is None
    assert route.call_count == 1
    await registry.aclose()


@respx.mock
async def test_registry_list_models_uses_ttl_cache() -> None:
    route = respx.get(LOCAL_LISTING_URL).mock(return_value=listing({"id": "a"}))
    registry = LlmRegistry(_config())
    first = await registry.list_models("local")
    second = await registry.list_models("local")
    assert ids(first) == ids(second)
    assert route.call_count == 1
    await registry.aclose()


@respx.mock
async def test_registry_refresh_bypasses_cache() -> None:
    route = respx.get(LOCAL_LISTING_URL).mock(side_effect=[listing({"id": "a"}), listing({"id": "b"})])
    registry = LlmRegistry(_config())
    await registry.list_models("local")
    refreshed = await registry.list_models("local", refresh=True)
    assert "b" in ids(refreshed)
    assert route.call_count == 2
    await registry.aclose()


@respx.mock
async def test_registry_discovery_failure_falls_back_to_configured_catalog() -> None:
    route = respx.get(LOCAL_LISTING_URL).mock(return_value=httpx.Response(500, text="down"))
    registry = LlmRegistry(_config())
    models = await registry.list_models("local")
    assert ids(models) == ["deepseek-v4-pro-qwen3.5-9b"]
    assert registry.last_catalog_error is not None
    assert registry.last_catalog_error.code == CODE_SERVER
    # Failure with no last-known-good listing backs off for a TTL instead of hammering.
    again = await registry.list_models("local")
    assert ids(again) == ["deepseek-v4-pro-qwen3.5-9b"]
    assert route.call_count == 1
    # An explicit refresh always retries.
    await registry.list_models("local", refresh=True)
    assert route.call_count == 2
    await registry.aclose()


@respx.mock
async def test_registry_keeps_last_known_good_listing_on_failure() -> None:
    route = respx.get(LOCAL_LISTING_URL).mock(
        side_effect=[listing({"id": "listed"}), httpx.Response(503, text="down")]
    )
    registry = LlmRegistry(_config(), catalog_ttl_s=0.0)
    first = await registry.list_models("local")
    assert "listed" in ids(first)
    second = await registry.list_models("local")
    assert ids(second) == ids(first)
    assert registry.last_catalog_error is not None
    assert route.call_count == 2
    await registry.aclose()


@respx.mock
async def test_registry_empty_listing_keeps_configured_catalog() -> None:
    respx.get(LOCAL_LISTING_URL).mock(return_value=httpx.Response(404, text="not found"))
    registry = LlmRegistry(_config())
    models = await registry.list_models("local")
    assert ids(models) == ["deepseek-v4-pro-qwen3.5-9b"]
    assert registry.last_catalog_error is None  # 404 is "no listing endpoint", not an error
    await registry.aclose()


async def test_registry_provider_without_listing_capability_returns_config() -> None:
    class NoListing:
        name = "local"

        async def aclose(self) -> None:
            return None

    registry = LlmRegistry(_config())
    registry._providers["local"] = NoListing()  # type: ignore[assignment]
    models = await registry.list_models("local")
    assert ids(models) == ["deepseek-v4-pro-qwen3.5-9b"]


async def test_registry_list_models_unknown_provider_raises() -> None:
    registry = LlmRegistry(_config())
    with pytest.raises(ConfigError, match="unknown provider"):
        await registry.list_models("missing")


@respx.mock
async def test_registry_model_info() -> None:
    respx.get(DEEPSEEK_LISTING_URL).mock(
        return_value=listing({"id": "deepseek-chat", "name": "DeepSeek Chat"})
    )
    registry = LlmRegistry(_config())
    info = await registry.model_info("deepseek", "deepseek-chat")
    assert info is not None
    assert info.name == "DeepSeek Chat"
    assert info.context_window == 65536  # configured floor fills what the listing omits
    assert await registry.model_info("deepseek", "unknown-model") is None
    await registry.aclose()


@respx.mock
async def test_registry_aclose_clears_catalog() -> None:
    respx.get(LOCAL_LISTING_URL).mock(return_value=httpx.Response(500, text="down"))
    registry = LlmRegistry(_config())
    await registry.list_models("local")
    assert registry.last_catalog_error is not None
    await registry.aclose()
    assert registry.last_catalog_error is None
    assert registry.catalog_snapshot("local") == registry.configured_models("local")
