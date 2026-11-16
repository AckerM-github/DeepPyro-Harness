"""Layer 4 of the TUI runtime: provider activation and opening sessions."""

from __future__ import annotations

import contextlib
from typing import Any

from deeppyro.api import DEFAULT_PRESET_ID, TEAM_PRESET_ID, PluginDependencyError
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.tui.runtime_session import SessionLayer
from deeppyro.tui.runtime_support import _context_window_for, _recorded_model_selection, _recorded_preset


class ModelLayer(SessionLayer):
    """Layer 4 of the TUI runtime: provider activation and opening sessions."""

    async def open_session(self, session_id: str) -> Any:
        """Open an existing session and rebuild the agent around it."""
        self._save_projections()
        session = self.store.open(session_id)
        self._pin_permission(session)
        selection = _recorded_model_selection(session)
        if selection is not None:
            provider_name, model_id = selection
            if provider_name in self.cfg.providers:
                # resume must not brick the UI
                with contextlib.suppress(Exception):
                    self._activate_provider(
                        provider_name,
                        model_id,
                        info=self._cached_model(provider_name, model_id),
                    )
        header_preset = (
            self._canonical_preset(getattr(session.header, "agentPreset", "")) or DEFAULT_PRESET_ID
        )
        recorded = _recorded_preset(session)
        recorded_preset = (
            self._canonical_preset(recorded[0]) if recorded is not None and recorded[0] else None
        )
        preset = recorded_preset or header_preset
        revision = recorded[1] if recorded is not None else None
        previous = self._canonical_preset(recorded[2]) if recorded is not None and recorded[2] else None
        self.agent_preset = preset
        self.preset_revision = revision
        if self.agent_preset == TEAM_PRESET_ID:
            fallback = header_preset if header_preset != TEAM_PRESET_ID else DEFAULT_PRESET_ID
            self.team_previous_preset = previous or fallback
        else:
            self.team_previous_preset = DEFAULT_PRESET_ID
        await self._ensure_preset(self.agent_preset, self.preset_revision)
        try:
            await self._attach(session)
        except PluginDependencyError:
            if self.preset_revision is None:
                raise
            # The recorded revision may have been dropped by a preset edit;
            # fall back to the newest revision of the same preset id.
            self.preset_revision = None
            await self._ensure_preset(self.agent_preset, None)
            await self._attach(session)
        return session

    def providers(self) -> list[str]:
        """Configured provider names, in config order."""
        return [str(name) for name in self.cfg.providers]

    def _cached_model(self, provider: str, model_id: str) -> Any | None:
        """One remembered catalog entry, or None."""
        return self._catalog.get(provider, {}).get(model_id)

    def _provider_config(self, name: str) -> Any:
        provider_cfg = self.cfg.providers.get(name)
        if provider_cfg is None:
            choices = ", ".join(self.providers()) or "(none configured)"
            raise ConfigError(f"unknown provider: {name} (choose from {choices})")
        return provider_cfg

    def _build_provider(self, provider_name: str) -> Any:
        app = self.kernel_app
        if app is None:
            raise DeeppyroError("cannot switch provider after the kernel has stopped")
        try:
            service = app.service("llm")
        except LookupError as exc:
            raise DeeppyroError("llm service is not available") from exc
        try:
            return service.provider(provider_name)
        except KeyError as exc:
            raise ConfigError(f"unknown provider: {provider_name}") from exc

    def _activate_provider(self, provider_name: str, model_id: str, *, info: Any = None) -> None:
        """Swap the provider client and model without touching the session."""
        provider_cfg = self._provider_config(provider_name)
        provider = self._build_provider(provider_name)
        self.provider = provider
        self.provider_name = provider_name
        self.model = model_id
        self.context_window = _context_window_for(provider_cfg, model_id, info)
