"""Runtime assembly for the terminal UI. This module is the facade of the layered TuiRuntime: it keeps the construction entry point, provider/model commands, and shutdown, and re-exports the module-level helpers and the schedule host that were historically defined here. It is the TUI's only seam to the domain layers, mirroring deeppyro.headless.boot_runtime."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro.api import (
    AGENT_MODE_DEFAULT,
    AGENT_MODE_TEAM,
    AGENT_PRESENTATION_NATIVE,
    DEFAULT_PRESET_ID,
    TEAM_PRESET_ID,
    PluginDependencyError,
)
from deeppyro.config import Config, load_config, save_agent_selection
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.hooks import SESSION_DISPOSED
from deeppyro.team.types import LIVE_PHASES
from deeppyro.tui.runtime_schedule_host import _now_rfc3339 as _now_rfc3339
from deeppyro.tui.runtime_schedule_host import _ScheduleSink as _ScheduleSink
from deeppyro.tui.runtime_schedule_host import _TuiScheduleHost as _TuiScheduleHost
from deeppyro.tui.runtime_session import SessionLayer
from deeppyro.tui.runtime_support import DEFAULT_CONTEXT_WINDOW as DEFAULT_CONTEXT_WINDOW
from deeppyro.tui.runtime_support import _context_window as _context_window
from deeppyro.tui.runtime_support import _context_window_for as _context_window_for
from deeppyro.tui.runtime_support import _recorded_model_selection as _recorded_model_selection
from deeppyro.tui.runtime_support import _recorded_preset as _recorded_preset
from deeppyro.tui.runtime_support import _resolve_model as _resolve_model
from deeppyro.tui.runtime_support import await_maybe as await_maybe
from deeppyro.tui.runtime_support import logger as logger
from deeppyro.tui.runtime_support import resolve_workspace as resolve_workspace


@dataclass
class TuiRuntime(SessionLayer):
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
        with contextlib.suppress(LookupError):
            activity = kernel_app.service("activity")
        jobs = None
        with contextlib.suppress(LookupError):
            jobs = kernel_app.service("jobs")
        team = None
        with contextlib.suppress(LookupError):
            team = kernel_app.service("team")
        subagent = None
        with contextlib.suppress(LookupError):
            subagent = kernel_app.service("subagent")
        working_directory = None
        with contextlib.suppress(LookupError):
            working_directory = kernel_app.service("working_directory")
        user_questions = None
        with contextlib.suppress(LookupError):
            user_questions = kernel_app.service("user_questions")
        projections = None
        with contextlib.suppress(LookupError):
            projections = kernel_app.service("projections")
        schedule = None
        with contextlib.suppress(LookupError):
            schedule = kernel_app.service("schedule")
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

    @property
    def presets(self) -> list[Any]:
        """Agent presets offered by the bundle, ordered by the service."""
        service = self._service("presets")
        if service is None:
            return []
        try:
            visible = getattr(service, "visible", None)
            specs = list(visible()) if callable(visible) else list(service.all())
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

    def permission_preset(self) -> str:
        """The current named permission preset (or "custom")."""
        service = self._service("permissionPresets")
        current = getattr(service, "current", None)
        if callable(current) and self.session is not None:
            try:
                return str(current(self.session))
            except Exception:  # noqa: BLE001 - an optional service must not break the UI
                logger.debug("permission preset unavailable", exc_info=True)
        return str(getattr(self.permission, "preset", "workspace-write"))

    def permission_names(self) -> tuple[str, ...]:
        service = self._service("permissionPresets")
        names = getattr(service, "names", None)
        if callable(names):
            try:
                return tuple(str(name) for name in names())
            except Exception:  # noqa: BLE001 - an optional service must not break the UI
                logger.debug("permission preset names unavailable", exc_info=True)
        return ("read-only", "workspace-write", "danger-full-access")

    async def set_permission(self, preset: str) -> str:
        """Switch the named permission preset for the live session."""
        service = self._service("permissionPresets")
        apply = getattr(service, "apply", None)
        if not callable(apply) or self.session is None:
            raise DeeppyroError("permission presets are not available in this session")
        return str(apply(self.session, preset))

    async def set_workspace(self, path: str | Path) -> Path:
        """Rebind tools and approvals to a new workspace, keeping the session."""
        workspace = resolve_workspace(path)
        if workspace == self.cwd:
            return workspace
        kernel_app = self.kernel_app
        if kernel_app is None:
            raise DeeppyroError("cannot switch workspace after the kernel has stopped")
        gate = kernel_app.service("sandbox").gate(workspace, ask=self.ask, session=self.session)
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

    def recent_sessions(self, limit: int = 20, *, include_archived: bool = False) -> list[Any]:
        """Most recent sessions, newest first; archived ones are hidden by default."""
        return list(self.store.list(include_archived=include_archived))[: max(limit, 0)]

    def archived_sessions(self, limit: int = 20) -> list[Any]:
        """Archived sessions only, newest first."""
        sessions = [info for info in self.store.list(include_archived=True) if info.archived]
        return sessions[: max(limit, 0)]

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

    async def close(self) -> None:
        """Dispose the kernel app (reverse plugin order, best effort)."""
        if self._closed:
            return
        self._closed = True
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


__all__ = ["DEFAULT_CONTEXT_WINDOW", "TuiRuntime", "await_maybe", "resolve_workspace"]
