"""Kernel-backed web session host: one native runtime behind the browser.

This module is the service half of the web layer. It is not a second agent: it
replays native session facts, builds NativeAgentHost instances through
AgentsPlugin.host exactly like the TUI, and forwards the native hook stream to
the browser. Conversation state stays in the append-only native session log;
the in-memory journal is only an output tap. The process owns one attached
session, one active turn and a small bounded message queue, so a browser
reconnect resumes from facts and the journal instead of hidden server state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from deeppyro.agent.budget import BudgetExceeded
from deeppyro.agent.errors import ProviderStreamError
from deeppyro.agent.native_factory import AgentBuild, PromptFacts
from deeppyro.agent.native_host import NativeAgentHost, NativeSessionHandle
from deeppyro.agent.native_records import RECORD_KIND_MAP, UnsupportedRecordTypeError
from deeppyro.api import AGENT_MODE_TEAM, DEFAULT_PRESET_ID, PluginContext
from deeppyro.api.errors import PluginDependencyError
from deeppyro.api.services import service_name
from deeppyro.api.types import PresetSpec
from deeppyro.attachments.store import AttachmentStore
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.inspect import InspectPlugin
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.plugin_manager import PluginManagerPlugin
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.sandbox import SandboxPlugin
from deeppyro.builtin.schedule import SchedulePlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.builtin.settings import SettingsPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.builtin.usage import UsageService
from deeppyro.config import Config, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.core.messages import (
    MISSING_TOOL_RESULT_TEXT,
    ImagePart,
    ReasoningPart,
    TextPart,
)
from deeppyro.hooks.service import HookService
from deeppyro.jobs.manager import JobsService
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.llm.retry import RetryPolicy
from deeppyro.memory.service import MemoryService
from deeppyro.observability.protocols import TraceExportSink, UsageSink
from deeppyro.ptc.runtime import PtcRuntime
from deeppyro.sandbox.approval import ApprovalService
from deeppyro.sandbox.gate import PermissionGate
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.sandbox.policy import (
    SandboxPolicyService,
    approval_policy_text,
    sandbox_policy_text,
)
from deeppyro.sandbox.types import DEFAULT_PERMISSION_PRESET
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.fold import SessionState, SessionView, fold
from deeppyro.session.native_control import NewSession
from deeppyro.session.native_index import NativeSessionInfo
from deeppyro.session.native_query import (
    NATIVE_QUERY_NOT_FOUND,
    NativeQueryError,
    normalize_native_title,
)
from deeppyro.subagent.manager import SubagentService
from deeppyro.team.manager import TeamService
from deeppyro.user_questions.service import UserQuestionsService
from deeppyro.web.approvals import ApprovalBroker, ApprovalDecision, ApprovalPrompt
from deeppyro.web.events import (
    HEARTBEAT_FRAME,
    ApprovalEvent,
    ErrorEvent,
    FinalEvent,
    SessionEvent,
    StatusEvent,
    StatusPhase,
    UsageEvent,
    WebEvent,
    encode_sse_frame,
)
from deeppyro.web.hooks import WebAgentHooks
from deeppyro.web.journal import EventJournal
from deeppyro.web.wire import (
    BadRequest,
    Conflict,
    CreateSessionRequest,
    HttpError,
    NotFound,
    SendMessageRequest,
    ServiceUnavailable,
    TooManyRequests,
)
from deeppyro.working_directory.service import WorkingDirectoryService

logger = logging.getLogger("deeppyro.web.service")

#: Context window assumed when the model config does not declare one.
DEFAULT_CONTEXT_WINDOW = 32768

#: How many user messages may wait behind the active turn.
DEFAULT_PENDING_TURNS = 8

#: Default page size of the paginated session catalogue.
SESSION_LIST_LIMIT = 50

#: Largest page a client may request from the session catalogue.
MAX_SESSION_LIST_LIMIT = 200

#: Tools receive these services through ToolContext.services. The tuple mirrors
#: tui/runtime_schedule.py:42-62 (the TUI cannot be imported from web code) plus
#: the native observability sinks the NativeAgentHost taps into.
_TOOL_CONTEXT_SERVICES: tuple[type[Any], ...] = (
    LlmPlugin,
    HookService,
    RetryPolicy,
    SandboxPlugin,
    SandboxPolicyService,
    ApprovalService,
    PermissionPresetsService,
    PluginManagerPlugin,
    InspectPlugin,
    SettingsPlugin,
    JobsService,
    TeamService,
    SubagentService,
    WorkingDirectoryService,
    UserQuestionsService,
    AttachmentStore,
    SchedulePlugin,
    MemoryService,
    PtcRuntime,
    TraceExportSink,
    UsageSink,
)

#: Reverse of the legacy record map: native event kind -> v4 record type.
_RECORD_TYPES_BY_KIND: Mapping[Any, str] = {
    mapping.kind: record_type for record_type, mapping in RECORD_KIND_MAP.items()
}


@dataclass(frozen=True, slots=True)
class HostServices:
    """The kernel services one web host needs, resolved once at start."""

    config: Config
    sessions: SessionsPlugin
    agents: AgentsPlugin
    presets: PresetsPlugin
    llm: LlmPlugin
    sandbox: SandboxPlugin
    tools: ToolsPlugin
    permission_presets: PermissionPresetsService
    working_directory: WorkingDirectoryService | None
    tool_services: Mapping[str, object]
    usage: UsageService | None = None


def resolve_host_services(app: Any) -> HostServices:
    """Resolve HostServices from a booted kernel App."""
    return _build_host_services(config=app.config, get=app.get, require=app.require)


def resolve_plugin_services(ctx: PluginContext) -> HostServices:
    """Resolve HostServices from a mounted plugin context (the bundle seam)."""
    config = ctx.require(KernelConfig).value
    return _build_host_services(config=config, get=ctx.get, require=ctx.require)


def _build_host_services(
    *,
    config: Config,
    get: Callable[[type[Any]], Any],
    require: Callable[[type[Any]], Any],
) -> HostServices:
    tool_services: dict[str, object] = {}
    for contract in _TOOL_CONTEXT_SERVICES:
        value = get(contract)
        if value is None:
            continue
        tool_services[service_name(contract)] = value
    return HostServices(
        config=config,
        sessions=require(SessionsPlugin),
        agents=require(AgentsPlugin),
        presets=require(PresetsPlugin),
        llm=require(LlmPlugin),
        sandbox=require(SandboxPlugin),
        tools=require(ToolsPlugin),
        permission_presets=require(PermissionPresetsService),
        working_directory=get(WorkingDirectoryService),
        tool_services=tool_services,
        usage=get(UsageService),
    )


@dataclass(frozen=True, slots=True)
class Selection:
    """A validated provider/model choice plus the context window it implies."""

    provider: str
    model: str
    context_window: int


def resolve_workspace(path: str | Path) -> Path:
    """Resolve a workspace path that must be an existing directory."""
    workspace = Path(path).expanduser()
    with contextlib.suppress(OSError):
        workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ConfigError(f"workspace directory does not exist: {workspace}")
    return workspace


def context_window(provider: ProviderConfig, model_id: str) -> int:
    """Return the configured context window for a model, with a safe fallback."""
    matched = next((entry for entry in provider.models if entry.id == model_id), None)
    if matched is not None and matched.context_window:
        return int(matched.context_window)
    if provider.models and provider.models[0].context_window:
        return int(provider.models[0].context_window)
    return DEFAULT_CONTEXT_WINDOW


def resolve_selection(
    config: Config,
    provider: str | None = None,
    model: str | None = None,
) -> Selection:
    """Validate a provider/model pair from config plus explicit overrides."""
    provider_name = (provider or config.agent.provider or "").strip()
    if not provider_name:
        raise BadRequest("no provider is configured")
    provider_cfg = config.providers.get(provider_name)
    if provider_cfg is None:
        raise BadRequest(f"unknown provider: {provider_name}")
    model_id = (model or "").strip()
    if model_id:
        known = [entry.id for entry in provider_cfg.models]
        if known and model_id not in known:
            raise BadRequest(f"unknown model: {model_id}")
    elif config.agent.provider == provider_name and config.agent.model.strip():
        model_id = config.agent.model.strip()
    elif provider_cfg.models:
        model_id = provider_cfg.models[0].id
    else:
        raise BadRequest(f"no model configured for provider {provider_name}")
    return Selection(
        provider=provider_name,
        model=model_id,
        context_window=context_window(provider_cfg, model_id),
    )


class _NativeView:
    """Adapt a NativeSessionHandle to the record view the sandbox services read.

    PermissionPresetsService, SandboxPolicyService and ApprovalService inspect
    getattr(session, "records", ()) and call session.append(type, data) with the
    legacy v4 record vocabulary. The native handle speaks schema events, so this
    adapter rebuilds records from facts and drops v4-only audit records such as
    approval/asked that the native schema does not accept. It lives in the web
    package because the TUI's NativeRecordView must not be imported here.
    """

    __slots__ = ("_handle", "_records", "_records_seq")

    def __init__(self, handle: NativeSessionHandle) -> None:
        self._handle = handle
        self._records: list[SimpleNamespace] = []
        self._records_seq = -1

    @property
    def handle(self) -> NativeSessionHandle:
        """The wrapped native handle."""
        return self._handle

    @property
    def session_id(self) -> str:
        """Native session id."""
        return self._handle.session_id

    @property
    def header(self) -> Any:
        """The native session header view."""
        return self._handle.header

    @property
    def events(self) -> Any:
        """The native event log."""
        return self._handle.events

    @property
    def records(self) -> list[SimpleNamespace]:
        """Legacy-shaped records rebuilt from the native events."""
        events = self._handle.events
        last_seq = events[-1].seq if events else 0
        if last_seq != self._records_seq:
            self._records = [
                SimpleNamespace(type=_record_type(event), data=dict(event.data)) for event in events
            ]
            self._records_seq = last_seq
        return self._records

    def append(self, record_type: str, data: Mapping[str, Any] | None = None) -> int:
        """Append a legacy record, dropping v4-only types the schema rejects."""
        try:
            return self._handle.append(record_type, data)
        except UnsupportedRecordTypeError:
            logger.debug("web view dropped unsupported record type %s", record_type)
            return -1

    def inject(self, text: str, source: Mapping[str, Any] | None = None) -> bool:
        """Queue a notice for the next native turn."""
        return self._handle.inject(text, source)

    def fold(self) -> Any:
        """Fold the native facts into a SessionView."""
        return self._handle.fold()

    def derive_messages(self, *, strict: bool | None = None) -> Any:
        """Rebuild the transcript from the fold."""
        return self._handle.derive_messages(strict=strict)

    def usage(self) -> dict[str, int]:
        """Cumulative host usage counters."""
        return self._handle.usage()

    def context_tokens(self) -> int:
        """Prompt tokens of the newest model call."""
        return self._handle.context_tokens()

    def aclose(self) -> None:
        """Close the wrapped native session."""
        self._handle.aclose()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


def _record_type(event: Any) -> str:
    kind = getattr(event, "kind", None)
    if kind is None:
        return str(getattr(event, "type", ""))
    return _RECORD_TYPES_BY_KIND.get(kind, str(getattr(kind, "value", kind)))


@dataclass(slots=True)
class SessionBinding:
    """One attached native session with its live host and turn queue."""

    handle: NativeSessionHandle
    view: _NativeView
    host: NativeAgentHost
    hooks: WebAgentHooks
    gate: PermissionGate
    provider: str
    model: str
    preset: str
    preset_revision: int
    context_window: int
    workspace: Path
    pending: deque[str] = field(default_factory=deque)
    task: asyncio.Task[None] | None = None

    @property
    def session_id(self) -> str:
        """Native session id of this binding."""
        return self.handle.session_id


async def _deny_approval(_request: Any) -> bool:
    """Refuse approval requests until the web UI grows an approval surface."""
    logger.debug("web host denied an approval request: no approval surface yet")
    return False


def _pin_permission(view: _NativeView, presets: PermissionPresetsService) -> None:
    """Pin the default permission facts on a fresh session, like the TUI does."""
    spec = presets.get(presets.default)
    state = view.fold().state
    if state.sandbox_mode is None:
        view.append("sandbox/mode", {"mode": str(spec.sandbox)})
    if state.permission_policy is None:
        view.append("approval/policy", {"policy": str(spec.approval)})


def _record_selection(view: _NativeView, selection: Selection, spec: PresetSpec) -> None:
    """Persist the provider/model/preset facts when they differ from the fold."""
    state = view.fold().state
    if state.model is None or (state.model.provider, state.model.model) != (
        selection.provider,
        selection.model,
    ):
        view.append(
            "model/selection",
            {"provider": selection.provider, "model": selection.model},
        )
    selected = state.preset
    if selected is None or selected.preset != spec.id or selected.revision != spec.revision:
        view.append(
            "agent-preset/selected",
            {"preset": spec.id, "mode": spec.mode, "revision": spec.revision},
        )


def _prompt_facts(
    view: _NativeView,
    gate: PermissionGate,
    model: str,
    fallback_cwd: Path,
    presets: PermissionPresetsService,
) -> PromptFacts:
    """Build the PromptFacts the native prompt source renders context from."""
    state = view.fold().state
    cwd = Path(str(state.cwd or view.header.cwd or fallback_cwd))
    approval = str(getattr(gate, "approval", "ask") or "ask")
    policy = getattr(gate, "policy", None)
    sandbox_text = sandbox_policy_text(policy) if policy is not None else ""
    try:
        preset_name = presets.derive(view)
    except Exception:
        logger.debug("web host could not derive the permission preset", exc_info=True)
        preset_name = "custom"
    return PromptFacts(
        cwd=cwd,
        model=model,
        permission_preset=preset_name,
        approval_policy=approval,
        sandbox_policy_text=sandbox_text,
        approval_policy_text=approval_policy_text(approval),
    )


def _config_permission_default(config: Config) -> str:
    """The [permission] defaultPreset of a loaded Config, with a safe fallback."""
    table = getattr(config, "permission", None)
    value = table.get("defaultPreset") if isinstance(table, Mapping) else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_PERMISSION_PRESET


class WebSessionHost:
    """The WebService implementation: native sessions behind HTTP and SSE."""

    def __init__(
        self,
        services: HostServices,
        *,
        default_cwd: str | Path,
        journal: EventJournal | None = None,
        version: str = "",
        pending_limit: int = DEFAULT_PENDING_TURNS,
    ) -> None:
        self._services = services
        self._default_cwd = Path(default_cwd).expanduser().resolve()
        self._journal = journal if journal is not None else EventJournal()
        self._version = version
        self._pending_limit = max(int(pending_limit), 1)
        self._binding: SessionBinding | None = None
        self._pending_runtime: tuple[Config, frozenset[str], str | None] | None = None
        self._started = False
        self._closing = False
        self._approvals = ApprovalBroker(
            notify=self._approval_pending,
            on_decided=self._approval_decided,
        )

    # -- lifecycle ---------------------------------------------------------

    @property
    def journal(self) -> EventJournal:
        """The SSE replay journal owned by this host."""
        return self._journal

    @property
    def binding(self) -> SessionBinding | None:
        """The currently attached session, if any."""
        return self._binding

    @property
    def active_session_id(self) -> str | None:
        """Id of the attached session, if any."""
        binding = self._binding
        return None if binding is None else binding.session_id

    def session_is_draft(self, session_id: str) -> bool:
        """True when the attached session has not written a log yet."""
        binding = self._binding
        return (
            binding is not None
            and binding.session_id == session_id
            and not binding.handle.native_session.persisted
        )

    @property
    def services(self) -> HostServices:
        """The resolved plugin services; read-only for API projections."""
        return self._services

    @property
    def version(self) -> str:
        """The DeepPyro version reported by health and bootstrap."""
        return self._version

    @property
    def default_cwd(self) -> str:
        """The workspace a new session inherits when the request names none."""
        return str(self._default_cwd)

    @property
    def approvals(self) -> ApprovalBroker:
        """The loopback approval broker the sandbox gate asks."""
        return self._approvals

    async def start(self) -> None:
        """Materialize the preset realms the native agent needs."""
        if self._started:
            return
        self._started = True
        try:
            await self._services.presets.ensure_all()
        except Exception as exc:  # noqa: BLE001 - a broken preset must not hide the UI
            logger.warning("web host could not materialize every preset: %s", exc)

    async def close(self) -> None:
        """Detach the session and end every SSE stream."""
        if self._closing:
            return
        self._closing = True
        await self._detach()
        self._approvals.close()
        self._journal.close()

    # -- live config --------------------------------------------------------

    async def apply_runtime_config(
        self,
        config: Config,
        *,
        changed: Iterable[str],
        previous_permission_default: str | None = None,
    ) -> dict[str, Any]:
        """Adopt a reloaded Config in the running host without restarting.

        Called after ConfigEditor saved config.toml. The new Config replaces the
        HostServices snapshot immediately, so sessions created later resolve the
        new provider/model. The attached session is retargeted in place when the
        host is idle; during a streaming turn the update is deferred and applied
        before the next turn starts.

        Returns {"applied": [...], "deferred": bool, "detail": str}; callers
        surface that verbatim instead of claiming success unconditionally.
        """
        changes = frozenset(changed)
        binding = self._binding
        self._services = replace(self._services, config=config)
        if binding is None:
            return {"applied": [], "deferred": False, "detail": "no active session to rebind"}
        busy = binding.host.busy or (binding.task is not None and not binding.task.done())
        if busy:
            self._pending_runtime = (config, changes, previous_permission_default)
            return {
                "applied": [],
                "deferred": True,
                "detail": "active turn is streaming; applies before the next turn",
            }
        return await self._apply_runtime_config_now(binding, config, changes, previous_permission_default)

    async def _apply_runtime_config_now(
        self,
        binding: SessionBinding,
        config: Config,
        changes: frozenset[str],
        previous_permission_default: str | None,
    ) -> dict[str, Any]:
        """Retarget one idle binding: provider/model, limits and prompt facts."""
        applied: list[str] = []
        presets = self._services.permission_presets
        if "permission.defaultPreset" in changes and previous_permission_default is not None:
            default = _config_permission_default(config)
            if default != previous_permission_default:
                presets.set_default(default)
                with contextlib.suppress(Exception):
                    presets.apply(binding.view, default)
                applied.append("permissionPresets")
        selection = Selection(binding.provider, binding.model, binding.context_window)
        if changes & {"agent.provider", "agent.model"}:
            selection = resolve_selection(config)
        provider = self._services.llm.native_provider(selection.provider)
        facts = _prompt_facts(binding.view, binding.gate, selection.model, binding.workspace, presets)
        changed = binding.host.reconfigure(
            provider=provider,
            model=selection.model,
            temperature=config.agent.temperature,
            max_tokens_per_call=config.agent.max_tokens,
            max_steps=config.agent.max_steps,
            prompt_facts=facts,
        )
        if (selection.provider, selection.model) != (binding.provider, binding.model):
            binding.view.append(
                "model/selection",
                {"provider": selection.provider, "model": selection.model},
            )
        binding.provider = selection.provider
        binding.model = selection.model
        binding.context_window = selection.context_window
        if changed:
            applied.append("session")
        return {"applied": applied, "deferred": False, "detail": ""}

    # -- WebService protocol ----------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Liveness and identity document."""
        return {
            "status": "closing" if self._closing else "ok",
            "version": self._version,
            "active_session_id": self.active_session_id,
        }

    async def list_sessions(
        self,
        *,
        include_archived: bool = False,
        limit: int | None = SESSION_LIST_LIMIT,
        offset: int = 0,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Paginated session catalogue, newest first, from native facts.

        "limit" defaults to the sidebar page size and is clamped to
        MAX_SESSION_LIST_LIMIT; None returns the whole catalogue (slash command
        resolution uses it so a long history stays reachable). "query" is a
        case-insensitive substring match over title, id, cwd and preset,
        mirroring the SPA search box. The native list already materializes the
        full newest-first index, so the host filters and slices that one
        snapshot: a second native scan only to count rows would double the
        listing cost for every page request.
        """
        if type(limit) is not int and limit is not None:
            raise ValueError("limit must be an int or None")
        if type(offset) is not int:
            raise ValueError("offset must be an int")
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("offset and limit must be non-negative")
        if query is not None and not isinstance(query, str):
            raise ValueError("query must be a string or None")
        infos = self._services.sessions.native_list(
            include_archived=include_archived,
            limit=None,
            offset=0,
        )
        binding = self._binding
        if binding is not None and all(info.id != binding.session_id for info in infos):
            # A deferred draft has no file yet; a just-materialized one can be
            # missing from the 2 s list cache. The attached session is always
            # newest-or-known, so merging it here keeps the sidebar complete.
            infos = [self._draft_info(binding), *infos]
        needle = (query or "").strip().casefold()
        if needle:
            infos = [
                info
                for info in infos
                if any(
                    needle in (value or "").casefold()
                    for value in (info.title, info.id, info.cwd, info.preset)
                )
            ]
        total = len(infos)
        page_limit = total if limit is None else min(limit, MAX_SESSION_LIST_LIMIT)
        page = infos[offset : offset + page_limit]
        return {
            "sessions": [self._info_summary(info) for info in page],
            "active_session_id": self.active_session_id,
            "total": total,
            "limit": page_limit,
            "offset": offset,
            "has_more": offset + len(page) < total,
            "query": (query or "").strip() or None,
        }

    async def facts(self, session_id: str | None = None) -> dict[str, Any]:
        """Folded session facts for read-only commands and inspectors."""
        target = session_id or self.active_session_id
        if not target:
            raise NotFound("no session is open")
        info = self._native_info(target)
        state: Any = None
        view: SessionView | None = None
        binding = self._binding
        if binding is not None and binding.session_id == target:
            view = cast(SessionView, binding.view.fold())
            state = view.state
        else:
            try:
                log = self._services.sessions.native_read(target)
                view = fold(log.header, log.events)
                state = view.state
            except (NativeQueryError, OSError):
                view = None
                state = None
        return {
            "info": self._info_summary(info),
            "state": _state_facts(state, view),
            "active": self.active_session_id == target,
        }

    async def create_session(self, request: CreateSessionRequest) -> dict[str, Any]:
        """Create and attach a new native session."""
        self._ensure_open()
        spec = await self._ensure_preset(request.preset)
        self._refuse_team(spec)
        selection = resolve_selection(self._services.config, request.provider, request.model)
        workspace = resolve_workspace(request.cwd or self._default_cwd)
        await self._detach()
        try:
            native = self._services.sessions.native_create(
                NewSession(cwd=workspace, preset=spec.id, origin="user"),
                deferred=True,
            )
        except (OSError, ValueError, NativeQueryError) as exc:
            raise Conflict(f"could not create a session: {exc}") from exc
        handle = NativeSessionHandle(native)
        view = _NativeView(handle)
        try:
            binding = await self._attach(
                handle,
                view,
                workspace=workspace,
                selection=selection,
                spec=spec,
                record_selection=True,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                handle.aclose()
            raise
        if request.title:
            binding.view.append("session/title", {"title": request.title, "source": "user"})
        return self._binding_snapshot(binding, resumed=False)

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        """Attach an existing native session by id."""
        self._ensure_open()
        info = self._look(session_id)
        if not info.readable:
            raise Conflict(f"session {session_id} is not readable")
        current = self._binding
        if current is not None and current.session_id == session_id:
            return self._binding_snapshot(current, resumed=True)
        await self._detach()
        try:
            native = self._services.sessions.native_open(session_id)
        except SessionLockedError as exc:
            raise Conflict(f"session {session_id} is already open in another runtime") from exc
        except NativeQueryError as exc:
            raise self._native_error(exc) from exc
        except OSError as exc:
            raise Conflict(f"could not open session {session_id}: {exc}") from exc
        handle = NativeSessionHandle(native)
        view = _NativeView(handle)
        state = view.fold().state
        preset = state.preset.preset if state.preset is not None else (info.preset or DEFAULT_PRESET_ID)
        spec = await self._ensure_preset(preset)
        self._refuse_team(spec)
        provider = state.model.provider if state.model is not None else None
        model = state.model.model if state.model is not None else None
        try:
            selection = resolve_selection(self._services.config, provider, model)
        except BadRequest as exc:
            raise Conflict(f"session model is no longer configured: {exc.message}") from exc
        workspace = Path(str(state.cwd or info.cwd or self._default_cwd))
        if not workspace.is_dir():
            raise Conflict(f"session workspace no longer exists: {workspace}")
        try:
            binding = await self._attach(
                handle,
                view,
                workspace=workspace,
                selection=selection,
                spec=spec,
                record_selection=False,
            )
        except BaseException:
            with contextlib.suppress(Exception):
                handle.aclose()
            raise
        return self._binding_snapshot(binding, resumed=True)

    async def transcript(self, session_id: str) -> dict[str, Any]:
        """Replay a transcript from the native session facts."""
        info = self._look(session_id)
        binding = self._binding
        active = binding is not None and binding.session_id == session_id
        try:
            transcript = self._services.sessions.native_transcript(session_id)
        except (NativeQueryError, FileNotFoundError) as exc:
            if not active or binding is None or binding.handle.native_session.persisted:
                raise self._native_error(exc) from exc
            # A deferred draft has no log to replay yet; its in-memory fold is
            # the same facts the first append will persist.
            return {
                "session_id": session_id,
                "title": info.title,
                "readable": True,
                "issues": [],
                "entries": [
                    {
                        "role": message.role,
                        "text": message.text(),
                        "is_error": False,
                        "tool_names": [],
                    }
                    for message in binding.view.derive_messages()
                ],
                "active": True,
                "busy": bool(binding.host.busy),
            }
        except OSError as exc:
            raise Conflict(f"could not read the session transcript: {exc}") from exc
        entries = [
            {
                "role": entry.role,
                "text": entry.text,
                "is_error": entry.is_error,
                "tool_names": list(entry.tool_names),
            }
            for entry in transcript.entries
        ]
        return {
            "session_id": session_id,
            "title": info.title,
            "readable": transcript.readable,
            "issues": list(transcript.issues),
            "entries": entries,
            "active": active,
            "busy": bool(binding.host.busy) if active and binding is not None else False,
        }

    def _ledger_usage(self, session_id: str) -> tuple[dict[str, int], int]:
        """Usage totals and last request size from the local accounting ledger."""
        service = self._services.usage
        if service is None:
            return {"input": 0, "output": 0, "cached": 0}, 0
        totals = service.by_session().get(session_id)
        if totals is None:
            return {"input": 0, "output": 0, "cached": 0}, 0
        context_tokens = 0
        for record in reversed(service.records()):
            if record.session_id == session_id:
                context_tokens = record.input_tokens
                break
        return {
            "input": totals.input_tokens,
            "output": totals.output_tokens,
            "cached": totals.cached_tokens,
        }, context_tokens

    async def timeline(self, session_id: str) -> dict[str, Any]:
        """The browser transcript: messages, joined tool results and live status.

        The active session reads the in-memory native handle; any other session
        is folded from its log read-only. Tool results are joined by call id so
        the browser renders one tool card per call instead of two rows.
        """
        info = self._look(session_id)
        binding = self._binding
        active = binding is not None and binding.session_id == session_id
        if active and binding is not None:
            view = binding.view
            messages = list(view.derive_messages())
            state = view.fold().state
            usage = binding.host.usage()
            context_tokens = binding.host.context_tokens()
            if not any(int(usage.get(key, 0)) for key in ("input", "output", "cached")):
                usage, context_tokens = self._ledger_usage(session_id)
            busy = bool(binding.host.busy)
            pending = len(binding.pending)
            provider = state.model.provider if state.model is not None else binding.provider
            model = state.model.model if state.model is not None else binding.model
            preset = state.preset.preset if state.preset is not None else binding.preset
            permission = self._services.permission_presets.derive(view)
            context_window = binding.context_window
            title = state.title or info.title
            cwd = str(state.cwd or binding.workspace)
        else:
            log = self._services.sessions.native_read(session_id)
            view = fold(log.header, log.events)
            messages = list(view.messages)
            state = view.state
            usage, context_tokens = self._ledger_usage(session_id)
            busy = False
            pending = 0
            provider = state.model.provider if state.model is not None else None
            model = state.model.model if state.model is not None else None
            preset = state.preset.preset if state.preset is not None else (info.preset or DEFAULT_PRESET_ID)
            permission = state.sandbox_mode
            context_window = DEFAULT_CONTEXT_WINDOW
            title = state.title or info.title
            cwd = str(state.cwd or info.cwd)
        return {
            "session_id": session_id,
            "title": title,
            "cwd": cwd,
            "active": active,
            "busy": busy,
            "pending": pending,
            "readable": info.readable,
            "archived": info.archived,
            "provider": provider,
            "model": model,
            "preset": preset,
            "permission_preset": permission,
            "context_tokens": context_tokens,
            "context_window": context_window,
            "usage": {
                "input_tokens": int(usage.get("input", 0)),
                "output_tokens": int(usage.get("output", 0)),
                "cached_input_tokens": int(usage.get("cached", 0)),
            },
            "messages": _timeline_messages(messages),
        }

    async def rename_session(self, session_id: str, title: str, *, source: str = "user") -> dict[str, Any]:
        """Rename one session, writing through the live handle when attached."""
        normalized = normalize_native_title(title)
        binding = self._binding
        try:
            if binding is not None and binding.session_id == session_id:
                binding.view.append("session/title", {"title": normalized, "source": source})
                return {"session_id": session_id, "title": normalized, "active": True}
            info = self._services.sessions.native_rename(session_id, normalized, source=source)
        except NativeQueryError as exc:
            raise self._native_error(exc) from exc
        except OSError as exc:
            raise Conflict(f"could not rename session {session_id}: {exc}") from exc
        return {"session_id": session_id, "title": info.title, "active": False}

    async def archive_session(self, session_id: str, *, archived: bool = True) -> dict[str, Any]:
        """Archive or restore one session; only a real state change detaches it."""
        info = self._look(session_id)
        if info.archived == archived:
            # Already in the requested state: never detach (or append a marker)
            # just to repeat it, or a no-op restore would strand the host with
            # no attached session even though nothing changed.
            return {"session_id": session_id, "archived": info.archived, "detached": False}
        binding = self._binding
        detached = binding is not None and binding.session_id == session_id
        if archived and detached and not binding.handle.native_session.persisted:
            # An unused draft has nothing to archive: detaching drops it, which
            # is exactly the requested outcome and must not fail on a missing log.
            await self._detach()
            return {
                "session_id": session_id,
                "archived": True,
                "detached": True,
                "discarded": True,
            }
        if detached:
            await self._detach()
        try:
            state = self._services.sessions.native_archive(session_id, archived=archived)
        except NativeQueryError as exc:
            await self._restore_after_failed_archive(session_id, detached)
            raise self._native_error(exc) from exc
        except OSError as exc:
            await self._restore_after_failed_archive(session_id, detached)
            raise Conflict(f"could not update session {session_id}: {exc}") from exc
        return {"session_id": session_id, "archived": state, "detached": detached}

    async def _restore_after_failed_archive(self, session_id: str, detached: bool) -> None:
        """Best-effort re-attach when an archive write fails after a detach."""
        if not detached:
            return
        with contextlib.suppress(Exception):
            await self.resume_session(session_id)

    async def apply_permission(self, session_id: str, preset: str) -> dict[str, Any]:
        """Switch the permission preset of the attached session without rebuilding it."""
        binding = self._require_active(session_id)
        try:
            applied = self._services.permission_presets.apply(binding.view, preset)
        except (KeyError, ValueError) as exc:
            raise BadRequest(f"unknown permission preset: {preset}") from exc
        return {
            "session_id": session_id,
            "permission_preset": applied,
            "sandbox_mode": binding.view.fold().state.sandbox_mode,
            "approval_policy": approval_policy_text(str(binding.view.fold().state.permission_policy or "")),
        }

    async def status(self, session_id: str) -> dict[str, Any]:
        """Model, preset, usage and context status of one session."""
        info = self._look(session_id)
        binding = self._binding
        if binding is not None and binding.session_id == session_id:
            state = binding.view.fold().state
            usage = binding.host.usage()
            return {
                "session_id": session_id,
                "active": True,
                "title": state.title or info.title,
                "turns": info.turns,
                "provider": state.model.provider if state.model is not None else binding.provider,
                "model": state.model.model if state.model is not None else binding.model,
                "preset": state.preset.preset if state.preset is not None else binding.preset,
                "cwd": str(state.cwd or binding.workspace),
                "permission_preset": self._services.permission_presets.derive(binding.view),
                "context_tokens": binding.host.context_tokens(),
                "context_window": binding.context_window,
                "usage": {
                    "input_tokens": int(usage.get("input", 0)),
                    "output_tokens": int(usage.get("output", 0)),
                    "cached_input_tokens": int(usage.get("cached", 0)),
                },
                "busy": bool(binding.host.busy),
                "pending": len(binding.pending),
            }
        state = self._folded_state(session_id)
        provider = state.model.provider if state is not None and state.model is not None else None
        model = state.model.model if state is not None and state.model is not None else None
        try:
            selection = resolve_selection(self._services.config, provider, model)
        except BadRequest:
            selection = Selection(
                provider=provider or self._services.config.agent.provider,
                model=model or self._services.config.agent.model,
                context_window=DEFAULT_CONTEXT_WINDOW,
            )
        return {
            "session_id": session_id,
            "active": False,
            "title": (state.title if state is not None else None) or info.title,
            "turns": info.turns,
            "provider": selection.provider,
            "model": selection.model,
            "preset": (
                state.preset.preset
                if state is not None and state.preset is not None
                else (info.preset or DEFAULT_PRESET_ID)
            ),
            "cwd": info.cwd,
            "permission_preset": state.sandbox_mode if state is not None else None,
            "context_tokens": 0,
            "context_window": selection.context_window,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0},
            "busy": False,
            "pending": 0,
        }

    async def send_message(self, session_id: str, request: SendMessageRequest) -> dict[str, Any]:
        """Queue one user turn for the attached session."""
        self._ensure_open()
        binding = self._require_active(session_id)
        if len(binding.pending) >= self._pending_limit:
            raise TooManyRequests("the turn queue is full; wait for the active turn to finish")
        busy = bool(binding.host.busy)
        binding.pending.append(request.text)
        self._start_turn_worker(binding)
        queued = len(binding.pending) if busy else len(binding.pending) - 1
        return {"session_id": session_id, "accepted": True, "queued": max(queued, 0)}

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        """Interrupt the active turn through the native agent host.

        Accepted messages are only dropped when a turn is actually interrupted;
        when nothing is running the queue is kept (and a worker ensured) so an
        interrupt that races a fresh send can never silently lose the message.
        """
        self._ensure_open()
        binding = self._require_active(session_id)
        task = binding.task
        if not binding.host.busy or task is None or task.done():
            if binding.pending:
                self._start_turn_worker(binding)
            return {
                "session_id": session_id,
                "interrupted": False,
                "detail": "nothing to interrupt",
                "dropped": 0,
            }
        dropped = len(binding.pending)
        binding.pending.clear()
        interrupted = await binding.host.interrupt()
        return {
            "session_id": session_id,
            "interrupted": bool(interrupted),
            "detail": (
                "interrupt requested" if interrupted else "the turn finished before the interrupt landed"
            ),
            "dropped": dropped,
        }

    async def sse(self, session_id: str, *, after: int = 0) -> AsyncIterator[bytes]:
        """Stream journal frames for one session, replaying after a sequence."""
        self._look(session_id)
        async for entry in self._journal.stream(after=after):
            if entry is None:
                yield HEARTBEAT_FRAME
                continue
            if entry.event.session_id != session_id:
                continue
            yield encode_sse_frame(entry.seq, entry.event)

    # -- internals ---------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closing:
            raise ServiceUnavailable("the web host is shutting down")

    def _require_active(self, session_id: str) -> SessionBinding:
        binding = self._binding
        if binding is None or binding.session_id != session_id:
            raise NotFound(f"session {session_id} is not active; resume it first")
        return binding

    def _look(self, session_id: str) -> NativeSessionInfo:
        try:
            return self._services.sessions.native_look(session_id)
        except NativeQueryError as exc:
            binding = self._binding
            if binding is not None and binding.session_id == session_id:
                # A fresh web session is deferred: it has no file until the
                # first user message, so its row comes from the live handle.
                return self._draft_info(binding)
            raise self._native_error(exc) from exc
        except OSError as exc:
            raise Conflict(f"could not read session {session_id}: {exc}") from exc

    @staticmethod
    def _draft_info(binding: SessionBinding) -> NativeSessionInfo:
        """Build the native row for an attached, not-yet-persisted draft."""
        session = binding.handle.native_session
        state = binding.view.fold().state
        turns = sum(1 for event in session.events if event.type == "turn.started")
        return NativeSessionInfo(
            id=session.header.id,
            path=session.path,
            cwd=session.header.cwd,
            created_at=session.header.created_at,
            updated_at=session.last_ts,
            title=state.title or "",
            turns=turns,
            event_count=len(session.events),
            preset=session.header.preset,
            parent_id=session.header.parent_id,
            origin=session.header.origin,
            preview=None,
        )

    @staticmethod
    def _native_error(exc: NativeQueryError) -> HttpError:
        if getattr(exc, "code", "") == NATIVE_QUERY_NOT_FOUND:
            return NotFound(str(exc))
        return Conflict(str(exc))

    async def _ensure_preset(self, preset: str | None) -> PresetSpec:
        try:
            return await self._services.presets.ensure(preset)
        except PluginDependencyError as exc:
            raise BadRequest(str(exc)) from exc

    @staticmethod
    def _refuse_team(spec: PresetSpec) -> None:
        if spec.mode == AGENT_MODE_TEAM:
            raise Conflict("team mode is not available in the web runtime yet; choose a single-agent preset")

    def _emit(self, event: WebEvent) -> None:
        """Publish one typed hook event to the journal (the WebAgentHooks callback)."""
        self._journal.publish(event)

    async def _attach(
        self,
        handle: NativeSessionHandle,
        view: _NativeView,
        *,
        workspace: Path,
        selection: Selection,
        spec: PresetSpec,
        record_selection: bool,
    ) -> SessionBinding:
        gate = self._services.sandbox.gate(workspace, ask=self._ask_approval)
        gate.bind_session(view)
        _pin_permission(view, self._services.permission_presets)
        if record_selection:
            _record_selection(view, selection, spec)
        hooks = WebAgentHooks(handle.session_id, self._emit)
        facts = _prompt_facts(
            view,
            gate,
            selection.model,
            workspace,
            self._services.permission_presets,
        )
        build = AgentBuild(
            provider=selection.provider,
            model=selection.model,
            cwd=workspace,
            preset=spec.id,
            preset_revision=spec.revision,
            permission=gate,
            hooks=hooks,
            prompt_facts=facts,
            services=self._services.tool_services,
            context_window=selection.context_window,
            temperature=self._services.config.agent.temperature,
            max_tokens=self._services.config.agent.max_tokens,
            max_steps=self._services.config.agent.max_steps,
        )
        try:
            host = self._services.agents.host(handle.native_session, build)
        except (PluginDependencyError, ConfigError, ValueError) as exc:
            raise Conflict(f"could not build the native agent: {exc}") from exc
        binding = SessionBinding(
            handle=handle,
            view=view,
            host=host,
            hooks=hooks,
            gate=gate,
            provider=selection.provider,
            model=selection.model,
            preset=spec.id,
            preset_revision=spec.revision,
            context_window=selection.context_window,
            workspace=workspace,
        )
        self._binding = binding
        self._journal.publish(SessionEvent(session_id=binding.session_id))
        return binding

    async def _detach(self) -> None:
        binding = self._binding
        self._binding = None
        self._pending_runtime = None
        if binding is None:
            return
        self._approvals.cancel_session(binding.session_id)
        binding.pending.clear()
        if binding.host.busy:
            with contextlib.suppress(Exception):
                await binding.host.interrupt()
        task = binding.task
        binding.task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("web turn worker failed during detach", exc_info=True)
        working_directory = self._services.working_directory
        if working_directory is not None:
            with contextlib.suppress(Exception):
                working_directory.unbind(binding.view)
        binding.hooks.reset()
        with contextlib.suppress(Exception):
            await binding.host.aclose()
        with contextlib.suppress(Exception):
            binding.handle.aclose()

    def _start_turn_worker(self, binding: SessionBinding) -> None:
        """Start the per-session turn worker unless one is already running."""
        if binding.task is not None and not binding.task.done():
            return
        binding.task = asyncio.create_task(
            self._run_turns(binding),
            name=f"web-turn-{binding.session_id}",
        )

    async def _run_turns(self, binding: SessionBinding) -> None:
        try:
            while binding.pending and not self._closing:
                pending = self._pending_runtime
                if pending is not None:
                    self._pending_runtime = None
                    try:
                        await self._apply_runtime_config_now(binding, *pending)
                    except Exception:
                        logger.exception("web host could not apply pending settings")
                text = binding.pending.popleft()
                await self._run_turn(binding, text)
        finally:
            if binding.task is asyncio.current_task():
                binding.task = None
            # A cancelled worker (interrupt) can leave messages that a sender
            # accepted while the interrupt was landing. Restart so an
            # acknowledged turn is never stranded in the queue.
            if binding is self._binding and binding.pending and not self._closing:
                self._start_turn_worker(binding)

    async def _run_turn(self, binding: SessionBinding, text: str) -> None:
        session_id = binding.session_id
        try:
            result = await binding.host.send(text)
        except asyncio.CancelledError:
            self._journal.publish(FinalEvent(session_id=session_id, text=binding.host.last_assistant_text()))
            self._journal.publish(StatusEvent(session_id=session_id, phase=StatusPhase.INTERRUPTED))
            raise
        except (BudgetExceeded, ProviderStreamError) as exc:
            self._journal.publish(ErrorEvent(session_id=session_id, message=str(exc)))
            self._journal.publish(StatusEvent(session_id=session_id, phase=StatusPhase.DONE))
        except Exception as exc:
            logger.exception("web turn failed")
            # The agent loop treats an unexpected exception as a crash and
            # leaves the turn open for checkpoint recovery. On the web surface
            # the process is still alive, so close it here before accepting the
            # next message; otherwise it would fail with an OrderingError.
            try:
                binding.host.restore()
            except Exception:
                logger.exception("web host could not repair the failed turn")
            self._journal.publish(ErrorEvent(session_id=session_id, message=f"{type(exc).__name__}: {exc}"))
            self._journal.publish(StatusEvent(session_id=session_id, phase=StatusPhase.DONE))
        else:
            self._journal.publish(FinalEvent(session_id=session_id, text=result.final_text))
            usage = binding.host.usage()
            self._journal.publish(
                UsageEvent(
                    session_id=session_id,
                    input_tokens=int(usage.get("input", 0)),
                    output_tokens=int(usage.get("output", 0)),
                    cached_input_tokens=int(usage.get("cached", 0)),
                )
            )
            self._journal.publish(StatusEvent(session_id=session_id, phase=StatusPhase.DONE))
            if result.finish_reason == "length":
                self._journal.publish(ErrorEvent(session_id=session_id, message="finish_reason: length"))
        finally:
            self._approvals.cancel_session(session_id)
            # The first turn materializes a deferred draft outside the
            # create/archive paths, so the catalogue cache must be dropped or
            # the just-written session would vanish from the sidebar on TTL.
            with contextlib.suppress(Exception):
                self._services.sessions.invalidate_list_cache()

    async def _ask_approval(self, request: Any) -> bool:
        binding = self._binding
        if binding is None:
            return False
        return await self._approvals.ask(binding.session_id, request)

    def _approval_pending(self, prompt: ApprovalPrompt) -> None:
        self._journal.publish(
            ApprovalEvent(
                session_id=prompt.session_id,
                approval_id=prompt.approval_id,
                tool=prompt.tool,
                summary=prompt.summary,
                detail=prompt.detail,
                status="pending",
            )
        )

    def _approval_decided(self, prompt: ApprovalPrompt, decision: ApprovalDecision) -> None:
        self._journal.publish(
            ApprovalEvent(
                session_id=prompt.session_id,
                approval_id=prompt.approval_id,
                tool=prompt.tool,
                summary=prompt.summary,
                detail=prompt.detail,
                status=str(decision),
            )
        )

    def pending_approvals(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Snapshot the approval prompts waiting for a browser decision."""
        return [
            {
                "approval_id": prompt.approval_id,
                "session_id": prompt.session_id,
                "tool": prompt.tool,
                "summary": prompt.summary,
                "detail": prompt.detail,
                "created_at": prompt.created_at,
            }
            for prompt in self._approvals.pending(session_id)
        ]

    def decide_approval(self, approval_id: str, decision: str) -> bool:
        """Settle one pending approval; False when it was already settled."""
        try:
            return self._approvals.decide(approval_id, ApprovalDecision(decision))
        except ValueError as exc:
            raise BadRequest(f"unknown approval decision: {decision}") from exc

    def _native_info(self, session_id: str) -> NativeSessionInfo:
        try:
            return self._services.sessions.native_look(session_id)
        except (NativeQueryError, OSError) as exc:
            binding = self._binding
            if binding is not None and binding.session_id == session_id:
                return self._draft_info(binding)
            raise NotFound(f"unknown session: {session_id}") from exc

    def _folded_state(self, session_id: str) -> SessionState | None:
        try:
            log = self._services.sessions.native_read(session_id)
        except (NativeQueryError, OSError):
            return None
        return fold(log.header, log.events).state

    def _info_summary(self, info: NativeSessionInfo) -> dict[str, Any]:
        binding = self._binding
        active = binding is not None and binding.session_id == info.id
        return {
            "session_id": info.id,
            "title": info.title,
            "cwd": info.cwd,
            "turns": info.turns,
            "preset": info.preset,
            "created_at": info.created_at,
            "updated_at": info.updated_at,
            "read_only": info.read_only and not active,
            "readable": info.readable,
            "active": active,
            "busy": bool(binding.host.busy) if active and binding is not None else False,
            "archived": info.archived,
        }

    @staticmethod
    def _binding_snapshot(binding: SessionBinding, *, resumed: bool) -> dict[str, Any]:
        state = binding.view.fold().state
        return {
            "session_id": binding.session_id,
            "resumed": resumed,
            "title": state.title or "",
            "provider": binding.provider,
            "model": binding.model,
            "preset": binding.preset,
            "cwd": str(binding.workspace),
        }


def _state_facts(state: SessionState | None, view: SessionView | None) -> dict[str, Any]:
    """Serialise folded session facts into snake_case for the browser."""
    if state is None:
        return {}
    goal = state.goal
    return {
        "title": state.title or "",
        "cwd": state.cwd or "",
        "model": (
            {"provider": state.model.provider, "model": state.model.model}
            if state.model is not None
            else None
        ),
        "preset": (
            {
                "preset": state.preset.preset,
                "mode": state.preset.mode,
                "revision": state.preset.revision,
            }
            if state.preset is not None
            else None
        ),
        "permission_policy": state.permission_policy,
        "sandbox_mode": state.sandbox_mode,
        "plan_mode": state.plan_mode,
        "rounds_started": state.rounds_started,
        "goal": (
            {
                "id": goal.id,
                "revision": goal.revision,
                "objective": goal.objective,
                "phase": goal.phase,
                "max_goal_rounds": goal.max_goal_rounds,
                "blocked_reason": (
                    {"code": goal.blocked_reason.code, "message": goal.blocked_reason.message}
                    if goal.blocked_reason is not None
                    else None
                ),
            }
            if goal is not None
            else None
        ),
        "todos": [{"content": item.content, "status": item.status} for item in state.todos],
        "deliverables": [{"path": item.path, "description": item.description} for item in state.deliverables],
        "tasks": (
            [
                {
                    "task_id": task.task_id,
                    "objective": task.objective,
                    "status": task.status,
                    "owner": task.owner,
                    "created_at": task.created_at,
                }
                for task in view.tasks
            ]
            if view is not None
            else []
        ),
        "handoffs": (
            [
                {
                    "run_id": item.run_id,
                    "task_id": item.task_id,
                    "from_owner": item.from_owner,
                    "to_owner": item.to_owner,
                    "summary": item.summary,
                    "open_items": list(item.open_items),
                    "result_ok": item.result_ok,
                    "result": item.result,
                    "error": item.error,
                    "created_at": item.created_at,
                }
                for item in view.handoffs
            ]
            if view is not None
            else []
        ),
    }


def _is_missing_tool_result(message: Any) -> bool:
    """A synthetic repair result stands in for a tool call that has not finished."""
    return (
        getattr(message, "role", None) == "tool"
        and getattr(getattr(message, "source", None), "kind", None) == "repair"
        and message.text() == MISSING_TOOL_RESULT_TEXT
    )


def _timeline_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Flatten native messages into the browser timeline with joined tool results."""
    results = {
        message.tool_call_id: message
        for message in messages
        if getattr(message, "tool_call_id", None) and not _is_missing_tool_result(message)
    }
    rendered: list[dict[str, Any]] = []
    for message in messages:
        if _is_missing_tool_result(message):
            continue
        calls: list[dict[str, Any]] = []
        for call in message.tool_calls():
            result = results.get(call.id)
            calls.append(
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "result": result.text() if result is not None else None,
                    "is_error": bool(result.is_error) if result is not None else False,
                    "pending": result is None,
                }
            )
        images = [
            {"attachment": part.attachment, "media_type": part.media_type}
            for part in message.content
            if isinstance(part, ImagePart)
        ]
        reasoning = "".join(part.text for part in message.content if isinstance(part, ReasoningPart))
        source = {"kind": getattr(message.source, "kind", "")}
        for name in ("provider", "model", "call_id", "plugin", "goal_id", "revision", "round"):
            value = getattr(message.source, name, None)
            if value is not None:
                source[name] = value
        rendered.append(
            {
                "id": message.id,
                "role": message.role,
                "created_at": message.created_at,
                "text": message.text(),
                "reasoning": reasoning,
                "source": source,
                "tool_call_id": message.tool_call_id,
                "is_error": bool(message.is_error),
                "tool_calls": calls,
                "images": images,
                "has_text": any(isinstance(part, TextPart) for part in message.content),
            }
        )
    return rendered


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_PENDING_TURNS",
    "MAX_SESSION_LIST_LIMIT",
    "SESSION_LIST_LIMIT",
    "HostServices",
    "Selection",
    "SessionBinding",
    "WebSessionHost",
    "context_window",
    "resolve_host_services",
    "resolve_plugin_services",
    "resolve_selection",
    "resolve_workspace",
]
