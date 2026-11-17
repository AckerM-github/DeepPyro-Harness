"""Layer 5 of the TUI runtime: presentation and preset switching."""

from __future__ import annotations

from typing import Any

from deeppyro.api import (
    AGENT_MODE_DEFAULT,
    AGENT_MODE_TEAM,
    AGENT_PRESENTATION_NATIVE,
    DEFAULT_PRESET_ID,
    TEAM_PRESET_ID,
)
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.core.errors import DeeppyroError
from deeppyro.hooks import SESSION_DISPOSED
from deeppyro.tui.runtime_model import ModelLayer
from deeppyro.tui.runtime_support import logger


class PresetLayer(ModelLayer):
    """Layer 5 of the TUI runtime: presentation and preset switching."""

    @property
    def agent_presentation(self) -> str:
        """Tool presentation of the active agent ("native" or "ptc")."""
        presentation = str(getattr(self.agent, "presentation", "") or "").strip()
        if presentation:
            return presentation
        spec = self._agent_preset_spec()
        presentation = str(getattr(spec, "presentation", "") or "").strip()
        return presentation or AGENT_PRESENTATION_NATIVE

    @property
    def agent_preset_display(self) -> str:
        """Human display name of the active preset (name, else id)."""
        spec = self._agent_preset_spec()
        display_name = getattr(spec, "display_name", None)
        if callable(display_name):
            try:
                value = str(display_name() or "").strip()
            except Exception:  # noqa: BLE001 - display must never fail
                value = ""
            if value:
                return value
        name = str(getattr(spec, "name", "") or "").strip()
        return name or self.agent_preset

    async def _dispose_sessions(self) -> None:
        # Sorted so shutdown is deterministic even though the set does not keep
        # insertion order.
        session_ids = sorted(self._created_sessions)
        self._created_sessions.clear()
        for session_id in session_ids:
            await self._emit_hook(SESSION_DISPOSED, {"sessionId": session_id})

    async def set_agent_preset(self, preset: str) -> str:
        """Switch the preset used by subsequent agents on this runtime.

        Hidden presets are reserved for their dedicated command: team mode is
        entered with /team on, so /preset team is rejected with a hint instead
        of silently switching.
        """
        service = self._service(PresetsPlugin)
        if service is None:
            raise DeeppyroError("agent presets are not available in this bundle")
        wanted = self._canonical_preset(preset)
        spec = self._resolve_preset(service, wanted or "")
        spec_id = self._canonical_preset(spec.id) or str(spec.id)
        mode = str(getattr(spec, "mode", AGENT_MODE_DEFAULT) or AGENT_MODE_DEFAULT)
        if mode == AGENT_MODE_TEAM or spec_id == TEAM_PRESET_ID or getattr(spec, "hidden", False):
            raise DeeppyroError("team mode is enabled with /team on, not /preset")
        if self.busy:
            raise DeeppyroError("cannot switch agent preset while a turn is running")
        current = self._canonical_preset(self.agent_preset) or DEFAULT_PRESET_ID
        if spec_id == current and self.preset_revision is None:
            return spec_id
        await self._switch_preset(spec_id, None)
        self._record_preset()
        return spec_id

    def _resolve_preset(self, service: Any, preset: str) -> Any:
        """Resolve a preset id, translating a service error for the UI."""
        try:
            return service.resolve(preset or None)
        except Exception as exc:  # surface the service message
            raise DeeppyroError(str(exc)) from exc

    async def _switch_preset(self, preset_id: str, revision: int | None) -> None:
        """Switch preset/agent atomically; restore the old pair on failure."""
        service = self._service(PresetsPlugin)
        if service is None:
            raise DeeppyroError("agent presets are not available in this bundle")
        spec = self._resolve_preset(service, preset_id)
        preset_id = self._canonical_preset(spec.id) or str(spec.id)
        old_preset = self.agent_preset
        old_revision = self.preset_revision
        old_agent = self.agent
        await self._ensure_preset(preset_id, revision)
        self.agent_preset = preset_id
        self.preset_revision = revision
        if self.session is None:
            return
        try:
            await self._attach(self.session)
        except Exception:
            self.agent_preset = old_preset
            self.preset_revision = old_revision
            if self.agent is not old_agent:
                # _attach got far enough to replace (and close) the old agent;
                # rebuild it so the UI keeps a working agent after a rollback.
                try:
                    await self._attach(self.session)
                except Exception:  # noqa: BLE001 - rollback must not mask the preset failure
                    logger.exception("failed to restore the agent after a preset switch failure")
            raise

    @property
    def busy(self) -> bool:
        return bool(getattr(self.agent, "busy", False))
