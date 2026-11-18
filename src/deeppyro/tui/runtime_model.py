"""Layer 4 of the TUI runtime: provider activation and opening sessions."""

from __future__ import annotations

import contextlib
from typing import Any

from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.api import DEFAULT_PRESET_ID, TEAM_PRESET_ID, PluginDependencyError
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.tui.runtime_session import SessionLayer
from deeppyro.tui.runtime_support import _context_window_for


class ModelLayer(SessionLayer):
    """Layer 4 of the TUI runtime: provider activation and opening sessions."""

    async def open_session(self, session_id: str) -> Any:
        """Open an existing native session and rebuild the agent around it."""
        handle = NativeSessionHandle(self.sessions.native_open(session_id))
        view = handle.fold()
        state = view.state
        model = getattr(state, "model", None)
        if model is not None:
            provider_name = str(getattr(model, "provider", "") or "")
            if provider_name and provider_name in self.cfg.providers:
                provider_cfg = self.cfg.providers[provider_name]
                # a stale provider must not brick the resume
                with contextlib.suppress(Exception):
                    self.provider = self._build_provider(provider_name)
                    self.provider_name = provider_name
                    self.model = str(getattr(model, "model", "") or self.model)
                    self.context_window = _context_window_for(
                        provider_cfg, self.model, self._cached_model(provider_name, self.model)
                    )
        preset = getattr(state, "preset", None)
        recorded_preset = self._canonical_preset(getattr(preset, "preset", None) or "") or DEFAULT_PRESET_ID
        revision = getattr(preset, "revision", None)
        previous = self._canonical_preset(getattr(preset, "previous", None) or "") or DEFAULT_PRESET_ID
        self.agent_preset = recorded_preset
        self.preset_revision = revision if isinstance(revision, int) else None
        self.team_previous_preset = previous if recorded_preset == TEAM_PRESET_ID else DEFAULT_PRESET_ID
        await self._ensure_preset(self.agent_preset, self.preset_revision)
        try:
            await self._attach(handle)
        except PluginDependencyError:
            if self.preset_revision is None:
                raise
            # The recorded revision may have been dropped by a preset edit;
            # fall back to the newest revision of the same preset id.
            self.preset_revision = None
            await self._ensure_preset(self.agent_preset, None)
            await self._attach(handle)
        self._emit_session_event(handle)
        return handle

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
        service = app.get(LlmPlugin)
        if service is None:
            raise DeeppyroError("llm service is not available")
        try:
            return service.native_provider(provider_name)
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
