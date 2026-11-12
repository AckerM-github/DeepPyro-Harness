"""Provider construction and per-config caching.

create_provider() is the single seam between configuration (deeppyro.config.ProviderConfig)
and the LLM layer: it picks the transport implementation for ProviderConfig.api.
LlmRegistry caches one provider instance per configured provider name so the
underlying HTTP connection pools are shared across turns.
"""

from __future__ import annotations

import asyncio
from typing import Any

from deeppyro.config import Config, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.core.protocols import LlmProvider
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


def create_provider(name: str, pc: ProviderConfig, *, timeout_s: float = 600.0) -> LlmProvider:
    """Build the LlmProvider described by one ProviderConfig.

    Raises ConfigError for an unknown api kind (e.g. a future "anthropic-messages"),
    so misconfiguration fails at startup instead of on the first request.
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
    supported = ", ".join(sorted(_OPENAI_COMPAT_APIS))
    raise ConfigError(
        f"provider {name!r} uses unsupported api {pc.api!r}; supported: {supported}"
    )


def _context_window(pc: ProviderConfig) -> int | None:
    windows = [m.contextWindow for m in pc.models if m.contextWindow is not None]
    return max(windows) if windows else None


class LlmRegistry:
    """Lazily instantiates and caches one LlmProvider per configured provider name."""

    def __init__(self, cfg: Config) -> None:
        self.config = cfg
        self._providers: dict[str, LlmProvider] = {}

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
            known = ", ".join(sorted(self.config.providers)) or "<none>"
            raise ConfigError(f"unknown provider: {provider!r} (configured: {known})")
        created = create_provider(provider, pc)
        self._providers[provider] = created
        return created

    @property
    def providers(self) -> dict[str, LlmProvider]:
        """Currently instantiated providers (a copy; used by tests and the CLI)."""
        return dict(self._providers)

    def __contains__(self, provider: object) -> bool:
        return isinstance(provider, str) and provider in self.config.providers

    async def aclose(self) -> None:
        """Close every instantiated provider, then forget them.

        All providers are closed even if one fails; the first exception is re-raised
        afterwards so shutdown problems are not silently swallowed.
        """
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
