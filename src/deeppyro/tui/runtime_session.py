"""Layer 3 of the TUI runtime: team/workspace setup, records, and new sessions."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from deeppyro.agent.native_factory import AgentBuild
from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.api import AGENT_MODE_TEAM, AGENT_PRESENTATION_PTC, DEFAULT_PRESET_ID
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.core.errors import DeeppyroError
from deeppyro.ptc.native_adapter import build_ptc_host
from deeppyro.ptc.runtime import PtcRuntime
from deeppyro.session.native_control import NewSession
from deeppyro.tui.native_facts import native_view
from deeppyro.tui.runtime_schedule import ScheduleLayer
from deeppyro.tui.runtime_support import await_maybe, logger

#: Session header fields that preset application must never rewrite. The v4
#: wire header has no model/system field, but a host header may grow one, so
#: the snapshot covers every field whose presence would change session meaning.
_HEADER_FIELDS = (
    "id",
    "createdAt",
    "cwd",
    "isSeeded",
    "delegationDepth",
    "agentPreset",
    "parentSession",
    "origin",
    "model",
    "system",
)


def _session_header_snapshot(session: Any) -> tuple[Any, dict[str, Any]]:
    """Capture the header object and the fields a preset must leave untouched."""
    header = getattr(session, "header", None)
    return header, {name: getattr(header, name, None) for name in _HEADER_FIELDS}


def _assert_session_header_unchanged(
    session: Any,
    snapshot: tuple[Any, dict[str, Any]],
    *,
    preset: str | None,
) -> None:
    """Reject a preset application that rewrote an existing session header.

    The header records what the session was created as: preset application may
    rebuild the live agent, but changing cwd/model/system (or the preset id in
    the header) is reserved for creating a new session or an explicit fork.
    """
    header, values = snapshot
    current = getattr(session, "header", None)
    changed = [name for name, value in values.items() if getattr(current, name, None) != value]
    if current is not header:
        changed.insert(0, "header")
    if changed:
        raise DeeppyroError(
            f"agent preset {preset!r} rewrote the session header ({', '.join(changed)}); "
            "applying a preset must not change an existing session header — "
            "create a new session or fork to change cwd/model/system"
        )


class SessionLayer(ScheduleLayer):
    """Layer 3 of the TUI runtime: team/workspace setup, records, and new sessions."""

    async def new_session(self) -> Any:
        """Create a fresh durable session and rebuild the agent."""
        if self.native:
            return await self._new_native_session()
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

    async def _new_native_session(self) -> Any:
        """Create a schema 1 session and build the native agent host around it."""
        preset = self._canonical_preset(self.agent_preset) or DEFAULT_PRESET_ID
        self.agent_preset = preset
        self.preset_revision = None
        handle = NativeSessionHandle(
            self.sessions.native_create(NewSession(cwd=self.cwd, preset=preset, origin="user"))
        )
        self._pin_permission(handle)
        await self._session_created(handle, cwd=str(self.cwd), preset=preset)
        await self._attach_native(handle)
        self._record_model_selection()
        self._record_preset()
        self._emit_session_event(handle)
        return handle

    async def _attach(self, session: Any) -> None:
        if self.native:
            await self._attach_native(session)
            return
        header_snapshot = _session_header_snapshot(session)
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
        _assert_session_header_unchanged(session, header_snapshot, preset=self.agent_preset)
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

    async def _attach_native(self, session: Any) -> None:
        """Build the native agent host for a schema 1 session handle.

        Only the native seam is touched here: no v4 header snapshot, no session
        store, no v4 agent. Reattaching the same session (model, preset or
        permission switch) rebuilds the host while keeping the log open.
        """
        spec = self._agent_preset_spec()
        handle = session if getattr(session, "native_session", None) is not None else None
        if handle is None:
            raise DeeppyroError("native attach requires a native session handle")
        build = AgentBuild(
            provider=self.provider_name,
            model=self.model,
            cwd=Path(self.cwd),
            preset=self.agent_preset,
            preset_revision=self.preset_revision,
            permission=self._bound_permission(handle),
            hooks=self.hooks,
            prompt_facts=self._prompt_facts(handle),
            services=self._plugin_services(),
            context_window=self.context_window,
        )
        ptc = self._service(PtcRuntime)
        legacy_ptc = ptc is not None and str(getattr(ptc, "mode", "native") or "native") == "ptc"
        if str(getattr(spec, "presentation", "") or "") == AGENT_PRESENTATION_PTC or legacy_ptc:
            host = build_ptc_host(
                handle.native_session,
                build,
                prompt=self.prompt,
                presets=self._service(PresetsPlugin),
                llm=self.llm,
                tools=self._service(ToolsPlugin),
                ptc=ptc,
                default_model=self.model,
                default_max_steps=getattr(self.cfg.agent, "max_steps", None),
            )
        else:
            host = self.agents.host(handle.native_session, build)
        old_agent = self.agent
        if old_agent is not None and getattr(old_agent, "native_session", None) is not handle.native_session:
            close_agent = getattr(old_agent, "aclose", None)
            if close_agent is not None:
                await await_maybe(close_agent())
        self.session = host.handle
        self.agent = host
        self.preset_revision = getattr(host, "preset_revision", self.preset_revision)
        self._configure_team(host.handle)
        self._configure_subagent(host.handle)
        self._configure_working_directory(host.handle)
        if self.agent_mode == AGENT_MODE_TEAM:
            await self._restore_team(host.handle)

    def _bound_permission(self, session: Any) -> Any:
        """Bind the shared permission gate to the session's native facts."""
        gate = self.permission
        bind = getattr(gate, "bind_session", None)
        if callable(bind):
            # a stale gate must not break the attach
            with contextlib.suppress(Exception):
                bind(native_view(session))
        return gate

    async def _detach(self) -> None:
        """Close the live native agent and release the session lock (best effort)."""
        agent, session = self.agent, self.session
        unbind = getattr(self.working_directory, "unbind", None)
        if callable(unbind) and session is not None:
            with contextlib.suppress(Exception):
                unbind(session)
        self.agent = None
        self.session = None
        hooks = self.hooks
        reset = getattr(hooks, "reset", None)
        if callable(reset):
            reset()
        if agent is None:
            return
        close = getattr(agent, "aclose", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await await_maybe(close())

    def _emit_session_event(self, session: Any) -> None:
        """Tell the UI which native session is live."""
        session_id = str(getattr(session, "session_id", "") or "")
        if not session_id:
            return
        sink = self.sink
        emit = getattr(sink, "emit", None)
        if callable(emit):
            emit({"type": "session", "sessionId": session_id})

    def _save_projections(self) -> None:
        """Write the live session's projection sidecar and stop observing it."""
        if self.native:
            return
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
        if self.native:
            return
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
        if self.native:
            view = native_view(session)
            state = view.fold().state if view is not None else None
            session_cwd = getattr(state, "cwd", None) or session_cwd
            notice = self.working_directory_notice
            # an optional service must not break a session
            with contextlib.suppress(Exception):
                bind(
                    session,
                    cwd=session_cwd,
                    record=session.append,
                    inject_notice=notice
                    if notice is not None
                    else lambda text: session.inject(text, source={"kind": "working-directory"}),
                )
            return
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
            if self.native:
                bind_parent = getattr(service, "bind_parent", None)
                if bind_parent is not None and self.agent is not None:
                    bind_parent(self.agent)

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
