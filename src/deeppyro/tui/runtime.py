"""Runtime assembly for the terminal UI.

The TUI boots the cordis kernel (the same bundle the one-shot headless path
uses) and resolves the services; everything workspace-scoped is built here once
and then reused for the whole UI session:

* one SessionStore for creating/opening sessions,
* one ToolRegistry bound to the workspace cwd,
* one PermissionGate whose approval callback is served by the UI modal,
* one LlmProvider,
* one AgentSession per active session.

This module is the TUI's only seam to the domain layers, mirroring
deeppyro.headless.boot_runtime.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deeppyro.api import (
    AGENT_MODE_DEFAULT,
    AGENT_MODE_TEAM,
    DEFAULT_PRESET_ID,
    TEAM_PRESET_ID,
    PluginDependencyError,
)
from deeppyro.config import Config, load_config, save_agent_selection
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.schedule import DeliveryReceipt, NewOnceTarget, NewTarget, SessionTarget
from deeppyro.schedule.timeutil import format_instant
from deeppyro.team.types import LIVE_PHASES

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 32768


def resolve_workspace(path: str | Path | None = None) -> Path:
    """Resolve the workspace directory, rejecting missing paths."""
    workspace = Path(path).expanduser() if path else Path.cwd()
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ConfigError(f"workspace directory does not exist: {workspace}")
    return workspace


def _context_window(provider_cfg: Any, model_id: str) -> int:
    """Pick the model context window, falling back to the provider default."""
    models = list(getattr(provider_cfg, "models", []) or [])
    for entry in models:
        if getattr(entry, "id", None) == model_id and getattr(entry, "contextWindow", 0):
            return int(entry.contextWindow)
    if models and getattr(models[0], "contextWindow", 0):
        return int(models[0].contextWindow)
    return DEFAULT_CONTEXT_WINDOW


def _context_window_for(provider_cfg: Any, model_id: str, info: Any = None) -> int:
    """Context window for a model, honoring a catalog entry for discovered ids.

    Configured models keep the configuration answer exactly as before; only a
    model that config does not declare falls back to the catalog window and
    finally to DEFAULT_CONTEXT_WINDOW (no borrowed provider default, matching
    the headless resolver).
    """
    configured = [str(getattr(entry, "id", "")) for entry in list(getattr(provider_cfg, "models", []) or [])]
    if model_id in configured:
        return _context_window(provider_cfg, model_id)
    window = getattr(info, "context_window", None) if info is not None else None
    if window is not None and int(window) > 0:
        return int(window)
    return DEFAULT_CONTEXT_WINDOW


async def await_maybe(value: Any) -> Any:
    """Await an awaitable or return a plain value (test doubles)."""
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class TuiRuntime:
    """Booted kernel plus the live session/agent pair for the TUI."""

    cfg: Config
    cwd: Path
    kernel_app: Any
    provider_name: str
    model: str
    context_window: int
    store: Any
    provider: Any
    registry: Any
    permission: Any
    agents: Any
    sink: Any
    ask: Callable[[Any], Awaitable[bool]]
    activity: Any = None
    jobs: Any = None
    team: Any = None
    subagent: Any = None
    working_directory: Any = None
    user_questions: Any = None
    projections: Any = None
    schedule: Any = None
    schedule_notice: Callable[[str], None] | None = None
    llm: Any = None
    agent_preset: str = DEFAULT_PRESET_ID
    preset_revision: int | None = None
    team_previous_preset: str = DEFAULT_PRESET_ID
    session: Any = None
    agent: Any = None
    last_persist_error: str | None = None
    last_catalog_error: str | None = None
    _catalog: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _schedule_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    _schedule_once: dict[str, str] = field(default_factory=dict, repr=False)
    _closed: bool = field(default=False, repr=False)

    # -- construction ---------------------------------------------------
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
    ) -> TuiRuntime:
        """Boot the kernel bundle and resolve the TUI runtime."""
        from deeppyro.kernel import boot

        cfg = config if config is not None else load_config()
        workspace = resolve_workspace(cwd)
        provider_name = provider or cfg.agent.provider
        if provider_name not in cfg.providers:
            raise ConfigError(f"unknown provider: {provider_name}")
        provider_cfg = cfg.providers[provider_name]
        model_id = _resolve_model(cfg, provider_name, model=model)
        kernel_app = await boot(cfg)
        try:
            services = {
                name: kernel_app.service(name) for name in ("llm", "sessions", "sandbox", "tools", "agents")
            }
            llm_provider = services["llm"].provider(provider_name)
            registry = services["tools"].registry_for(workspace)
            permission = services["sandbox"].gate(workspace, ask=ask)
        except BaseException:
            await await_maybe(kernel_app.stop())
            raise
        activity = None
        try:
            activity = kernel_app.service("activity")
        except LookupError:
            pass
        jobs = None
        try:
            jobs = kernel_app.service("jobs")
        except LookupError:
            pass
        team = None
        try:
            team = kernel_app.service("team")
        except LookupError:
            pass
        subagent = None
        try:
            subagent = kernel_app.service("subagent")
        except LookupError:
            pass
        working_directory = None
        try:
            working_directory = kernel_app.service("working_directory")
        except LookupError:
            pass
        user_questions = None
        try:
            user_questions = kernel_app.service("user_questions")
        except LookupError:
            pass
        projections = None
        try:
            projections = kernel_app.service("projections")
        except LookupError:
            pass
        schedule = None
        try:
            schedule = kernel_app.service("schedule")
        except LookupError:
            pass
        runtime = cls(
            cfg=cfg,
            cwd=workspace,
            kernel_app=kernel_app,
            provider_name=provider_name,
            model=model_id,
            context_window=_context_window(provider_cfg, model_id),
            store=services["sessions"].store,
            provider=llm_provider,
            registry=registry,
            permission=permission,
            agents=services["agents"],
            sink=sink,
            ask=ask,
            activity=activity,
            jobs=jobs,
            team=team,
            subagent=subagent,
            working_directory=working_directory,
            user_questions=user_questions,
            projections=projections,
            schedule=schedule,
            llm=services["llm"],
        )
        await runtime.ensure_presets()
        runtime._configure_team(None)
        runtime._configure_subagent(None)
        await runtime.new_session()
        return runtime

    def configure_user_questions(self, handler: Any | None = None) -> None:
        """Route ask_user_question requests to a handler.

        No-op when the bundle did not load the user_questions service or when
        the service object has no configure hook, so older bundles keep working.
        """
        service = self.user_questions
        if service is None:
            return
        configure = getattr(service, "configure", None)
        if callable(configure):
            configure(handler)

    def configure_schedule_notice(self, handler: Callable[[str], None] | None = None) -> None:
        """Route scheduled-message notices to the UI."""
        self.schedule_notice = handler

    async def start_schedule(self) -> None:
        """Attach the TUI schedule host and start the timer (best effort)."""
        service = self.schedule
        if service is None:
            return
        configure_host = getattr(service, "configure_host", None)
        if callable(configure_host):
            try:
                configure_host(_TuiScheduleHost(self))
            except Exception:  # noqa: BLE001, S110 - an optional service must not break the UI
                pass
        start = getattr(service, "start", None)
        if callable(start):
            try:
                await await_maybe(start())
            except Exception:  # noqa: BLE001, S110 - an optional service must not break the UI
                pass

    async def stop_schedule(self) -> None:
        """Stop the schedule timer and any background agent it spawned."""
        service = self.schedule
        if service is not None:
            stop = getattr(service, "stop", None)
            if callable(stop):
                try:
                    await await_maybe(stop())
                except Exception:  # noqa: BLE001, S110 - shutdown must not mask an exception
                    pass
        tasks = list(self._schedule_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:  # noqa: BLE001, S110 - shutdown must not mask an exception
                pass
        self._schedule_tasks.clear()

    async def open_schedule_session(self, session_id: str) -> Any:
        """Open a persisted session for a schedule delivery."""
        return self.store.open(session_id)

    async def create_schedule_session(self, spec: Any, preset: str) -> Any:
        """Create a fresh session for a schedule delivery."""
        directory = str(getattr(spec, "cwd", "") or "")
        cwd = self.cwd
        if directory:
            candidate = Path(directory).expanduser()
            try:
                candidate = candidate.resolve()
            except OSError:
                candidate = self.cwd
            if candidate.is_dir():
                cwd = candidate
        return self.store.create(str(cwd), agent_preset=preset)

    async def create_schedule_agent(self, session: Any, *, preset: str) -> Any:
        """Build a silent agent for a scheduled turn."""
        await self._ensure_preset(preset, None)
        options = self.agents.options(model=self.model, context_window=self.context_window)
        return self.agents.create(
            session=session,
            provider=self.provider,
            registry=self._registry_for_preset(preset),
            options=options,
            sink=_ScheduleSink(),
            permission=self.permission,
            cwd=str(self.cwd),
            services=self._plugin_services(),
            preset=preset,
        )

    def track_schedule_agent(self, session: Any, agent: Any) -> None:
        """Close a background agent once its scheduled turn is idle."""
        task = asyncio.create_task(self._finish_schedule_agent(session, agent))
        self._schedule_tasks.add(task)
        task.add_done_callback(self._schedule_tasks.discard)

    async def _finish_schedule_agent(self, session: Any, agent: Any) -> None:
        try:
            wait = getattr(session, "wait_for_idle", None)
            if callable(wait):
                await await_maybe(wait())
        except Exception:  # noqa: BLE001, S110 - background cleanup is best effort
            pass
        finally:
            close = getattr(agent, "aclose", None)
            if callable(close):
                try:
                    await await_maybe(close())
                except Exception:  # noqa: BLE001, S110 - background cleanup is best effort
                    pass

    def _registry_for_preset(self, preset: str) -> Any:
        """Tool registry narrowed to one agent preset."""
        service = self._service("tools")
        registry_for = getattr(service, "registry_for", None)
        if callable(registry_for):
            try:
                return registry_for(Path(self.cwd), scope=preset)
            except TypeError:
                try:
                    return registry_for(Path(self.cwd))
                except Exception:  # noqa: BLE001 - fall back to the base registry
                    return self.registry
            except Exception:  # noqa: BLE001 - fall back to the base registry
                return self.registry
        return self.registry

    # -- sessions -------------------------------------------------------
    async def new_session(self) -> Any:
        """Create a fresh durable session and rebuild the agent."""
        self._save_projections()
        self.preset_revision = None
        session = self.store.create(str(self.cwd), agent_preset=self.agent_preset)
        await self._attach(session)
        self._record_model_selection()
        self._record_preset()
        return session

    async def open_session(self, session_id: str) -> Any:
        """Open an existing session and rebuild the agent around it."""
        self._save_projections()
        session = self.store.open(session_id)
        selection = _recorded_model_selection(session)
        if selection is not None:
            provider_name, model_id = selection
            if provider_name in self.cfg.providers:
                try:
                    self._activate_provider(
                        provider_name,
                        model_id,
                        info=self._cached_model(provider_name, model_id),
                    )
                except Exception:  # noqa: BLE001, S110 - resume must not brick the UI
                    pass
        header_preset = str(getattr(session.header, "agentPreset", "") or "").strip() or DEFAULT_PRESET_ID
        recorded = _recorded_preset(session)
        preset = recorded[0] if recorded is not None and recorded[0] else header_preset
        revision = recorded[1] if recorded is not None else None
        previous = recorded[2] if recorded is not None else None
        self.agent_preset = preset or header_preset
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

    def _save_projections(self) -> None:
        """Write the live session's projection sidecar and stop observing it."""
        service = self.projections
        session = self.session
        if service is None or session is None:
            return
        save = getattr(service, "save", None)
        detach = getattr(service, "detach", None)
        try:
            if save is not None:
                save(session)
        except Exception:  # noqa: BLE001, S110 - an optional service must not break a session
            pass
        try:
            if detach is not None:
                detach(session)
        except Exception:  # noqa: BLE001, S110 - an optional service must not break a session
            pass

    def _restore_projections(self, session: Any) -> None:
        """Restore the session's projection sidecar and start observing appends."""
        service = self.projections
        if service is None or session is None:
            return
        attach = getattr(service, "attach", None)
        if attach is None:
            return
        try:
            attach(session)
        except Exception:  # noqa: BLE001, S110 - an optional service must not break a session
            pass

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
        try:
            bind(
                session,
                cwd=session_cwd,
                record=session.append,
                inject_notice=lambda text: session.inject(text, source={"kind": "working-directory"}),
            )
        except Exception:  # noqa: BLE001, S110 - an optional service must not break a session
            pass

    def _configure_team(self, session: Any) -> None:
        """Keep the team service pointed at the live lead session context."""
        service = self.team
        if service is None:
            return
        configure = getattr(service, "configure", None)
        try:
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
        except Exception:  # noqa: BLE001, S110 - an optional service must not break a session
            pass

    def _configure_subagent(self, session: Any) -> None:
        """Keep the subagent service pointed at the live lead session context."""
        service = self.subagent
        if service is None:
            return
        configure = getattr(service, "configure", None)
        try:
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
        except Exception:  # noqa: BLE001, S110 - an optional service must not break a session
            pass

    @property
    def agent_mode(self) -> str:
        """Mode of the active agent preset ("default" or "team")."""
        service = self._service("presets")
        resolver = getattr(service, "resolve", None)
        spec: Any = None
        if callable(resolver):
            try:
                spec = resolver(self.agent_preset, revision=self.preset_revision)
            except Exception:  # noqa: BLE001 - status display must never fail
                spec = None
        return str(getattr(spec, "mode", "default") or "default")

    def _plugin_services(self) -> dict[str, Any]:
        """Plugin services the agent tools read from ToolContext.services."""
        services: dict[str, Any] = {}
        for name in (
            "llm",
            "hooks",
            "retryPolicy",
            "plugin_manager",
            "inspect",
            "settings",
            "jobs",
            "team",
            "subagent",
            "working_directory",
            "user_questions",
            "attachments",
            "schedule",
            "memory",
            "ptc",
        ):
            service = self._service(name)
            if service is not None:
                services[name] = service
        return services

    def _service(self, name: str) -> Any | None:
        app = self.kernel_app
        if app is None:
            return None
        try:
            return app.service(name)
        except LookupError:
            return None

    def _scoped_registry(self, workspace: Any) -> Any:
        """Tools registry narrowed to contributions visible to the preset."""
        service = self._service("tools")
        if service is None:
            return self.registry
        registry_for = getattr(service, "registry_for", None)
        if registry_for is None:
            return self.registry
        try:
            return registry_for(Path(workspace), scope=self.agent_preset)
        except TypeError:
            return registry_for(Path(workspace))

    # -- services -------------------------------------------------------
    @property
    def presets(self) -> list[Any]:
        """Agent presets offered by the bundle, ordered by the service."""
        service = self._service("presets")
        if service is None:
            return []
        try:
            visible = getattr(service, "visible", None)
            if callable(visible):
                specs = list(visible())
            else:
                specs = list(service.all())
        except Exception:  # noqa: BLE001 - the picker must keep working
            return []
        return [spec for spec in specs if not getattr(spec, "hidden", False)]

    async def set_agent_preset(self, preset: str) -> str:
        """Switch the preset used by subsequent agents on this runtime.

        Hidden presets are reserved for their dedicated command: team mode is
        entered with /team on, so /preset team is rejected with a hint instead
        of silently switching.
        """
        service = self._service("presets")
        if service is None:
            raise DeeppyroError("agent presets are not available in this bundle")
        wanted = (preset or "").strip()
        spec = self._resolve_preset(service, wanted)
        mode = str(getattr(spec, "mode", AGENT_MODE_DEFAULT) or AGENT_MODE_DEFAULT)
        if mode == AGENT_MODE_TEAM or spec.id == TEAM_PRESET_ID or getattr(spec, "hidden", False):
            raise DeeppyroError("team mode is enabled with /team on, not /preset")
        if self.busy:
            raise DeeppyroError("cannot switch agent preset while a turn is running")
        if spec.id == self.agent_preset and self.preset_revision is None:
            return spec.id
        await self._switch_preset(spec.id, None)
        self._record_preset()
        return spec.id

    async def set_team_mode(self, enabled: bool) -> str:
        """Enter or leave explicit team mode on the current session.

        Returns the preset now active. Entering remembers the preset that was
        active before team mode so /team off can restore it; leaving refuses
        while any team member is still live.
        """
        service = self._service("presets")
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

    def _resolve_preset(self, service: Any, preset: str) -> Any:
        """Resolve a preset id, translating a service error for the UI."""
        try:
            return service.resolve(preset or None)
        except Exception as exc:  # surface the service message
            raise DeeppyroError(str(exc)) from exc

    async def _switch_preset(self, preset_id: str, revision: int | None) -> None:
        """Switch preset/agent atomically; restore the old pair on failure."""
        service = self._service("presets")
        if service is None:
            raise DeeppyroError("agent presets are not available in this bundle")
        spec = self._resolve_preset(service, preset_id)
        old_preset = self.agent_preset
        old_revision = self.preset_revision
        old_agent = self.agent
        await self._ensure_preset(spec.id, revision)
        self.agent_preset = spec.id
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
                except Exception:
                    logger.exception("failed to restore the agent after a preset switch failure")
            raise

    def _live_team_members(self) -> list[Any]:
        """Team members that are still spawning, running or idle."""
        service = self.team
        if service is None:
            service = self._service("team")
        getter = getattr(service, "members", None)
        if not callable(getter):
            return []
        try:
            members = list(getter())
        except Exception:  # noqa: BLE001 - the switch guard must not crash on a store error
            return []
        return [member for member in members if str(getattr(member, "phase", "")) in LIVE_PHASES]

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

    # -- provider / model switching -------------------------------------
    def providers(self) -> list[str]:
        """Configured provider names, in config order."""
        return [str(name) for name in self.cfg.providers]

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

    # -- model catalog ----------------------------------------------------
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

    def _cached_model(self, provider: str, model_id: str) -> Any | None:
        """One remembered catalog entry, or None."""
        return self._catalog.get(provider, {}).get(model_id)

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

    def _provider_config(self, name: str) -> Any:
        provider_cfg = self.cfg.providers.get(name)
        if provider_cfg is None:
            choices = ", ".join(self.providers()) or "(none configured)"
            raise ConfigError(f"unknown provider: {name} (choose from {choices})")
        return provider_cfg

    def _validate_model(self, provider_name: str, model_id: str) -> None:
        """Reject models the config does not know and the catalog has not seen."""
        available = self.models(provider_name)
        if not available or model_id in available:
            return
        if self._cached_model(provider_name, model_id) is not None:
            return
        raise ConfigError(
            f"unknown model: {model_id} (provider {provider_name} offers {', '.join(available)})"
        )

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

    # -- policy / workspace switching -----------------------------------
    async def set_preset(self, preset: str) -> None:
        """Switch the sandbox preset used by subsequent tool calls."""
        setter = getattr(self.permission, "set_preset", None)
        if setter is None:
            raise DeeppyroError("permission gate does not support preset switching")
        setter(preset)
        self._record("permission/preset", {"preset": preset})

    async def set_approval(self, approval: str) -> None:
        """Switch the approval policy used by subsequent gated actions."""
        setter = getattr(self.permission, "set_approval", None)
        if setter is None:
            raise DeeppyroError("permission gate does not support approval switching")
        setter(approval)
        self._record("approval/policy", {"policy": approval})

    async def set_workspace(self, path: str | Path) -> Path:
        """Rebind tools and approvals to a new workspace, keeping the session."""
        workspace = resolve_workspace(path)
        if workspace == self.cwd:
            return workspace
        kernel_app = self.kernel_app
        if kernel_app is None:
            raise DeeppyroError("cannot switch workspace after the kernel has stopped")
        current = self.permission
        gate = kernel_app.service("sandbox").gate(workspace, ask=self.ask)
        gate.set_preset(str(getattr(current, "preset", "workspace-write")))
        gate.set_approval(str(getattr(current, "approval", "ask")))
        registry = kernel_app.service("tools").registry_for(workspace)
        self.permission = gate
        self.registry = registry
        self.cwd = workspace
        self._record("working-directory/change", {"cwd": str(workspace)})
        if self.session is not None:
            await self._attach(self.session)
        return workspace

    def known_workspaces(self, limit: int = 12) -> list[str]:
        """Workspace paths remembered by previous sessions, newest first."""
        paths: list[str] = []
        try:
            infos = self.store.list()
        except Exception:  # noqa: BLE001 - the dropdown must keep working
            infos = []
        for info in infos:
            cwd = str(getattr(info, "cwd", "") or "")
            if cwd and cwd not in paths:
                paths.append(cwd)
            if len(paths) >= max(limit, 1):
                break
        return paths

    async def ensure_presets(self) -> None:
        """Materialize every preset realm before any agent is created."""
        service = self._service("presets")
        ensure_all = getattr(service, "ensure_all", None)
        if callable(ensure_all):
            await await_maybe(ensure_all())

    async def _ensure_preset(self, preset_id: str, revision: int | None) -> None:
        """Materialize one preset revision (no-op without a presets service)."""
        service = self._service("presets")
        ensure = getattr(service, "ensure", None)
        if callable(ensure):
            await await_maybe(ensure(preset_id, revision=revision))

    def _record_preset(self) -> None:
        """Persist the pinned preset revision so a resume reproduces it."""
        mode = self.agent_mode
        data: dict[str, Any] = {"preset": self.agent_preset, "mode": mode}
        if self.preset_revision is not None:
            data["revision"] = self.preset_revision
        if mode == AGENT_MODE_TEAM:
            data["previous"] = self.team_previous_preset
        self._record("agent-preset/selected", data)

    def _record_model_selection(self) -> None:
        """Persist the active provider/model so a resume reproduces it."""
        self._record("model/selection", {"provider": self.provider_name, "model": self.model})

    def _record(self, record_type: str, data: dict[str, Any]) -> None:
        """Append one non-surface event to the active session (best effort)."""
        append = getattr(self.session, "append", None)
        if callable(append):
            append(record_type, data)

    @property
    def session_id(self) -> str:
        return str(getattr(self.session, "session_id", "") or "")

    @property
    def busy(self) -> bool:
        return bool(getattr(self.agent, "busy", False))

    def session_usage(self) -> dict[str, int]:
        """Cumulative token usage of the active session."""
        getter = getattr(self.session, "usage", None)
        if getter is None:
            return {"input": 0, "output": 0, "cached": 0}
        try:
            usage = getter()
        except Exception:  # noqa: BLE001 - usage display must never break the UI
            return {"input": 0, "output": 0, "cached": 0}
        return {key: int(usage.get(key, 0)) for key in ("input", "output", "cached")}

    def context_tokens(self) -> int:
        """Prompt tokens of the latest request, i.e. the live context size."""
        getter = getattr(self.session, "context_tokens", None)
        if getter is None:
            return self.session_usage()["input"]
        try:
            return int(getter())
        except Exception:  # noqa: BLE001 - context display must never break the UI
            return 0

    def recent_sessions(self, limit: int = 20) -> list[Any]:
        """Most recent sessions, newest first."""
        return list(self.store.list())[: max(limit, 0)]

    async def compact_now(self) -> bool:
        """Compact the active session immediately; True when it shrank."""
        session, agent = self.session, self.agent
        if session is None or agent is None:
            return False
        compactor = getattr(agent, "compactor", None)
        if compactor is None:
            raise DeeppyroError("compaction is not available for this agent")
        messages = session.derive_messages()
        return bool(await compactor.compact(session, messages))

    # -- shutdown -------------------------------------------------------
    async def close(self) -> None:
        """Dispose the kernel app (reverse plugin order, best effort)."""
        if self._closed:
            return
        self._closed = True
        await self.stop_schedule()
        self._save_projections()
        jobs = self.jobs
        cancel_all = getattr(jobs, "cancel_all", None)
        if cancel_all is not None:
            try:
                await await_maybe(cancel_all())
            except Exception:  # noqa: BLE001, S110 - shutdown must not mask an exception
                pass
        subagent = self.subagent
        close_subagent = getattr(subagent, "close", None)
        if close_subagent is not None:
            try:
                await await_maybe(close_subagent())
            except Exception:  # noqa: BLE001, S110 - shutdown must not mask an exception
                pass
        team = self.team
        close_team = getattr(team, "close", None)
        if close_team is not None:
            try:
                await await_maybe(close_team())
            except Exception:  # noqa: BLE001, S110 - shutdown must not mask an exception
                pass
        kernel_app, self.kernel_app = self.kernel_app, None
        if kernel_app is not None:
            try:
                await await_maybe(kernel_app.stop())
            except Exception:  # noqa: BLE001, S110 - shutdown must never mask an exception
                pass


class _ScheduleSink:
    """EventSink that discards events from background scheduled turns."""

    def begin_turn(self) -> None:
        return None

    def end_turn(self) -> None:
        return None

    def emit(self, event: dict[str, Any]) -> None:
        return None


def _now_rfc3339() -> str:
    return format_instant(datetime.now(UTC))


class _TuiScheduleHost:
    """ScheduleHost that delivers occurrences into TUI sessions."""

    def __init__(self, runtime: TuiRuntime) -> None:
        self._runtime = runtime

    async def session_exists(self, session_id: str) -> bool:
        runtime = self._runtime
        if not session_id:
            return False
        if session_id == runtime.session_id:
            return True
        try:
            runtime.store.open(session_id)
        except Exception:  # noqa: BLE001 - an unreadable session cannot receive messages
            return False
        return True

    async def delegation_depth(self, session_id: str) -> int:
        return 0

    async def deliver(self, task: Any, message: str) -> DeliveryReceipt | None:
        target = task.target
        source = {"kind": "schedule", "taskId": task.id, "occurrence": task.scheduled_at}
        if isinstance(target, SessionTarget):
            return await self._deliver_to_session(target.session_id, task, message, source)
        if isinstance(target, (NewOnceTarget, NewTarget)):
            return await self._deliver_to_new(target, task, message, source)
        raise DeeppyroError(f"unsupported schedule target: {target!r}")

    async def _deliver_to_session(
        self,
        session_id: str,
        task: Any,
        message: str,
        source: dict[str, Any],
    ) -> DeliveryReceipt:
        runtime = self._runtime
        if session_id == runtime.session_id and runtime.session is not None:
            notice = runtime.schedule_notice
            if notice is not None:
                try:
                    notice(message)
                except Exception:
                    logger.debug("schedule notice failed", exc_info=True)
            runtime.session.followup(message, source=source)
            return DeliveryReceipt(
                scheduled_at="",
                delivered_at=_now_rfc3339(),
                target_session_id=session_id,
            )
        session = await runtime.open_schedule_session(session_id)
        agent = await runtime.create_schedule_agent(session, preset=runtime.agent_preset)
        try:
            session.followup(message, source=source)
        except Exception:
            close = getattr(agent, "aclose", None)
            if callable(close):
                try:
                    await await_maybe(close())
                except Exception:  # noqa: BLE001, S110 - cleanup is best effort
                    pass
            raise
        runtime.track_schedule_agent(session, agent)
        return DeliveryReceipt(
            scheduled_at="",
            delivered_at=_now_rfc3339(),
            target_session_id=session_id,
        )

    async def _deliver_to_new(
        self,
        target: Any,
        task: Any,
        message: str,
        source: dict[str, Any],
    ) -> DeliveryReceipt:
        runtime = self._runtime
        spec = target.create
        preset = str(getattr(spec, "agent_preset", "") or runtime.agent_preset)
        session = None
        if isinstance(target, NewOnceTarget):
            session_id = str(target.session_id or runtime._schedule_once.get(task.id, ""))
            if session_id:
                try:
                    session = await runtime.open_schedule_session(session_id)
                except Exception:  # noqa: BLE001 - recreate a missing once-target session
                    session = None
        if session is None:
            session = await runtime.create_schedule_session(spec, preset)
            if isinstance(target, NewOnceTarget):
                runtime._schedule_once[task.id] = str(getattr(session, "session_id", "") or "")
        agent = await runtime.create_schedule_agent(session, preset=preset)
        try:
            session.followup(message, source=source)
        except Exception:
            close = getattr(agent, "aclose", None)
            if callable(close):
                try:
                    await await_maybe(close())
                except Exception:  # noqa: BLE001, S110 - cleanup is best effort
                    pass
            raise
        runtime.track_schedule_agent(session, agent)
        return DeliveryReceipt(
            scheduled_at="",
            delivered_at=_now_rfc3339(),
            target_session_id=str(getattr(session, "session_id", "") or ""),
        )


def _recorded_preset(session: Any) -> tuple[str, int | None, str | None] | None:
    """Last agent-preset/selected record: (preset, revision, previous).

    The header only carries the preset a session was created with, so the
    record is what makes a later switch (including /team on) survive resume.
    """
    records = getattr(session, "records", None) or []
    for record in reversed(list(records)):
        if getattr(record, "type", "") != "agent-preset/selected":
            continue
        data = getattr(record, "data", None)
        if not isinstance(data, dict):
            return None
        preset = data.get("preset")
        revision = data.get("revision")
        previous = data.get("previous")
        return (
            preset if isinstance(preset, str) else "",
            revision if isinstance(revision, int) else None,
            previous if isinstance(previous, str) and previous else None,
        )
    return None


def _recorded_model_selection(session: Any) -> tuple[str, str] | None:
    """The last model/selection written to the session, if still readable."""
    records = getattr(session, "records", None) or []
    for record in reversed(list(records)):
        if getattr(record, "type", "") != "model/selection":
            continue
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            provider = data.get("provider")
            model = data.get("model")
            if isinstance(provider, str) and isinstance(model, str) and provider and model:
                return provider, model
        return None
    return None


def _resolve_model(cfg: Config, provider_name: str, *, model: str | None) -> str:
    """Resolve the model from an override, the configured provider, or its default.

    The [agent] model belongs to the configured provider: when another provider
    is selected explicitly (--provider or /provider) its own default is used
    instead of leaking the configured model onto a different endpoint.
    """
    provider_cfg = cfg.providers[provider_name]
    available = [str(entry.id) for entry in list(provider_cfg.models or [])]
    if model:
        if available and model not in available:
            raise ConfigError(
                f"unknown model: {model} (provider {provider_name} offers {', '.join(available)})"
            )
        return model
    if provider_name == cfg.agent.provider and cfg.agent.model:
        return cfg.agent.model
    if available:
        return available[0]
    if provider_name == cfg.agent.provider:
        try:
            _, resolved = cfg.resolve()
        except KeyError as exc:
            raise ConfigError(f"unknown provider: {exc.args[0]}") from exc
        return resolved
    return ""


__all__ = ["DEFAULT_CONTEXT_WINDOW", "TuiRuntime", "await_maybe", "resolve_workspace"]
