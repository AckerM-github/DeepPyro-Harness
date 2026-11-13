"""Provider construction, caching and model-catalog lookups.

create_provider() is the single seam between configuration (deeppyro.config.ProviderConfig)
and the LLM layer: it picks the transport implementation for ProviderConfig.api.
LlmRegistry caches one provider instance per configured provider name so the underlying
HTTP connection pools are shared across turns.

Transports (docs/spec/llm.md section 14): "openai-completions" (DeepSeek, llama-server,
vLLM, ...) and "anthropic-messages" (Anthropic Messages API and compatibles). Anthropic
requires an explicit baseURL: ProviderConfig.baseURL has a DeepSeek-flavoured default, so a
missing value is almost certainly a mistake and is reported with the official endpoint as
a hint instead of silently calling the wrong host.

The registry also owns model discovery (docs/spec/llm.md section 13): a configured
provider may expose an optional list_models() method (see
deeppyro.llm.catalog.ModelCatalog), whose listing is cached for a TTL and merged over the
configured catalog. Discovery is advisory only: it never mutates config.toml, never widens
which model ids are routable, and every failure degrades to ProviderConfig.models.
"""

from __future__ import annotations

import asyncio
from typing import Any

from deeppyro.config import Config, ProviderConfig
from deeppyro.core.errors import ConfigError, LlmError
from deeppyro.core.protocols import LlmProvider
from deeppyro.llm.anthropic import AnthropicProvider
from deeppyro.llm.catalog import ModelInfo, catalog_from_config, merge_catalogs
from deeppyro.llm.discovery import DEFAULT_CATALOG_TTL_S, CatalogCache
from deeppyro.llm.openai_compat import OpenAICompatProvider

#: ProviderConfig.api values that map to OpenAICompatProvider. The canonical value is
#: "openai-completions"; the aliases exist so hand-written configs are forgiving.
_OPENAI_COMPAT_APIS = frozenset(
    {
        "openai-completions",
        "openai_completions",
        "openai-chat",
        "openai",
        "chat-completions",
    }
)

#: ProviderConfig.api values that map to AnthropicProvider.
_ANTHROPIC_APIS = frozenset(
    {
        "anthropic-messages",
        "anthropic_messages",
        "anthropic",
    }
)

#: Hint shown when an Anthropic provider omits baseURL (it has no default on purpose).
_DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


def create_provider(name: str, pc: ProviderConfig, *, timeout_s: float = 600.0) -> LlmProvider:
    """Build the LlmProvider described by one ProviderConfig.

    Raises ConfigError for an unknown api kind or an Anthropic provider without an explicit
    baseURL, so misconfiguration fails at startup instead of on the first request.
    """
    api = (pc.api or "").strip().lower()
    if api in _OPENAI_COMPAT_APIS:
        return OpenAICompatProvider(
            name,
            pc.baseURL,
            pc.resolve_api_key(),
            model_ctx=_context_window(pc),
            timeout_s=timeout_s,
        )
    if api in _ANTHROPIC_APIS:
        if "baseURL" not in pc.model_fields_set or not pc.baseURL.strip():
            raise ConfigError(
                f"provider {name!r} uses api {pc.api!r} but does not set baseURL; "
                "Anthropic providers require an explicit endpoint "
                f"(for the official API: {_DEFAULT_ANTHROPIC_BASE_URL})"
            )
        return AnthropicProvider(
            name,
            pc.baseURL,
            pc.resolve_api_key(),
            model_ctx=_context_window(pc),
            model_max_tokens=_max_tokens(pc),
            reasoning_effort=_reasoning_effort(pc),
            timeout_s=timeout_s,
        )
    supported = ", ".join(sorted(_OPENAI_COMPAT_APIS | _ANTHROPIC_APIS))
    raise ConfigError(f"provider {name!r} uses unsupported api {pc.api!r}; supported: {supported}")


def _context_window(pc: ProviderConfig) -> int | None:
    windows = [m.contextWindow for m in pc.models if m.contextWindow is not None]
    return max(windows) if windows else None


def _max_tokens(pc: ProviderConfig) -> int | None:
    """Output cap declared by config extras (model-level first, then provider-level)."""
    for model in pc.models:
        value = _extra(model, "maxTokens", "max_tokens")
        if isinstance(value, int) and value > 0:
            return value
    value = _extra(pc, "maxTokens", "max_tokens")
    return value if isinstance(value, int) and value > 0 else None


def _reasoning_effort(pc: ProviderConfig) -> str:
    """Anthropic reasoning effort from config extras; "off" when not configured."""
    for model in pc.models:
        value = _extra(model, "reasoningEffort", "reasoning_effort")
        if isinstance(value, str) and value:
            return value.lower()
    value = _extra(pc, "reasoningEffort", "reasoning_effort")
    if isinstance(value, str) and value:
        return value.lower()
    return "off"


def _extra(obj: Any, *keys: str) -> Any:
    extra = getattr(obj, "model_extra", None) or {}
    for key in keys:
        if key in extra and extra[key] is not None:
            return extra[key]
    return None


class LlmRegistry:
    """Lazily instantiates and caches one LlmProvider per configured provider name."""

    def __init__(self, cfg: Config, *, catalog_ttl_s: float = DEFAULT_CATALOG_TTL_S) -> None:
        self.config = cfg
        self.catalog_ttl_s = float(catalog_ttl_s)
        self._providers: dict[str, LlmProvider] = {}
        self._catalog = CatalogCache(ttl_s=self.catalog_ttl_s)
        self._catalog_locks: dict[str, asyncio.Lock] = {}

    # -- providers ----------------------------------------------------------
    def get(self, provider: str) -> LlmProvider:
        """Return the (cached) provider for a configured name.

        Raises ConfigError when the name is not present in Config.providers. Note that
        Config.resolve() raises KeyError for the same condition; the LLM layer
        normalizes configuration mistakes to ConfigError.
        """
        cached = self._providers.get(provider)
        if cached is not None:
            return cached
        pc = self.config.providers.get(provider)
        if pc is None:
            raise ConfigError(self._unknown_provider_message(provider))
        created = create_provider(provider, pc)
        self._providers[provider] = created
        return created

    @property
    def providers(self) -> dict[str, LlmProvider]:
        """Currently instantiated providers (a copy; used by tests and the CLI)."""
        return dict(self._providers)

    def __contains__(self, provider: object) -> bool:
        return isinstance(provider, str) and provider in self.config.providers

    # -- model catalog ------------------------------------------------------
    def configured_models(self, provider: str) -> list[ModelInfo]:
        """Catalog entries declared in config.toml ([] for an unknown provider)."""
        pc = self.config.providers.get(provider)
        if pc is None:
            return []
        return catalog_from_config(provider, pc)

    def catalog_snapshot(self, provider: str) -> list[ModelInfo]:
        """Best catalog available without awaiting: last listing, else config.

        This is the synchronous accessor for inspect/TUI rendering; use list_models()
        when a (possibly refreshed) listing is wanted.
        """
        cached = self._catalog.last_good(provider)
        if cached is not None:
            return cached
        return self.configured_models(provider)

    async def list_models(self, provider: str, *, refresh: bool = False) -> list[ModelInfo]:
        """Return the model catalog for one provider, discovering when needed.

        Precedence: fresh cache -> provider listing merged over the configured catalog ->
        last-known-good listing -> configured catalog. Unknown provider names raise
        ConfigError (a typo must not look like "this provider has no models"); every
        discovery failure is recorded and degrades silently, because the catalog is
        display metadata, not a routing decision.
        """
        if provider not in self.config.providers:
            raise ConfigError(self._unknown_provider_message(provider))
        if not refresh:
            cached = self._catalog.fresh(provider)
            if cached is not None:
                return cached
            if self._failure_backs_off(provider):
                return self.configured_models(provider)
        lock = self._catalog_locks.setdefault(provider, asyncio.Lock())
        async with lock:
            # Another coroutine may have refreshed while we waited for the lock.
            if not refresh:
                cached = self._catalog.fresh(provider)
                if cached is not None:
                    return cached
                if self._failure_backs_off(provider):
                    return self.configured_models(provider)
            return await self._discover(provider)

    async def model_info(self, provider: str, model_id: str) -> ModelInfo | None:
        """Look up one catalog entry by model id (None when it is not listed)."""
        for info in await self.list_models(provider):
            if info.id == model_id:
                return info
        return None

    @property
    def last_catalog_error(self) -> LlmError | None:
        """Most recent discovery failure, kept for diagnostics (never raised)."""
        return self._catalog.last_error

    # -- internals ----------------------------------------------------------
    async def _discover(self, provider: str) -> list[ModelInfo]:
        base = self.configured_models(provider)
        impl = self.get(provider)
        lister = getattr(impl, "list_models", None)
        if lister is None:
            # Provider has no discovery capability: the configured catalog is the answer.
            return base
        try:
            listed = list(await lister())
        except LlmError as exc:
            self._catalog.store_error(provider, exc)
            last_good = self._catalog.last_good(provider)
            return last_good if last_good is not None else base
        if not listed:
            # An empty listing is not evidence that no models exist (404/405 maps to []);
            # keep showing the configured catalog but do not mark it as fetched.
            return base
        merged = merge_catalogs(base, listed)
        self._catalog.store(provider, merged)
        return list(merged)

    def _failure_backs_off(self, provider: str) -> bool:
        """A failed discovery without a last-known-good listing is not retried per call."""
        return self._catalog.failure_fresh(provider) and self._catalog.last_good(provider) is None

    def _unknown_provider_message(self, provider: str) -> str:
        known = ", ".join(sorted(self.config.providers)) or "<none>"
        return f"unknown provider: {provider!r} (configured: {known})"

    async def aclose(self) -> None:
        """Close every instantiated provider, then forget them.

        All providers are closed even if one fails; the first exception is re-raised
        afterwards so shutdown problems are not silently swallowed.
        """
        self._catalog.clear()
        self._catalog_locks.clear()
        providers, self._providers = self._providers, {}
        if not providers:
            return
        results: list[Any] = await asyncio.gather(
            *(p.aclose() for p in providers.values()), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result


__all__ = ["LlmRegistry", "create_provider"]
