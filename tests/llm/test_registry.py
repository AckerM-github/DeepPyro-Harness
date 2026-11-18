"""Native provider construction, registry caching and model-catalog tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from deeppyro.config import Config, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.core.llm import LlmProvider as NativeLlmProvider
from deeppyro.llm import LlmRegistry, ModelInfo, create_native_provider
from deeppyro.llm.errors import CODE_SERVER
from deeppyro.llm.native_anthropic import NativeAnthropicProvider
from deeppyro.llm.native_provider import NativeOpenAICompatProvider


def provider_config(**kwargs: object) -> ProviderConfig:
    data = {
        "api": "openai-completions",
        "base_url": "http://127.0.0.1:8000/v1",
        "models": [{"id": "deepseek-v4-pro-qwen3.5-9b", "context_window": 32768}],
    }
    data.update(kwargs)
    return ProviderConfig.model_validate(data)


def anthropic_config(**kwargs: object) -> ProviderConfig:
    data: dict[str, object] = {
        "api": "anthropic-messages",
        "base_url": "https://api.anthropic.com",
        "api_key": "anthropic-key",
        "models": [
            {
                "id": "claude-sonnet-4-5",
                "context_window": 200000,
                "max_tokens": 64000,
                "reasoning_effort": "medium",
            }
        ],
    }
    data.update(kwargs)
    return ProviderConfig.model_validate(data)


# --------------------------------------------------------------------------
# provider construction
# --------------------------------------------------------------------------


def test_create_native_provider_builds_a_native_openai_compat_provider() -> None:
    provider = create_native_provider("local", provider_config(api_key="secret"))
    assert isinstance(provider, NativeOpenAICompatProvider)
    assert isinstance(provider, NativeLlmProvider)
    assert provider.name == "local"
    assert provider.base_url == "http://127.0.0.1:8000/v1"
    assert provider.api_key == "secret"
    assert provider.model_ctx == 32768
    assert provider.timeout_s == 600.0


def test_create_native_provider_accepts_aliases_and_timeout() -> None:
    provider = create_native_provider("p", provider_config(api="openai"), timeout_s=12.5)
    assert isinstance(provider, NativeOpenAICompatProvider)
    assert provider.timeout_s == 12.5


def test_create_native_provider_without_models_has_no_context_window() -> None:
    provider = create_native_provider("p", provider_config(models=[]))
    assert isinstance(provider, NativeOpenAICompatProvider)
    assert provider.model_ctx is None


def test_create_native_provider_model_ctx_uses_largest_window() -> None:
    config = provider_config(
        models=[
            {"id": "small", "context_window": 8192},
            {"id": "big", "context_window": 65536},
            {"id": "unknown"},
        ]
    )
    provider = create_native_provider("p", config)
    assert isinstance(provider, NativeOpenAICompatProvider)
    assert provider.model_ctx == 65536


def test_create_native_provider_builds_a_native_anthropic_provider() -> None:
    provider = create_native_provider("claude", anthropic_config())
    assert isinstance(provider, NativeAnthropicProvider)
    assert isinstance(provider, NativeLlmProvider)
    assert provider.name == "claude"
    assert provider.messages_url == "https://api.anthropic.com/v1/messages"
    assert provider.api_key == "anthropic-key"
    assert provider.model_ctx == 200000
    assert provider.model_max_tokens == 64000
    assert provider.reasoning_effort == "medium"
    assert provider.timeout_s == 600.0


def test_create_native_provider_anthropic_accepts_aliases_and_timeout() -> None:
    provider = create_native_provider("claude", anthropic_config(api="anthropic"), timeout_s=12.5)
    assert isinstance(provider, NativeAnthropicProvider)
    assert provider.timeout_s == 12.5


def test_create_native_provider_anthropic_requires_explicit_base_url() -> None:
    config = ProviderConfig(api="anthropic-messages", api_key="key")
    with pytest.raises(ConfigError, match="base_url") as info:
        create_native_provider("claude", config)
    assert "https://api.anthropic.com" in str(info.value)


def test_create_native_provider_anthropic_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPPYRO_MISSING_KEY", raising=False)
    config = anthropic_config(api_key=None, api_key_env="DEEPPYRO_MISSING_KEY")
    with pytest.raises(ConfigError, match="API key") as info:
        create_native_provider("claude", config)
    assert "'claude'" in str(info.value)
    assert "DEEPPYRO_MISSING_KEY" in str(info.value)


def test_create_native_provider_anthropic_reads_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_TEST_ANTHROPIC_KEY", "env-anthropic-key")
    config = anthropic_config(api_key=None, api_key_env="DEEPPYRO_TEST_ANTHROPIC_KEY")
    provider = create_native_provider("claude", config)
    assert isinstance(provider, NativeAnthropicProvider)
    assert provider.api_key == "env-anthropic-key"


def test_create_native_provider_rejects_unknown_api() -> None:
    with pytest.raises(ConfigError, match="unsupported api"):
        create_native_provider("p", provider_config(api="gizmo"))


# --------------------------------------------------------------------------
# registry caching and lifecycle
# --------------------------------------------------------------------------


def _config() -> Config:
    return Config(
        providers={
            "local": provider_config(),
            "deepseek": ProviderConfig.model_validate(
                {
                    "api": "openai-completions",
                    "base_url": "https://api.deepseek.com/v1",
                    "models": [{"id": "deepseek-chat", "context_window": 65536}],
                }
            ),
        }
    )


def test_registry_caches_provider_instances() -> None:
    registry = LlmRegistry(_config())
    first = registry.native_provider("deepseek")
    second = registry.native_provider("deepseek")
    assert isinstance(first, NativeOpenAICompatProvider)
    assert first is second
    assert "deepseek" in registry
    assert "nope" not in registry
    assert set(registry.providers) == {"deepseek"}


def test_registry_unknown_provider_raises_config_error() -> None:
    registry = LlmRegistry(_config())
    with pytest.raises(ConfigError, match="unknown provider"):
        registry.native_provider("missing")


def test_registry_builds_an_anthropic_provider() -> None:
    cfg = Config(
        providers={
            "claude": ProviderConfig.model_validate(
                {
                    "api": "anthropic-messages",
                    "base_url": "https://api.anthropic.com",
                    "api_key": "key",
                    "models": [{"id": "claude-sonnet-4-5"}],
                }
            )
        }
    )
    registry = LlmRegistry(cfg)
    provider = registry.native_provider("claude")
    assert isinstance(provider, NativeAnthropicProvider)
    assert provider is registry.native_provider("claude")


async def test_registry_aclose_closes_and_forgets() -> None:
    registry = LlmRegistry(_config())
    provider = registry.native_provider("local")
    assert isinstance(provider, NativeOpenAICompatProvider)
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
    good = registry.native_provider("local")
    bad = registry.native_provider("deepseek")
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
