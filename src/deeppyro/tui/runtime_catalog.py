"""Layer 6 of the TUI runtime: commands and the provider/model catalog."""

from __future__ import annotations

from typing import Any

from deeppyro.core.errors import ConfigError
from deeppyro.tui.runtime_preset import PresetLayer
from deeppyro.tui.runtime_support import await_maybe


class CatalogLayer(PresetLayer):
    """Layer 6 of the TUI runtime: commands and the provider/model catalog."""

    @property
    def commands(self) -> Any | None:
        """The kernel commands service, when the bundle provides one."""
        app = self.kernel_app
        if app is None:
            return None
        try:
            return app.service("commands")
        except LookupError:
            return None

    def models(self, provider: str | None = None) -> list[str]:
        """Model ids configured for one provider (the active one by default)."""
        provider_cfg = self._provider_config(provider or self.provider_name)
        return [str(entry.id) for entry in list(provider_cfg.models or [])]

    def model_choices(self) -> list[tuple[str, str]]:
        """Every (provider, model) pair: configured first, then discovered."""
        choices: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for name in self.providers():
            for model_id in self.models(name):
                pair = (name, model_id)
                if pair not in seen:
                    seen.add(pair)
                    choices.append(pair)
        for name in self.providers():
            for info in self.cached_catalog(name):
                model_id = str(getattr(info, "id", "") or "")
                pair = (name, model_id)
                if model_id and pair not in seen:
                    seen.add(pair)
                    choices.append(pair)
        return choices

    def _llm_service(self) -> Any | None:
        """The kernel llm service, when the bundle loaded one."""
        service = self.llm
        if service is not None:
            return service
        app = self.kernel_app
        if app is None:
            return None
        try:
            return app.service("llm")
        except (AttributeError, LookupError):
            return None

    def cached_catalog(self, provider: str) -> list[Any]:
        """Catalog entries remembered for one provider (no I/O)."""
        return list(self._catalog.get(provider, {}).values())

    def _remember_catalog(self, provider: str, entries: Any) -> int:
        """Merge catalog entries into the local cache; returns the stored count."""
        bucket = self._catalog.setdefault(provider, {})
        stored = 0
        for entry in entries or []:
            model_id = str(getattr(entry, "id", "") or "")
            if not model_id:
                continue
            bucket[model_id] = entry
            stored += 1
        return stored

    @staticmethod
    def _catalog_error(llm: Any) -> str | None:
        """Last discovery failure reported by the llm service registry, if any."""
        registry = getattr(llm, "registry", None)
        error = getattr(registry, "last_catalog_error", None)
        if error is None:
            return None
        return str(error) or error.__class__.__name__

    async def model_catalog_info(self, provider: str, model_id: str) -> Any | None:
        """One catalog entry, querying the llm service only when not cached."""
        cached = self._cached_model(provider, model_id)
        if cached is not None:
            return cached
        llm = self._llm_service()
        lookup = getattr(llm, "model_info", None)
        if not callable(lookup):
            return None
        try:
            info = await await_maybe(lookup(provider, model_id))
        except Exception as exc:  # noqa: BLE001 - discovery must not crash the UI
            self.last_catalog_error = str(exc) or exc.__class__.__name__
            return None
        if info is not None:
            self._remember_catalog(provider, [info])
        return info

    def has_api_key(self, provider: str) -> bool:
        """Whether the provider can resolve an API key right now."""
        provider_cfg = self.cfg.providers.get(provider)
        if provider_cfg is None:
            return False
        try:
            return bool(provider_cfg.resolve_api_key())
        except Exception:  # noqa: BLE001 - display only
            return False

    async def _discovered_model(self, provider_name: str, model_id: str) -> Any | None:
        """Catalog entry for a model the config does not declare (None if configured).

        Raises ConfigError only for a definitive miss: the provider has configured
        models and neither the cache nor the llm service lists the model.
        """
        if not model_id:
            return None
        available = self.models(provider_name)
        if model_id in available:
            return None
        info = self._cached_model(provider_name, model_id)
        if info is not None:
            return info
        info = await self.model_catalog_info(provider_name, model_id)
        if info is None and available:
            raise ConfigError(
                f"unknown model: {model_id} (provider {provider_name} offers {', '.join(available)})"
            )
        return info
