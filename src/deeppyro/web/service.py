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
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
from deeppyro.config import Config, ProviderConfig
from deeppyro.core.errors import ConfigError
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
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.fold import SessionState, fold
from deeppyro.session.native_control import NewSession
from deeppyro.session.native_index import NativeSessionInfo
from deeppyro.session.native_query import NATIVE_QUERY_NOT_FOUND, NativeQueryError
from deeppyro.subagent.manager import SubagentService
from deeppyro.team.manager import TeamService
from deeppyro.user_questions.service import UserQuestionsService
from deeppyro.web.events import (
    HEARTBEAT_FRAME,
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

#: How many recent sessions the sidebar catalogue returns.
SESSION_LIST_LIMIT = 50

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
        self._started = False
        self._closing = False

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
        self._journal.close()

    # -- WebService protocol ----------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Liveness and identity document."""
        return {
            "status": "closing" if self._closing else "ok",
            "version": self._version,
            "active_session_id": self.active_session_id,
        }

    async def list_sessions(self) -> dict[str, Any]:
        """Session catalogue, newest first, from native facts."""
        infos = self._services.sessions.native_list(
            include_archived=False,
            limit=SESSION_LIST_LIMIT,
        )
        return {
            "sessions": [self._info_summary(info) for info in infos],
            "active_session_id": self.active_session_id,
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
                NewSession(cwd=workspace, preset=spec.id, origin="user")
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
        try:
            transcript = self._services.sessions.native_transcript(session_id)
        except NativeQueryError as exc:
            raise self._native_error(exc) from exc
        except OSError as exc:
            raise Conflict(f"could not read the session transcript: {exc}") from exc
        binding = self._binding
        active = binding is not None and binding.session_id == session_id
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
        if binding.task is None or binding.task.done():
            binding.task = asyncio.create_task(
                self._run_turns(binding),
                name=f"web-turn-{session_id}",
            )
        queued = len(binding.pending) if busy else len(binding.pending) - 1
        return {"session_id": session_id, "accepted": True, "queued": max(queued, 0)}

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        """Interrupt the active turn through the native agent host."""
        binding = self._require_active(session_id)
        dropped = len(binding.pending)
        binding.pending.clear()
        task = binding.task
        if not binding.host.busy or task is None or task.done():
            return {
                "session_id": session_id,
                "interrupted": False,
                "detail": "nothing to interrupt",
                "dropped": dropped,
            }
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
            raise self._native_error(exc) from exc
        except OSError as exc:
            raise Conflict(f"could not read session {session_id}: {exc}") from exc

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
        gate = self._services.sandbox.gate(workspace, ask=_deny_approval)
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
        if binding is None:
            return
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

    async def _run_turns(self, binding: SessionBinding) -> None:
        try:
            while binding.pending and not self._closing:
                text = binding.pending.popleft()
                await self._run_turn(binding, text)
        finally:
            if binding.task is asyncio.current_task():
                binding.task = None

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
        except Exception as exc:
            logger.exception("web turn failed")
            self._journal.publish(ErrorEvent(session_id=session_id, message=f"{type(exc).__name__}: {exc}"))
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

    def _folded_state(self, session_id: str) -> SessionState | None:
        try:
            log = self._services.sessions.native_read(session_id)
        except (NativeQueryError, OSError):
            return None
        return fold(log.header, log.events).state

    @staticmethod
    def _info_summary(info: NativeSessionInfo) -> dict[str, Any]:
        return {
            "session_id": info.id,
            "title": info.title,
            "cwd": info.cwd,
            "turns": info.turns,
            "preset": info.preset,
            "created_at": info.created_at,
            "updated_at": info.updated_at,
            "read_only": info.read_only,
            "readable": info.readable,
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


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "DEFAULT_PENDING_TURNS",
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
