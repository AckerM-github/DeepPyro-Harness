"""Connection state and low-level plumbing for the agent session."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from deeppyro.agent.inbox import Inbox
from deeppyro.agent.options import AgentOptions
from deeppyro.agent.stream import AssistantStreamAccumulator
from deeppyro.api.services import ServiceBag
from deeppyro.compact.compactor import Compactor
from deeppyro.core.protocols import EventSink, LlmProvider
from deeppyro.core.wire import LogRecord
from deeppyro.hooks.service import HookService
from deeppyro.llm.retry import RetryPolicy
from deeppyro.prompt import build_system_prompt, collect_instructions
from deeppyro.working_directory import WorkingDirectoryService

from .session_support import SessionHandle, StreamAccumulator, T, ToolCallDraft, logger

if TYPE_CHECKING:
    from deeppyro.api import PluginContext


class _SessionState:
    """Connection state and low-level plumbing for the agent session."""

    def __init__(
        self,
        *,
        session: SessionHandle,
        provider: LlmProvider,
        registry: Any,
        options: AgentOptions,
        sink: EventSink,
        permission: Any | None = None,
        cwd: Path,
        services: dict[str, Any] | None = None,
        compactor: Compactor | None = None,
        prompt_builder: Any | None = None,
        surface_registry: Any | None = None,
        runtime_context: bool = True,
        service_access: PluginContext | None = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._registry = registry
        # The surface registry is what the model is offered directly; PTC mode
        # narrows it to run_code while the full registry stays available to the
        # nested dispatcher and to the system prompt (tools:sdk).
        self._surface = surface_registry if surface_registry is not None else registry
        #: The registry this session was asked to present (None means native).
        self.surface_registry = surface_registry
        #: The preset id that built this agent; the agents service attaches the
        #: real value right after construction, so the loop reads it lazily.
        self.preset: str | None = None
        #: Whether each step writes the per-turn runtime-context projection.
        #: Presets that hide host facts (minimal compositions) turn it off.
        self._runtime_context = bool(runtime_context)
        self._options = options
        self._sink = sink
        self._permission = permission
        # Bind the gate to this session so the standing sandbox mode and
        # approval policy resolve from the session's own log (permission/preset,
        # sandbox/mode, approval/policy records).
        bind_session = getattr(permission, "bind_session", None)
        if callable(bind_session):
            bind_session(session)
        self._cwd = Path(cwd)
        self._services = dict(services or {})
        # ToolContext.services always exposes the live session so tools that need
        # durable session access (for example compress) never have to reopen it.
        self._services.setdefault("session", session)
        # PTC and future transports dispatch nested tools through the agent's
        # composed registry, narrowed to this agent's preset.
        self._services.setdefault("tool_registry", registry)
        # Typed lookups read the live plugin context first (types survive
        # locally scoped services) and fall back to the plain name dictionary.
        self._service_access = service_access
        self._bag = ServiceBag(self._services)
        self._prompt_builder = prompt_builder or build_system_prompt
        hooks = self._service(HookService)
        self._hooks: HookService | None = hooks
        self._retry_policy: Any | None = self._service(RetryPolicy)
        if self._retry_policy is None:
            self._retry_policy = self._services.get("retryPolicy")
        if self._retry_policy is None:
            self._retry_policy = self._services.get("retry_policy")
        self._model = options.model or str(getattr(provider, "model", "") or "") or "unknown"
        self._instructions = [*collect_instructions(self._cwd), *options.instructions]
        self._append_supports_envelope = self._detect_envelope_support()
        self._inbox = Inbox.from_records(list(self._records()), log=self._session, emit=self._inbox_event)
        self._inbox_tasks: set[asyncio.Task[Any]] = set()
        self._header_logged = False
        self._phase = "idle"
        self._driving = False
        self._driver_task: asyncio.Task[Any] | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._interrupt_requested = False
        self._cancel_event = asyncio.Event()
        self._turn_open = False
        self._step_open = False
        self._turn = 0
        self._turn_conclusion: dict[str, Any] | None = None
        self._step = 0
        self._acc: StreamAccumulator | None = None
        self._stream: AssistantStreamAccumulator | None = None
        self._assistant_seq: int | None = None
        self._assistant_appended = False
        self._attempt_appended = False
        self._pending_calls: list[ToolCallDraft] = []
        self._undispatched_calls: list[ToolCallDraft] = []
        # callId -> seq of the tool/call record, so abort results can reference it.
        self._call_seqs: dict[str, int] = {}
        # Which lifecycle boundary raised the current failure: "request" while
        # the provider streams, "tool" while a batch runs, "turn" otherwise.
        self._failure_phase = "turn"
        self.compactor = compactor or Compactor(
            provider=provider,
            model=self._model,
            keep_recent_tokens=8000,
            context_window=options.context_window,
            threshold=options.compact_threshold,
        )
        self.last_compaction_error: BaseException | None = None

    @property
    def busy(self) -> bool:
        """True while the driver is running (including tool calls and streams)."""
        return self._driving

    @property
    def model(self) -> str:
        return self._model

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def step(self) -> int:
        return self._step

    @property
    def inbox(self) -> Inbox:
        return self._inbox

    @property
    def session_id(self) -> str:
        """The durable session id (the default hook dispatch scope key)."""
        return self._session.session_id

    @property
    def session(self) -> Any:
        """The durable session this agent appends to.

        Plugins that observe a turn through a hook use it to read records or to
        append their own (titles, notes) without opening a second handle.
        """
        return self._session

    @property
    def provider_name(self) -> str:
        """Name of the provider that serves this agent's requests."""
        return self._provider.name

    def _service(self, contract: type[T]) -> T | None:
        """Resolve one typed service: live plugin context first, name bag second."""
        if self._service_access is not None:
            value = self._service_access.get(contract)
            if value is not None:
                return value
        return self._bag.get(contract)

    def _current_cwd(self) -> Path:
        service = self._service(WorkingDirectoryService)
        getter = getattr(service, "get", None)
        if callable(getter):
            try:
                current = getter(self._session)
            except Exception:  # noqa: BLE001 - the working-directory service is injected by plugins
                logger.warning("failed to read the working directory service", exc_info=True)
            else:
                if current is not None:
                    return Path(cast(str, current))
        return self._cwd

    def _hook_scope(self) -> dict[str, Any]:
        """The dispatch scope for hook calls made by this agent.

        The session id isolates concurrent sessions (a lead and its subagents);
        the preset id lets plugins target a whole agent mode without knowing
        which session is running.  The preset attribute is attached by the
        agents service right after construction, so it is read lazily.
        """
        scope: dict[str, Any] = {"session": self.session_id}
        preset = self.preset
        if isinstance(preset, str) and preset:
            scope["preset"] = preset
        return scope

    def _hook_enabled(self, name: str) -> bool:
        """Return True when dispatching name can reach a listener or an observer.

        A dispatch without listeners is skipped as a fast path, but registered
        hook observers (the P2 hook journal and bridge) must still see the
        dispatch so fallback and veto decisions are replayable from the log.
        """
        hooks = self._hooks
        if hooks is None:
            return False
        if hooks.has(name):
            return True
        return hooks.observed

    def _detect_envelope_support(self) -> bool:
        append = getattr(self._session, "append", None)
        if append is None:
            return False
        try:
            signature = inspect.signature(append)
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )

    def _records(self) -> list[LogRecord]:
        return self._session.records

    def _emit(self, event: dict[str, Any]) -> None:
        self._sink.emit(event)

    def _inbox_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event: dict[str, Any] = {"type": event_type}
        event.update(payload)
        self._emit(event)
        self._publish_inbox(event_type, payload)

    def _publish_inbox(self, event_type: str, payload: dict[str, Any]) -> None:
        """Mirror one inbox notification onto the bus, without blocking the caller.

        The inbox mutates synchronously, so the bus dispatch is scheduled and
        failures are logged only: an observer must never break input handling.
        """
        hooks = self._hooks
        if hooks is None or not self._hook_enabled(event_type):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop means no bus dispatch
            return
        task = loop.create_task(hooks.emit(event_type, payload, scope=self._hook_scope()))
        self._inbox_tasks.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self._inbox_tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                logger.warning("failed to publish %s", event_type, exc_info=error)

        task.add_done_callback(done)
