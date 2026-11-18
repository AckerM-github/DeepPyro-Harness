"""Runtime assembly for the terminal UI.

The runtime state, optional service lookups, scheduling, session setup, preset
and model catalog, and status APIs live in the layered ``runtime_*`` modules
under ``deeppyro.tui``. This module is the facade around them: it keeps the
construction entry point, the schedule start seam, team-mode switching, the
provider/model commands, and shutdown, and re-exports the module-level helpers
and the schedule host that were historically defined here.

It is the TUI's only seam to the domain layers, mirroring
``deeppyro.headless.boot_runtime``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro.api import AGENT_MODE_TEAM, DEFAULT_PRESET_ID, TEAM_PRESET_ID
from deeppyro.builtin.activity import ActivityService
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.projections import ProjectionsService
from deeppyro.builtin.prompt_service import PromptPlugin
from deeppyro.builtin.sandbox import SandboxPlugin
from deeppyro.builtin.schedule import SchedulePlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.config import Config, load_config, save_agent_selection
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.jobs.manager import JobsService
from deeppyro.observability.protocols import TraceExportSink
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.subagent.manager import SubagentService
from deeppyro.team.manager import TeamService
from deeppyro.team.types import LIVE_PHASES
from deeppyro.tui.native_hooks import TuiAgentHooks
from deeppyro.tui.native_mode import tui_native_enabled
from deeppyro.tui.runtime_schedule_host import _now_rfc3339 as _now_rfc3339
from deeppyro.tui.runtime_schedule_host import _ScheduleSink as _ScheduleSink
from deeppyro.tui.runtime_schedule_host import _TuiScheduleHost as _TuiScheduleHost
from deeppyro.tui.runtime_status import StatusLayer
from deeppyro.tui.runtime_support import DEFAULT_CONTEXT_WINDOW as DEFAULT_CONTEXT_WINDOW
from deeppyro.tui.runtime_support import _context_window as _context_window
from deeppyro.tui.runtime_support import _context_window_for as _context_window_for
from deeppyro.tui.runtime_support import _recorded_model_selection as _recorded_model_selection
from deeppyro.tui.runtime_support import _recorded_preset as _recorded_preset
from deeppyro.tui.runtime_support import _resolve_model as _resolve_model
from deeppyro.tui.runtime_support import await_maybe as await_maybe
from deeppyro.tui.runtime_support import logger as logger
from deeppyro.tui.runtime_support import resolve_workspace as resolve_workspace
from deeppyro.user_questions.service import UserQuestionsService
from deeppyro.working_directory.service import WorkingDirectoryService


def _native_compactor(kernel_app: Any, provider: Any, model: str) -> Any:
    """Best-effort native compactor built from the optional compact plugin."""
    try:
        from deeppyro.compact.native_service import NativeCompactPlugin
    except Exception:  # noqa: BLE001 - the optional module may be absent
        return None
    try:
        plugin = kernel_app.get(NativeCompactPlugin)
    except Exception:  # noqa: BLE001 - an optional service must not break boot
        return None
    attach = getattr(plugin, "attach", None)
    if not callable(attach):
        return None
    try:
        return attach(provider=provider, model=model)
    except Exception:  # noqa: BLE001 - compaction is optional
        logger.debug("native compactor unavailable", exc_info=True)
        return None


@dataclass
class TuiRuntime(StatusLayer):
    """Booted kernel plus the live session/agent pair for the TUI."""

    @classmethod
    async def boot(
        cls,
        *,
        sink: Any,
        ask: Callable[[Any], Awaitable[bool]],
        cwd: str | Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        config: Config | None = None,
        resume: str | None = None,
        native: bool | None = None,
    ) -> TuiRuntime:
        """Boot the kernel bundle and resolve the TUI runtime.

        When DEEPPYRO_TUI_NATIVE is enabled (or native=True is passed) the
        runtime is assembled from the schema 1 control plane: SessionsPlugin
        native APIs, AgentsPlugin.host and the native agent factory. The v4
        path stays byte-for-byte unchanged when the flag is off.
        """
        from deeppyro.kernel import boot

        native_mode = tui_native_enabled(native)
        cfg = config if config is not None else load_config()
        workspace = resolve_workspace(cwd)
        provider_name = provider or cfg.agent.provider
        if provider_name not in cfg.providers:
            raise ConfigError(f"unknown provider: {provider_name}")
        provider_cfg = cfg.providers[provider_name]
        model_id = _resolve_model(cfg, provider_name, model=model)
        kernel_app = await boot(cfg)
        try:
            llm_service = kernel_app.require(LlmPlugin)
            sessions_service = kernel_app.require(SessionsPlugin)
            sandbox_service = kernel_app.require(SandboxPlugin)
            tools_service = kernel_app.require(ToolsPlugin)
            agents_service = kernel_app.require(AgentsPlugin)
            if native_mode:
                prompt_service = kernel_app.require(PromptPlugin)
                llm_provider = llm_service.native_provider(provider_name)
            else:
                prompt_service = None
                llm_provider = llm_service.provider(provider_name)
            registry = tools_service.registry_for(workspace)
            permission = sandbox_service.gate(workspace, ask=ask)
        except BaseException:
            await await_maybe(kernel_app.stop())
            raise
        activity = kernel_app.get(ActivityService)
        jobs = kernel_app.get(JobsService)
        team = kernel_app.get(TeamService)
        subagent = kernel_app.get(SubagentService)
        working_directory = kernel_app.get(WorkingDirectoryService)
        user_questions = kernel_app.get(UserQuestionsService)
        projections = kernel_app.get(ProjectionsService)
        schedule = kernel_app.get(SchedulePlugin)
        native_extra: dict[str, Any] = {}
        if native_mode:
            native_extra = {
                "native": True,
                "sessions": sessions_service,
                "prompt": prompt_service,
                "trace_export": kernel_app.get(TraceExportSink),
                "compactor": _native_compactor(kernel_app, llm_provider, model_id),
                "hooks": TuiAgentHooks(sink),
            }
        runtime = cls(
            cfg=cfg,
            cwd=workspace,
            kernel_app=kernel_app,
            provider_name=provider_name,
            model=model_id,
            context_window=_context_window(provider_cfg, model_id),
            store=None if native_mode else sessions_service.store,
            provider=llm_provider,
            registry=registry,
            permission=permission,
            agents=agents_service,
            sink=sink,
            ask=ask,
            activity=activity,
            jobs=jobs,
            team=team,
            subagent=subagent,
            working_directory=working_directory,
            user_questions=user_questions,
            projections=None if native_mode else projections,
            schedule=schedule,
            llm=llm_service,
            **native_extra,
        )
        await runtime.ensure_presets()
        runtime._configure_team(None)
        runtime._configure_subagent(None)
        if native_mode and resume:
            await runtime.open_session(resume)
        else:
            await runtime.new_session()
        return runtime

    async def start_schedule(self) -> None:
        """Attach the TUI schedule host and start the timer (best effort)."""
        service = self.schedule
        if service is None:
            return
        configure_host = getattr(service, "configure_host", None)
        if callable(configure_host):
            # an optional service must not break the UI
            with contextlib.suppress(Exception):
                configure_host(_TuiScheduleHost(self))
        start = getattr(service, "start", None)
        if callable(start):
            # an optional service must not break the UI
            with contextlib.suppress(Exception):
                await await_maybe(start())

    @property
    def presets(self) -> list[Any]:
        """Agent presets offered by the bundle, ordered by the service."""
        service = self._service(PresetsPlugin)
        if service is None:
            return []
        try:
            visible = getattr(service, "visible", None)
            specs = list(visible()) if callable(visible) else list(service.all())
        except Exception:  # noqa: BLE001 - the picker must keep working
            return []
        return [spec for spec in specs if not getattr(spec, "hidden", False)]

    async def set_team_mode(self, enabled: bool) -> str:
        """Enter or leave explicit team mode on the current session.

        Returns the preset now active. Entering remembers the preset that was
        active before team mode so /team off can restore it; leaving refuses
        while any team member is still live.
        """
        if self.native:
            raise DeeppyroError(
                "team mode is not ported to the native TUI runtime yet; "
                "/team is disabled while DEEPPYRO_TUI_NATIVE=1"
            )
        service = self._service(PresetsPlugin)
        if service is None:
            raise DeeppyroError("agent presets are not available in this bundle")
        if enabled:
            if self.agent_mode == AGENT_MODE_TEAM:
                return TEAM_PRESET_ID
            if self.busy:
                raise DeeppyroError("cannot enter team mode while a turn is running")
            previous = self.agent_preset
            if previous == TEAM_PRESET_ID or self.agent_mode == AGENT_MODE_TEAM:
                previous = DEFAULT_PRESET_ID
            self._resolve_preset(service, TEAM_PRESET_ID)
            await self._switch_preset(TEAM_PRESET_ID, None)
            self.team_previous_preset = previous
            self._record_preset()
            return TEAM_PRESET_ID
        if self.agent_mode != AGENT_MODE_TEAM:
            raise DeeppyroError("agent team mode is not active")
        if self.busy:
            raise DeeppyroError("cannot leave team mode while a turn is running")
        live = self._live_team_members()
        if live:
            labels = ", ".join(
                f"{getattr(member, 'name', '') or getattr(member, 'id', 'member')} "
                f"({getattr(member, 'phase', 'unknown')})"
                for member in live
            )
            raise DeeppyroError(f"cannot leave team mode while {len(live)} member(s) are active: {labels}")
        previous = self.team_previous_preset or DEFAULT_PRESET_ID
        try:
            self._resolve_preset(service, previous)
        except DeeppyroError:
            previous = DEFAULT_PRESET_ID
        await self._switch_preset(previous, None)
        self.team_previous_preset = DEFAULT_PRESET_ID
        self._record_preset()
        return previous

    def _live_team_members(self) -> list[Any]:
        """Team members that are still spawning, running or idle."""
        service = self.team
        if service is None:
            service = self._service(TeamService)
        getter = getattr(service, "members", None)
        if not callable(getter):
            return []
        try:
            members = list(getter())
        except Exception:  # noqa: BLE001 - the switch guard must not crash on a store error
            return []
        return [member for member in members if str(getattr(member, "phase", "")) in LIVE_PHASES]

    async def refresh_models(self, providers: list[str] | None = None, *, refresh: bool = False) -> int:
        """Refresh the remembered catalog for the given providers (best effort).

        The synchronous snapshot is merged first, then the provider listing is
        refreshed. Discovery errors are recorded on last_catalog_error and never
        propagate, so the configured catalog stays usable offline.
        """
        llm = self._llm_service()
        if llm is None:
            return 0
        names = list(providers) if providers is not None else self.providers()
        stored = 0
        error: str | None = None
        for name in names:
            snapshot = getattr(llm, "catalog_snapshot", None)
            if callable(snapshot):
                try:
                    stored += self._remember_catalog(name, list(snapshot(name) or []))
                except Exception as exc:  # noqa: BLE001 - fall back to the listing
                    error = str(exc) or exc.__class__.__name__
            lister = getattr(llm, "list_models", None)
            if not callable(lister):
                continue
            try:
                entries = await await_maybe(lister(name, refresh=refresh))
            except Exception as exc:  # noqa: BLE001 - catalog discovery is optional
                error = str(exc) or exc.__class__.__name__
                continue
            stored += self._remember_catalog(name, entries)
            error = None
        self.last_catalog_error = error if error is not None else self._catalog_error(llm)
        return stored

    async def set_provider(
        self,
        name: str,
        *,
        model: str | None = None,
        persist: bool = True,
    ) -> tuple[str, str]:
        """Switch the active LLM provider (and optionally its model)."""
        provider_name = (name or "").strip()
        if not provider_name:
            raise ConfigError("provider name is required")
        self._provider_config(provider_name)
        model_id = (model or "").strip()
        if not model_id:
            model_id = _resolve_model(self.cfg, provider_name, model=None)
        info = await self._discovered_model(provider_name, model_id)
        if provider_name == self.provider_name and model_id == self.model:
            return self.provider_name, self.model
        await self._apply_selection(provider_name, model_id, persist=persist, info=info)
        return self.provider_name, self.model

    async def set_model(self, model_id: str, *, persist: bool = True) -> str:
        """Switch the model on the active provider and rebuild the agent."""
        model_id = (model_id or "").strip()
        if not model_id:
            raise ConfigError("model id is required")
        info = await self._discovered_model(self.provider_name, model_id)
        if model_id == self.model:
            if persist:
                self._persist_selection()
            return self.model
        await self._apply_selection(self.provider_name, model_id, persist=persist, info=info)
        return self.model

    async def _apply_selection(
        self,
        provider_name: str,
        model_id: str,
        *,
        persist: bool,
        info: Any = None,
    ) -> None:
        self._activate_provider(provider_name, model_id, info=info)
        if self.session is not None:
            await self._attach(self.session)
        self._record_model_selection()
        if persist:
            self._persist_selection()

    def _persist_selection(self) -> None:
        """Best-effort write of the [agent] selection; failures stay visible."""
        try:
            save_agent_selection(self.provider_name, self.model)
        except Exception as exc:  # noqa: BLE001 - the switch itself still succeeded
            self.last_persist_error = str(exc)
        else:
            self.last_persist_error = None

    def permission_names(self) -> tuple[str, ...]:
        service = self._service(PermissionPresetsService)
        names = getattr(service, "names", None)
        if callable(names):
            try:
                return tuple(str(name) for name in names())
            except Exception:  # noqa: BLE001 - an optional service must not break the UI
                logger.debug("permission preset names unavailable", exc_info=True)
        return ("read-only", "workspace-write", "danger-full-access")

    async def close(self) -> None:
        """Dispose the kernel app (reverse plugin order, best effort)."""
        if self._closed:
            return
        self._closed = True
        if self.native:
            await self._detach()
        await self._dispose_sessions()
        await self.stop_schedule()
        self._save_projections()
        jobs = self.jobs
        cancel_all = getattr(jobs, "cancel_all", None)
        if cancel_all is not None:
            # shutdown must not mask an exception
            with contextlib.suppress(Exception):
                await await_maybe(cancel_all())
        subagent = self.subagent
        close_subagent = getattr(subagent, "close", None)
        if close_subagent is not None:
            # shutdown must not mask an exception
            with contextlib.suppress(Exception):
                await await_maybe(close_subagent())
        team = self.team
        close_team = getattr(team, "close", None)
        if close_team is not None:
            # shutdown must not mask an exception
            with contextlib.suppress(Exception):
                await await_maybe(close_team())
        kernel_app, self.kernel_app = self.kernel_app, None
        if kernel_app is not None:
            # shutdown must never mask an exception
            with contextlib.suppress(Exception):
                await await_maybe(kernel_app.stop())

    async def resume_session(self, session_id: str) -> Any:
        """Reopen an existing session, restoring its conversation and agent."""
        return await self.open_session(session_id)

    async def name_session_title(self) -> str | None:
        """Name the native session from its first exchange (best effort)."""
        if not self.native or self.session is None:
            return None
        kernel_app = self.kernel_app
        native_session = getattr(self.session, "native_session", None)
        if kernel_app is None or native_session is None:
            return None
        mount = getattr(kernel_app, "mount_record", None)
        record = mount("session-title") if callable(mount) else None
        plugin = getattr(record, "instance", None)
        options = getattr(plugin, "options", None)
        if options is not None and not bool(getattr(options, "enabled", True)):
            return None
        namer = getattr(plugin, "name_native_session", None)
        if not callable(namer):
            return None
        try:
            return await await_maybe(
                namer(native_session, provider_name=self.provider_name, model=self.model)
            )
        except Exception:  # noqa: BLE001 - titling is a nicety, never fatal
            logger.warning("native session title failed", exc_info=True)
            return None


__all__ = ["DEFAULT_CONTEXT_WINDOW", "TuiRuntime", "await_maybe", "resolve_workspace"]
