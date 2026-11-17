"""Layer 3 of the TUI runtime: team/workspace setup, records, and new sessions."""

from __future__ import annotations

import contextlib
from typing import Any

from deeppyro.api import AGENT_MODE_TEAM, DEFAULT_PRESET_ID
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.tui.runtime_schedule import ScheduleLayer
from deeppyro.tui.runtime_support import await_maybe, logger


class SessionLayer(ScheduleLayer):
    """Layer 3 of the TUI runtime: team/workspace setup, records, and new sessions."""

    async def new_session(self) -> Any:
        """Create a fresh durable session and rebuild the agent."""
        self._save_projections()
        preset = self._canonical_preset(self.agent_preset) or DEFAULT_PRESET_ID
        self.agent_preset = preset
        self.preset_revision = None
        session = self.store.create(str(self.cwd), agent_preset=preset)
        self._pin_permission(session)
        await self._session_created(session, cwd=str(self.cwd), preset=preset)
        await self._attach(session)
        self._record_model_selection()
        self._record_preset()
        return session

    async def _attach(self, session: Any) -> None:
        options = self.agents.options(
            model=self.model,
            context_window=self.context_window,
        )
        agent = self.agents.create(
            session=session,
            provider=self.provider,
            registry=self._scoped_registry(self.cwd),
            options=options,
            sink=self.sink,
            permission=self.permission,
            cwd=str(self.cwd),
            services=self._plugin_services(),
            preset=self.agent_preset,
            preset_revision=self.preset_revision,
        )
        close_agent = getattr(self.agent, "aclose", None)
        if close_agent is not None:
            await await_maybe(close_agent())
        self.session = session
        self.agent = agent
        self.preset_revision = getattr(agent, "preset_revision", None)
        self._configure_team(session)
        self._configure_subagent(session)
        self._configure_working_directory(session)
        self._restore_projections(session)
        if self.agent_mode == AGENT_MODE_TEAM:
            await self._restore_team(session)

    def _save_projections(self) -> None:
        """Write the live session's projection sidecar and stop observing it."""
        service = self.projections
        session = self.session
        if service is None or session is None:
            return
        save = getattr(service, "save", None)
        detach = getattr(service, "detach", None)
        # an optional service must not break a session
        with contextlib.suppress(Exception):
            if save is not None:
                save(session)
        # an optional service must not break a session
        with contextlib.suppress(Exception):
            if detach is not None:
                detach(session)

    def _restore_projections(self, session: Any) -> None:
        """Restore the session's projection sidecar and start observing appends."""
        service = self.projections
        if service is None or session is None:
            return
        attach = getattr(service, "attach", None)
        if attach is None:
            return
        # an optional service must not break a session
        with contextlib.suppress(Exception):
            attach(session)

    def _configure_working_directory(self, session: Any) -> None:
        """Bind the live session so cd records and notices reach its log."""
        service = self.working_directory
        if service is None or session is None:
            return
        bind = getattr(service, "bind", None)
        if bind is None:
            return
        header = getattr(session, "header", None)
        session_cwd = getattr(header, "cwd", None) or self.cwd
        # an optional service must not break a session
        with contextlib.suppress(Exception):
            bind(
                session,
                cwd=session_cwd,
                record=session.append,
                inject_notice=lambda text: session.inject(text, source={"kind": "working-directory"}),
            )

    def _configure_team(self, session: Any) -> None:
        """Keep the team service pointed at the live lead session context."""
        service = self.team
        if service is None:
            return
        configure = getattr(service, "configure", None)
        # an optional service must not break a session
        with contextlib.suppress(Exception):
            if configure is not None:
                configure(
                    cwd=self.cwd,
                    provider=self.provider,
                    registry=self._scoped_registry(self.cwd),
                    permission=self.permission,
                    model=self.model,
                    context_window=self.context_window,
                    preset=self.agent_preset,
                    preset_revision=self.preset_revision,
                    services=self._plugin_services(),
                    ask=self.ask,
                )
            bind = getattr(service, "bind_session", None)
            if bind is not None and session is not None:
                bind(session)

    def _configure_subagent(self, session: Any) -> None:
        """Keep the subagent service pointed at the live lead session context."""
        service = self.subagent
        if service is None:
            return
        configure = getattr(service, "configure", None)
        # an optional service must not break a session
        with contextlib.suppress(Exception):
            if configure is not None:
                configure(
                    cwd=self.cwd,
                    provider=self.provider,
                    registry=self._scoped_registry(self.cwd),
                    permission=self.permission,
                    model=self.model,
                    context_window=self.context_window,
                    preset=self.agent_preset,
                    preset_revision=self.preset_revision,
                    services=self._plugin_services(),
                    ask=self.ask,
                )
            bind = getattr(service, "bind_session", None)
            if bind is not None and session is not None:
                bind(session)
            bind_parent = getattr(service, "bind_parent", None)
            if bind_parent is not None and self.agent is not None:
                bind_parent(self.agent)

    async def _restore_team(self, session: Any) -> None:
        """Rebuild team state from the lead session log (cold resume).

        A session is restored at most once per process: switching presets or
        rebinding a workspace re-attaches the same session and must not clear a
        live roster.  The team service itself is idempotent, but auto-resumed
        members would be orphaned by a second replay, hence the session guard.
        """
        service = self.team
        if service is None:
            return
        restore = getattr(service, "restore", None)
        if not callable(restore):
            return
        header = getattr(session, "header", None)
        session_id = str(getattr(header, "id", "") or getattr(session, "session_id", "") or "").strip()
        if session_id:
            if session_id in self._restored_team_sessions:
                return
            self._restored_team_sessions.add(session_id)
        try:
            await await_maybe(restore())
        except Exception:  # noqa: BLE001 - team restore runs plugin code and must not break opening
            logger.warning("failed to restore team state", exc_info=True)

    def _agent_preset_spec(self) -> Any | None:
        """Resolve the active preset spec (None when the service is unavailable)."""
        service = self._service(PresetsPlugin)
        resolver = getattr(service, "resolve", None)
        if not callable(resolver):
            return None
        try:
            return resolver(self.agent_preset, revision=self.preset_revision)
        except Exception:  # noqa: BLE001 - status display must never fail
            return None

    @property
    def agent_mode(self) -> str:
        """Mode of the active agent preset ("default" or "team")."""
        spec = self._agent_preset_spec()
        return str(getattr(spec, "mode", "default") or "default")

    def _record_preset(self) -> None:
        """Persist the pinned preset revision so a resume reproduces it."""
        mode = self.agent_mode
        preset = self._canonical_preset(self.agent_preset) or DEFAULT_PRESET_ID
        data: dict[str, Any] = {"preset": preset, "mode": mode}
        if self.preset_revision is not None:
            data["revision"] = self.preset_revision
        if mode == AGENT_MODE_TEAM:
            data["previous"] = self._canonical_preset(self.team_previous_preset) or DEFAULT_PRESET_ID
        self._record("agent-preset/selected", data)

    def _record_model_selection(self) -> None:
        """Persist the active provider/model so a resume reproduces it."""
        self._record("model/selection", {"provider": self.provider_name, "model": self.model})

    def _record(self, record_type: str, data: dict[str, Any]) -> None:
        """Append one non-surface event to the active session (best effort)."""
        append = getattr(self.session, "append", None)
        if callable(append):
            append(record_type, data)
