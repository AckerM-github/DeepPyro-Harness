"""The llm service: configured providers plus dynamically registered ones."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from deeppyro.api import PluginConfig, PluginContext, PluginDependencyError, Service
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.llm import LlmRegistry, ModelInfo


class LlmPlugin(Service):
    """Hosts the provider registry under the llm service name."""

    provide = "llm"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "llm"
    version = "0.2.0"
    description = "LLM provider registry"

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        cfg = ctx.require(KernelConfig).value
        self._registry = LlmRegistry(cfg)
        self._factories: dict[str, tuple[Callable[[Any], Any], str, str]] = {}
        self._native_factories: dict[str, tuple[Callable[[Any], Any], str, str]] = {}

    @property
    def registry(self) -> LlmRegistry:
        return self._registry

    def provider(self, name: str) -> Any:
        """Resolve a provider: plugin contribution first, config second."""
        entry = self._factories.get(name)
        if entry is not None:
            return entry[0](self)
        return self._registry.get(name)

    def add_provider(
        self,
        name: str,
        factory: Callable[[Any], Any],
        *,
        plugin_id: str = "",
        description: str = "",
    ) -> Callable[[], None]:
        """Register a provider factory; the returned callable removes it."""
        if not name:
            raise PluginDependencyError("provider name must not be empty")
        if name in self._factories:
            owner = self._factories[name][1]
            raise PluginDependencyError(f"provider {name!r} is already registered by {owner!r}")
        self._factories[name] = (factory, plugin_id, description)

        def remove() -> None:
            self._factories.pop(name, None)

        return remove

    def native_provider(self, name: str) -> Any:
        """Resolve the native (KI-010) provider for the native agent loop.

        Plugin contributions win over configuration, exactly like provider(). Both
        configured api kinds now have a native transport (OpenAI-compatible and Anthropic
        Messages); a construction failure raises ConfigError and propagates to the caller,
        which fails the native path instead of falling back to the v4 loop.
        """
        entry = self._native_factories.get(name)
        if entry is not None:
            return entry[0](self)
        return self._registry.native_provider(name)

    def add_native_provider(
        self,
        name: str,
        factory: Callable[[Any], Any],
        *,
        plugin_id: str = "",
        description: str = "",
    ) -> Callable[[], None]:
        """Register a native provider factory; the returned callable removes it."""
        if not name:
            raise PluginDependencyError("provider name must not be empty")
        if name in self._native_factories:
            owner = self._native_factories[name][1]
            raise PluginDependencyError(f"provider {name!r} is already registered by {owner!r}")
        self._native_factories[name] = (factory, plugin_id, description)

        def remove() -> None:
            self._native_factories.pop(name, None)

        return remove

    def provider_names(self) -> list[str]:
        names = set(self._registry.providers)
        names.update(self._factories)
        names.update(self._native_factories)
        return sorted(names)

    def descriptions(self) -> dict[str, str]:
        descriptions = {name: entry[2] for name, entry in self._factories.items() if entry[2]}
        descriptions.update({name: entry[2] for name, entry in self._native_factories.items() if entry[2]})
        return descriptions

    async def list_models(self, provider: str, *, refresh: bool = False) -> list[ModelInfo]:
        """Catalog entries for one provider.

        Configured providers forward to the registry (cached discovery merged over
        config); plugin-registered factories are instantiated and asked directly, and
        contribute nothing when they do not implement list_models().
        """
        entry = self._factories.get(provider)
        if entry is not None:
            instance = entry[0](self)
            lister = getattr(instance, "list_models", None)
            if lister is None:
                return []
            return list(await lister())
        return await self._registry.list_models(provider, refresh=refresh)

    async def model_info(self, provider: str, model_id: str) -> ModelInfo | None:
        """One catalog entry by model id, or None when the provider does not list it."""
        entry = self._factories.get(provider)
        if entry is not None:
            instance = entry[0](self)
            lister = getattr(instance, "list_models", None)
            if lister is None:
                return None
            for info in await lister():
                if info.id == model_id:
                    return info
            return None
        return await self._registry.model_info(provider, model_id)

    def catalog_snapshot(self, provider: str) -> list[ModelInfo]:
        """Best catalog available without awaiting (inspect/TUI rendering)."""
        if provider in self._factories:
            return []
        return self._registry.catalog_snapshot(provider)

    async def stop(self) -> None:
        await self._registry.aclose()
