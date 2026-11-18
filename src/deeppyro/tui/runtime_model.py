"""Layer 4 of the TUI runtime: provider activation and opening sessions."""

from __future__ import annotations

import contextlib
from typing import Any

from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.api import DEFAULT_PRESET_ID, TEAM_PRESET_ID, PluginDependencyError
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.tui.runtime_session import SessionLayer
from deeppyro.tui.runtime_support import _context_window_for, _recorded_model_selection, _recorded_preset


class ModelLayer(SessionLayer):
    """Layer 4 of the TUI runtime: provider activation and opening sessions."""

    async def open_session(self, session_id: str) -> Any:
        """Open an existing session and rebuild the agent around it."""
        if self.native:
            return await self._open_native_session(session_id)
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

    async def _open_native_session(self, session_id: str) -> Any:
        """Open a schema 1 session, hydrate the agent and record the selection."""
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
        await self._attach_native(handle)
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
            if self.native:
                return service.native_provider(provider_name)
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
