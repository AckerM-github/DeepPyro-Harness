"""The llm service: configured native providers plus dynamically registered ones."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from deeppyro.api import PluginConfig, PluginContext, PluginDependencyError, Service
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.llm import LlmRegistry, ModelInfo


class LlmPlugin(Service):
    """Hosts the native provider registry under the llm service name."""

    provide = "llm"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "llm"
    version = "0.3.0"
    description = "LLM provider registry"

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        cfg = ctx.require(KernelConfig).value
        self._registry = LlmRegistry(cfg)
        self._factories: dict[str, tuple[Callable[[Any], Any], str, str]] = {}

    @property
    def registry(self) -> LlmRegistry:
        return self._registry

    def provider(self, name: str) -> Any:
        """Resolve the native provider: plugin contribution first, config second.

        Compatibility alias for native_provider(); the v4 transport no longer exists,
        so this returns a deeppyro.core.llm provider (or raises ConfigError).
        """
        return self.native_provider(name)

    def native_provider(self, name: str) -> Any:
        """Resolve the native provider for the agent loop.

        Plugin contributions win over configuration. Both configured api kinds have a
        native transport; a construction failure raises ConfigError and propagates to the
        caller instead of falling back to another transport.
        """
        entry = self._factories.get(name)
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
        if name in self._factories:
            owner = self._factories[name][1]
            raise PluginDependencyError(f"provider {name!r} is already registered by {owner!r}")
        self._factories[name] = (factory, plugin_id, description)

        def remove() -> None:
            self._factories.pop(name, None)

        return remove

    def add_provider(
        self,
        name: str,
        factory: Callable[[Any], Any],
        *,
        plugin_id: str = "",
        description: str = "",
    ) -> Callable[[], None]:
        """Backward-compatible alias for add_native_provider().

        PluginContext.register_provider() keeps its name; factories registered through it
        must build native providers because the v4 transport layer was removed.
        """
        return self.add_native_provider(
            name,
            factory,
            plugin_id=plugin_id,
            description=description,
        )

    def provider_names(self) -> list[str]:
        names = set(self._registry.providers)
        names.update(self._factories)
        return sorted(names)

    def descriptions(self) -> dict[str, str]:
        descriptions = {name: entry[2] for name, entry in self._factories.items() if entry[2]}
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
