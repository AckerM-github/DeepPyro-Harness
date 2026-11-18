"""Native subagent service: spawn and fork children on the native runtime.

This module is the additive native branch of :mod:`deeppyro.subagent.manager`.
When the subagent service is bound to a native parent session host it creates
children through ``SessionsPlugin.native_create`` (origin ``"subagent"``) and
prefix-forks them through :func:`deeppyro.session.native_fork.fork_native_session`
bounded at the parent's last completed turn.  Child agents are built with
``AgentsPlugin.host`` so each child session is owned by one native
:class:`~deeppyro.agent.native_host.NativeAgentHost`; the v4 record/session
machinery is not involved.

Durable parent facts are schema-valid ``plugin.subagent.*`` events:

* ``plugin.subagent.started``  one per child, written before its first run;
* ``plugin.subagent.finished`` one per child, with a status and usage flag;
* ``plugin.subagent.notice``   one per settlement notice delivery attempt.

Token usage, latency and retries never enter the session log: they live in
:class:`~deeppyro.subagent.types.SubagentUsage` and are offered to the optional
usage sink, so replay and evaluation read them from the trace.  The parent
notification is a bounded in-memory notice delivered through the parent host
(``NativeAgentHost.deliver``); while the parent is mid-turn it waits in the host
notice queue and drains into the log on the next run.

The service is duck-typed at every boundary (agents/sessions plugins, parent
host, child host) so unit tests can substitute the same fakes the v4 tests use.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import inspect
import logging
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from deeppyro.agent.native_factory import AgentBuild, PromptFacts
from deeppyro.api.types import DEFAULT_PRESET_ID
from deeppyro.core.messages import PluginSource
from deeppyro.session.native_control import NewSession
from deeppyro.session.native_fork import (
    NativeForkError,
    fork_native_session,
    forked_event_count,
)
from deeppyro.session.schema import EventKind, NativeSession
from deeppyro.subagent.errors import SubagentError
from deeppyro.subagent.types import (
    DEFAULT_CONTEXT_WINDOW,
    MAX_LIVE_RUNS,
    SubagentActivation,
    SubagentResult,
    SubagentUsage,
    sum_usages,
)

STARTED_EVENT = "plugin.subagent.started"
FINISHED_EVENT = "plugin.subagent.finished"
NOTICE_EVENT = "plugin.subagent.notice"
PLUGIN = "subagent"

_ORPHAN = ""
_ACTIVATION_MODES = ("spawn", "fork")
#: Set while a child host runs its turn; a nested delegation is refused, not crashed.
_CHILD_RUN: contextvars.ContextVar[int] = contextvars.ContextVar("deeppyro_native_subagent_depth", default=0)
NESTED_ERROR = (
    "nested subagent delegation is not supported on the native runtime; "
    "a subagent cannot start another subagent"
)
logger = logging.getLogger(__name__)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SubagentError("expected a string value")
    return value.strip()


def _required_text(value: Any, field_name: str) -> str:
    text = _text(value)
    if not text:
        raise SubagentError(f"{field_name} must be a non-empty string")
    return text


def _provider_name(provider: Any) -> str:
    name = getattr(provider, "name", None)
    return name if isinstance(name, str) else str(name or "")


def _activation_mode(mode: Any) -> str:
    if mode in _ACTIVATION_MODES:
        return mode
    raise SubagentError(f"unsupported subagent mode: {mode!r}")


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _native_root(sessions: Any) -> Path | None:
    native = getattr(sessions, "native", None)
    root = getattr(native, "root", None)
    return Path(root) if root is not None else None


def _fork_boundary(events: tuple[Any, ...]) -> int:
    """The last completed turn's seq, or the source tail when no turn closed."""
    for event in reversed(events):
        if getattr(event, "type", None) == EventKind.TURN_ENDED.value:
            seq = getattr(event, "seq", None)
            if type(seq) is int:
                return seq
    if not events:
        return 0
    last_seq = getattr(events[-1], "seq", 0)
    return last_seq if type(last_seq) is int else 0


def _assistant_count(session: NativeSession | None) -> int:
    if session is None:
        return 0
    return sum(1 for event in session.events if event.type == EventKind.ASSISTANT_MESSAGE.value)


def _host_usage(host: Any) -> tuple[int, int, int]:
    usage = getattr(host, "usage", None)
    if not callable(usage):
        return 0, 0, 0
    try:
        totals = usage()
    # boundary: the child host is a duck-typed object; usage is reporting only
    except Exception:  # noqa: BLE001
        return 0, 0, 0
    if not isinstance(totals, Mapping):
        return 0, 0, 0
    return _count(totals.get("input")), _count(totals.get("output")), _count(totals.get("cached"))


def _outcome_text(outcome: Any) -> str:
    text = getattr(outcome, "final_text", None)
    if isinstance(text, str):
        return text
    return str(text or "")


def _outcome_steps(outcome: Any) -> int:
    return _count(getattr(outcome, "steps", 0))


def _parent_session_of(candidate: Any) -> NativeSession | None:
    if isinstance(candidate, NativeSession):
        return candidate
    native = getattr(candidate, "native_session", None)
    return native if isinstance(native, NativeSession) else None


def _parent_host_of(candidate: Any) -> Any:
    if candidate is None:
        return None
    if callable(getattr(candidate, "deliver", None)) or callable(getattr(candidate, "inject", None)):
        return candidate
    handle = getattr(candidate, "handle", None)
    return handle if callable(getattr(handle, "inject", None)) else None


def _parent_prompt_facts(host: Any) -> PromptFacts | None:
    facts = getattr(host, "_prompt_facts", None)
    return facts if isinstance(facts, PromptFacts) else None


def _child_facts(events: tuple[Any, ...]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    started: dict[str, dict[str, Any]] = {}
    finished: set[str] = set()
    for event in events:
        data = getattr(event, "data", None)
        if not isinstance(data, Mapping):
            continue
        child_id = data.get("child_session_id")
        if not isinstance(child_id, str) or not child_id:
            continue
        event_type = getattr(event, "type", "")
        if event_type == STARTED_EVENT:
            started.setdefault(child_id, dict(data))
        elif event_type == FINISHED_EVENT:
            finished.add(child_id)
    return started, finished


def _fact_str(fact: Mapping[str, Any], key: str, default: str = "") -> str:
    value = fact.get(key)
    return value if isinstance(value, str) else default


def _fact_int(fact: Mapping[str, Any], key: str, default: int) -> int:
    value = fact.get(key)
    return value if type(value) is int else default


def _fact_mode(fact: Mapping[str, Any]) -> str:
    mode = fact.get("mode")
    return mode if mode in _ACTIVATION_MODES else "spawn"


def _notice_parts(result: SubagentResult) -> tuple[str, str]:
    if result.is_error:
        summary = f"Background subagent {result.child_id} failed before it finished."
    else:
        summary = (
            f"Background subagent {result.child_id} finished and will do no further work "
            "unless you send it more."
        )
    closing = result.text.strip()
    if closing:
        return summary, f"{summary}\nIts closing message:\n{closing}"
    return summary, f"{summary}\nIt left no closing message."


async def _close_quietly(host: Any) -> None:
    close = getattr(host, "aclose", None)
    if not callable(close):
        return
    try:
        outcome = close()
        if inspect.isawaitable(outcome):
            await outcome
    # boundary: closing a child host must never mask the child's result
    except Exception:
        logger.warning("failed to close native subagent host", exc_info=True)


def _close_native_session(session: NativeSession | None) -> None:
    if session is None:
        return
    try:
        session.close()
    # boundary: the lock may already be released by a failed open or a host close
    except Exception:
        logger.warning("failed to close native subagent session", exc_info=True)


class NativeSubagentService:
    """Run one child native session per subagent, spawned or prefix-forked.

    Lifecycle facts land on the bound parent session as ``plugin.subagent.*``
    events; measured usage stays in :class:`SubagentUsage` and the optional
    usage sink.  Every child gets a host built by the agents plugin, so the
    native loop, trace and checkpoint stores own its run.
    """

    def __init__(
        self,
        *,
        agents: Any = None,
        sessions: Any = None,
        max_runs: int = MAX_LIVE_RUNS,
    ) -> None:
        self._agents = agents
        self._sessions = sessions
        self._max_runs = max_runs
        self._cwd: Path | None = None
        self._provider: Any = None
        self._registry: Any = None
        self._permission: Any = None
        self._model = ""
        self._context_window = DEFAULT_CONTEXT_WINDOW
        self._preset = DEFAULT_PRESET_ID
        self._preset_revision: int | None = None
        self._services: dict[str, Any] = {}
        self._ask: Any = None
        self._usage_sink: Any = None
        self._prompt_facts: PromptFacts | None = None
        self._parent_session: NativeSession | None = None
        self._parent_host: Any = None
        self._tasks: dict[str, asyncio.Task[SubagentResult]] = {}
        self._hosts: dict[str, Any] = {}
        self._sessions_by_id: dict[str, NativeSession] = {}
        self._activations: dict[str, SubagentActivation] = {}
        self._baselines: dict[str, int] = {}
        self._usages: dict[str, SubagentUsage] = {}
        self._settled: set[str] = set()
        self._active_ids: set[str] = set()
        self._labels = 0
        self._live = 0
        self._closed = False

    # -- configuration ---------------------------------------------------

    @property
    def max_runs(self) -> int:
        """The maximum number of concurrent children."""
        return int(self._max_runs)

    @property
    def live_runs(self) -> int:
        """The number of children still running."""
        return self._live

    @property
    def parent_session_id(self) -> str:
        """The bound parent native session id, or an empty string."""
        parent = self._parent_session
        if parent is None:
            return _ORPHAN
        return parent.header.id

    @property
    def parent_session(self) -> NativeSession | None:
        """The bound parent native session, when one is bound."""
        return self._parent_session

    def configure(
        self,
        *,
        cwd: Any,
        provider: Any = None,
        registry: Any = None,
        permission: Any = None,
        model: str = "",
        context_window: int = DEFAULT_CONTEXT_WINDOW,
        preset: str = DEFAULT_PRESET_ID,
        preset_revision: int | None = None,
        services: dict[str, Any] | None = None,
        ask: Any = None,
        usage_sink: Any = None,
        prompt_facts: Any = None,
    ) -> None:
        """Configure the child defaults, mirroring the v4 service surface."""
        self._cwd = Path(cwd)
        self._provider = provider
        self._registry = registry
        self._permission = permission
        self._model = _text(model)
        self._context_window = int(context_window)
        self._preset = _text(preset) or DEFAULT_PRESET_ID
        self._preset_revision = preset_revision
        self._services = dict(services or {})
        self._ask = ask
        if usage_sink is not None:
            self._usage_sink = usage_sink
        self._prompt_facts = prompt_facts if isinstance(prompt_facts, PromptFacts) else None

    def bind_session(self, session: Any) -> None:
        """Bind the parent native session that receives the lifecycle facts."""
        if session is None:
            self._parent_session = None
            self._parent_host = None
            return
        parent = _parent_session_of(session)
        if parent is None:
            raise SubagentError("native subagent service requires a native session")
        self._parent_session = parent
        self._parent_host = _parent_host_of(session)

    def bind_parent(self, parent: Any) -> None:
        """Bind the parent host, or its session handle, that receives notices."""
        if parent is None:
            self._parent_host = None
            return
        session = _parent_session_of(parent)
        host = _parent_host_of(parent)
        if session is None and host is not None:
            session = _parent_session_of(host)
        if session is not None:
            self._parent_session = session
        self._parent_host = host

    def set_ask(self, ask: Any) -> None:
        """Remember the ask callback for child permission gates."""
        self._ask = ask

    # -- runs ------------------------------------------------------------

    async def run(
        self,
        prompt: str,
        *,
        description: str = "",
        name: str | None = None,
        provider: Any = None,
        model: str | None = None,
        preset: str | None = None,
    ) -> SubagentResult:
        """Run one child to completion and return its result."""
        return await self._run_task(
            prompt,
            description=description,
            name=name,
            provider=provider,
            model=model,
            preset=preset,
            mode="spawn",
            notify=False,
        )

    async def start(
        self,
        prompt: str,
        *,
        description: str = "",
        name: str | None = None,
        provider: Any = None,
        model: str | None = None,
        preset: str | None = None,
        mode: str = "spawn",
    ) -> SubagentActivation:
        """Start one child in the background and return its activation."""
        return await self._start(
            prompt,
            description=description,
            name=name,
            provider=provider,
            model=model,
            preset=preset,
            mode=mode,
            notify=True,
        )

    async def _run_task(
        self,
        prompt: Any,
        *,
        description: str,
        name: str | None,
        provider: Any,
        model: str | None,
        preset: str | None,
        mode: Any,
        notify: bool,
    ) -> SubagentResult:
        activation = await self._start(
            prompt,
            description=description,
            name=name,
            provider=provider,
            model=model,
            preset=preset,
            mode=mode,
            notify=notify,
        )
        task = self._tasks.get(activation.child_id)
        if task is None:
            raise SubagentError("subagent background task disappeared")
        try:
            return await task
        finally:
            self._tasks.pop(activation.child_id, None)

    async def _start(
        self,
        prompt: Any,
        *,
        description: str,
        name: str | None,
        provider: Any,
        model: str | None,
        preset: str | None,
        mode: Any,
        notify: bool,
    ) -> SubagentActivation:
        text = _required_text(prompt, "prompt")
        if _CHILD_RUN.get():
            raise SubagentError(NESTED_ERROR)
        parent = self._ensure_ready()
        resolved_mode = _activation_mode(mode)
        if self._live >= self._max_runs:
            raise SubagentError(f"at most {self._max_runs} concurrent subagent runs are allowed")
        if resolved_mode == "fork":
            if provider is not None or model is not None or preset is not None:
                raise SubagentError("fork does not support provider, model, or preset overrides")
            provider_name = _provider_name(self._provider)
            model_id = self._model
            child_session, inherited_event_count = self._create_fork_child(parent)
        else:
            resolved_provider = self._resolve_provider(provider)
            provider_name = _provider_name(resolved_provider)
            preset_id = _text(preset) or self._preset
            model_id = _text(model) or self._model
            child_session = self._create_child_session(parent, preset_id)
            inherited_event_count = 0
        preset_id = child_session.header.preset
        preset_revision = self._preset_revision if preset_id == self._preset else None
        child_id = child_session.header.id
        label = self._next_label(name)
        try:
            host = self._create_host(
                child_session,
                provider=provider_name,
                model=model_id,
                preset=preset_id,
                preset_revision=preset_revision,
            )
        except BaseException:
            _close_native_session(child_session)
            raise
        activation = SubagentActivation(
            child_id=child_id,
            session_id=child_id,
            label=label,
            provider=provider_name,
            model=model_id,
            mode=resolved_mode,
            created_at=int(child_session.header.created_at),
        )
        self._activations[child_id] = activation
        self._baselines[child_id] = _assistant_count(child_session)
        self._sessions_by_id[child_id] = child_session
        self._hosts[child_id] = host
        self._append_fact(
            STARTED_EVENT,
            {
                "version": 1,
                "child_session_id": child_id,
                "label": label,
                "provider": provider_name,
                "model": model_id,
                "mode": resolved_mode,
                "inherited_event_count": inherited_event_count,
                "created_at": activation.created_at,
            },
        )
        self._live += 1
        self._active_ids.add(child_id)
        task = asyncio.create_task(
            self._activate(text, child_id, notify=notify),
            name=f"subagent:{child_id}",
        )
        self._tasks[child_id] = task
        task.add_done_callback(lambda done, cid=child_id: self._on_task_done(cid, done))
        return activation

    async def _activate(self, prompt: str, child_id: str, *, notify: bool) -> SubagentResult:
        started = time.perf_counter()
        activation = self._activations[child_id]
        host = self._hosts[child_id]
        session = self._sessions_by_id[child_id]
        result: SubagentResult | None = None
        try:
            steps = 0
            nested_token = _CHILD_RUN.set(_CHILD_RUN.get() + 1)
            try:
                try:
                    outcome = await host.send(prompt)
                except asyncio.CancelledError:
                    raise
                # boundary: a child host runs the whole native loop; any failure is its result
                except Exception as exc:  # noqa: BLE001
                    result = self._result_for(
                        activation,
                        is_error=True,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    result = self._result_for(activation, text=_outcome_text(outcome))
                    steps = _outcome_steps(outcome)
            finally:
                _CHILD_RUN.reset(nested_token)
            usage = self._measure_usage(activation, host, session, started, steps=steps)
            result.usage = usage
            self._publish_usage(activation, usage)
            self._settle(
                child_id,
                status="error" if result.is_error else "ok",
                result=result,
            )
            if notify:
                await self._notify_parent(result)
            return result
        finally:
            if result is None:
                # a cancelled child still reports the usage it already spent
                with contextlib.suppress(Exception):
                    usage = self._measure_usage(activation, host, session, started, steps=0)
                    cancelled = self._result_for(activation)
                    cancelled.usage = usage
                    self._publish_usage(activation, usage)
                    self._settle(child_id, status="cancelled", result=cancelled)
            self._release(child_id)
            self._activations.pop(child_id, None)
            self._baselines.pop(child_id, None)
            self._sessions_by_id.pop(child_id, None)
            self._hosts.pop(child_id, None)
            await _close_quietly(host)
            _close_native_session(session)

    async def wait_children(self, timeout: float | None = None) -> None:
        """Wait for the background children, raising on a timeout."""
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._tasks.values()
            if task is not None and task is not current and not task.done()
        ]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            raise SubagentError(f"timed out waiting for {len(pending)} background subagent(s)")
        await asyncio.gather(*done, return_exceptions=True)

    async def cancel_all(self) -> int:
        """Cancel every running child and return how many were cancelled."""
        current = asyncio.current_task()
        tasks = {
            child_id: task
            for child_id, task in self._tasks.items()
            if task is not None and task is not current and not task.done()
        }
        if not tasks:
            return 0
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        for child_id in tasks:
            activation = self._activations.get(child_id)
            if activation is not None:
                cancelled = self._result_for(activation)
                self._publish_usage(
                    activation,
                    SubagentUsage(model=activation.model, provider=activation.provider),
                )
                self._settle(child_id, status="cancelled", result=cancelled)
            self._release(child_id)
            host = self._hosts.pop(child_id, None)
            await _close_quietly(host)
            session = self._sessions_by_id.pop(child_id, None)
            _close_native_session(session)
            self._activations.pop(child_id, None)
            self._baselines.pop(child_id, None)
            self._tasks.pop(child_id, None)
        return len(tasks)

    async def close(self) -> None:
        """Cancel every child and release the hosts held for restore."""
        self._closed = True
        await self.cancel_all()
        for child_id, host in list(self._hosts.items()):
            self._hosts.pop(child_id, None)
            session = self._sessions_by_id.pop(child_id, None)
            await _close_quietly(host)
            _close_native_session(session)
        self._parent_host = None

    # -- restore ---------------------------------------------------------

    async def restore(self) -> tuple[str, ...]:
        """Reopen children that started but never finished before a restart.

        The parent session is the source of truth: for every
        ``plugin.subagent.started`` child id without a
        ``plugin.subagent.finished`` fact this reopens the child session,
        rebuilds its host and lets the host repair a turn that was open when the
        process disposed.  Broken children are logged and skipped; the returned
        ids are the ones brought back.
        """
        if self._closed or self._agents is None or self._sessions is None or self._cwd is None:
            return ()
        parent = self._parent_session
        if parent is None:
            return ()
        opener: Any = getattr(self._sessions, "native_open", None)
        if not callable(opener):
            return ()
        started, finished = _child_facts(parent.events)
        restored: list[str] = []
        for child_id, fact in started.items():
            if child_id in finished or child_id in self._hosts or child_id in self._tasks:
                continue
            try:
                child = cast(NativeSession, opener(child_id))
            # boundary: a child session may be missing, locked or corrupt after a crash
            except Exception as exc:  # noqa: BLE001
                logger.warning("cannot restore native subagent %s: %s", child_id, exc)
                continue
            activation = SubagentActivation(
                child_id=child_id,
                session_id=child_id,
                label=_fact_str(fact, "label", child_id),
                provider=_fact_str(fact, "provider"),
                model=_fact_str(fact, "model"),
                mode=_fact_mode(fact),
                created_at=_fact_int(fact, "created_at", int(child.header.created_at)),
            )
            preset = child.header.preset
            try:
                host = self._create_host(
                    child,
                    provider=activation.provider,
                    model=activation.model,
                    preset=preset,
                    preset_revision=self._preset_revision if preset == self._preset else None,
                )
            # boundary: one broken child must never block restoring the others
            except Exception as exc:  # noqa: BLE001
                logger.warning("cannot restore native subagent %s: %s", child_id, exc)
                _close_native_session(child)
                continue
            try:
                host.restore()
            # boundary: repairing a disposed child is best effort during recovery
            except Exception:
                logger.warning("failed to repair native subagent %s", child_id, exc_info=True)
            self._activations[child_id] = activation
            self._baselines[child_id] = _assistant_count(child)
            self._sessions_by_id[child_id] = child
            self._hosts[child_id] = host
            restored.append(child_id)
        return tuple(restored)

    # -- usage -----------------------------------------------------------

    def usage(self, child_id: str | None = None) -> SubagentUsage | None:
        """The measured usage of one child, or the aggregate of every child."""
        if child_id is not None:
            return self._usages.get(child_id)
        if not self._usages:
            return None
        return sum_usages(self._usages.values())

    # -- internals -------------------------------------------------------

    def _ensure_ready(self) -> NativeSession:
        if self._closed:
            raise SubagentError("native subagent service is closed")
        if self._agents is None or self._sessions is None:
            raise SubagentError("native subagent service requires the agents and sessions services")
        if self._cwd is None:
            raise SubagentError("native subagent service is not configured")
        parent = self._parent_session
        if parent is None:
            raise SubagentError("native subagent service has no bound parent session")
        if parent.closed:
            raise SubagentError("native subagent service parent session is closed")
        return parent

    def _create_child_session(self, parent: NativeSession, preset_id: str) -> NativeSession:
        create: Any = getattr(self._sessions, "native_create", None)
        if not callable(create):
            raise SubagentError("sessions service does not expose the native control plane")
        if self._cwd is None:
            raise SubagentError("native subagent service is not configured")
        depth = int(parent.header.delegation_depth) + 1
        spec = NewSession(
            cwd=self._cwd,
            preset=preset_id,
            origin="subagent",
            parent_id=parent.header.id,
            delegation_depth=depth,
        )
        try:
            return cast(NativeSession, create(spec))
        # boundary: the sessions plugin is a duck-typed native service
        except Exception as exc:
            raise SubagentError(f"cannot create native subagent session: {exc}") from exc

    def _create_fork_child(self, parent: NativeSession) -> tuple[NativeSession, int]:
        boundary = _fork_boundary(parent.events)
        root = _native_root(self._sessions)
        try:
            child = fork_native_session(parent.header.id, root=root, through_seq=boundary)
        except NativeForkError as exc:
            raise SubagentError(f"cannot fork subagent session: {exc}") from exc
        # boundary: the fork helper is another owner's native module
        except Exception as exc:
            raise SubagentError(f"cannot fork subagent session: {exc}") from exc
        return child, forked_event_count(child)

    def _resolve_provider(self, provider: Any) -> Any:
        if provider is None or not isinstance(provider, str):
            return self._provider if provider is None else provider
        if _provider_name(self._provider) == provider:
            return self._provider
        llm = self._services.get("llm")
        factory = getattr(llm, "provider", None)
        if callable(factory):
            try:
                return factory(provider)
            # boundary: the llm plugin's provider() factory is plugin code
            except Exception as exc:
                raise SubagentError(f"unknown llm provider {provider!r}: {exc}") from exc
        raise SubagentError(f"unknown llm provider {provider!r}")

    def _facts_for(self, model: str) -> PromptFacts | None:
        if self._prompt_facts is not None:
            return self._prompt_facts
        parent_facts = _parent_prompt_facts(self._parent_host)
        if parent_facts is not None and self._cwd is not None:
            with contextlib.suppress(Exception):
                return replace(parent_facts, cwd=self._cwd, model=model)
        permission = self._permission
        mode = getattr(getattr(permission, "policy", None), "mode", None)
        approval = getattr(permission, "approval", None)
        if isinstance(mode, str) and isinstance(approval, str) and self._cwd is not None:
            return PromptFacts(
                cwd=self._cwd,
                model=model,
                permission_preset=mode,
                approval_policy=approval,
            )
        return None

    def _child_services(self) -> dict[str, Any]:
        services = dict(self._services)
        services.pop(PLUGIN, None)
        return services

    def _create_host(
        self,
        session: NativeSession,
        *,
        provider: str,
        model: str,
        preset: str,
        preset_revision: int | None,
    ) -> Any:
        if not provider:
            raise SubagentError("native subagent service requires a provider")
        if self._cwd is None:
            raise SubagentError("native subagent service is not configured")
        host_factory = getattr(self._agents, "host", None)
        if not callable(host_factory):
            raise SubagentError("agents service does not expose host()")
        build = AgentBuild(
            provider=provider,
            model=model,
            cwd=self._cwd,
            preset=preset,
            preset_revision=preset_revision,
            permission=self._permission,
            hooks=None,
            prompt_facts=self._facts_for(model),
            services=self._child_services(),
            context_window=self._context_window,
        )
        try:
            return host_factory(session, build)
        # boundary: the agents plugin builds the host and surfaces its own errors
        except Exception as exc:
            raise SubagentError(f"cannot create native subagent host: {exc}") from exc

    def _measure_usage(
        self,
        activation: SubagentActivation,
        host: Any,
        session: NativeSession,
        started: float,
        *,
        steps: int,
    ) -> SubagentUsage:
        input_tokens, output_tokens, cached_tokens = _host_usage(host)
        baseline = self._baselines.get(activation.child_id, 0)
        measured_steps = steps if steps > 0 else max(0, _assistant_count(session) - baseline)
        return SubagentUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            model=activation.model,
            provider=activation.provider,
            steps=measured_steps,
        )

    def _publish_usage(self, activation: SubagentActivation, usage: SubagentUsage) -> None:
        self._usages[activation.child_id] = usage
        sink = self._usage_sink if self._usage_sink is not None else self._services.get("usage")
        record = getattr(sink, "record", None)
        if not callable(record):
            return
        if not any((usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.retries)):
            return
        try:
            record(
                provider=usage.provider,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
            )
        # boundary: the usage sink is an optional decoupled Protocol implementation
        except Exception:
            logger.warning("failed to report subagent usage", exc_info=True)

    def _append_fact(self, event_type: str, payload: dict[str, Any]) -> None:
        session = self._parent_session
        if session is None:
            return
        try:
            session.append(event_type, payload)
        # boundary: the parent session may close while a child settles
        except Exception:
            logger.warning("failed to append %s event", event_type, exc_info=True)

    def _settle(self, child_id: str, *, status: str, result: SubagentResult | None) -> None:
        if child_id in self._settled:
            return
        self._settled.add(child_id)
        activation = self._activations.get(child_id)
        if activation is None:
            return
        payload: dict[str, Any] = {
            "version": 1,
            "child_session_id": child_id,
            "status": status,
            "is_error": bool(result.is_error) if result is not None else False,
            "usage_known": result is not None and result.usage is not None,
        }
        if result is not None:
            if result.error:
                payload["error"] = result.error
            if result.text:
                payload["text"] = result.text
        self._append_fact(FINISHED_EVENT, payload)

    async def _notify_parent(self, result: SubagentResult) -> None:
        summary, notice = _notice_parts(result)
        state = await self._deliver_notice(result.child_id, summary, notice)
        self._append_fact(
            NOTICE_EVENT,
            {
                "version": 1,
                "child_session_id": result.child_id,
                "state": state,
                "summary": summary,
            },
        )

    async def _deliver_notice(self, child_id: str, summary: str, notice: str) -> str:
        host = self._parent_host
        deliver = getattr(host, "deliver", None) if host is not None else None
        try:
            if callable(deliver):
                queued = deliver(notice, source=PluginSource(plugin=PLUGIN))
                if inspect.isawaitable(queued):
                    queued = await queued
                return "queued" if queued else "delivered"
            inject = getattr(host, "inject", None) if host is not None else None
            if callable(inject):
                accepted = inject(notice, {"plugin": PLUGIN})
                if inspect.isawaitable(accepted):
                    accepted = await accepted
                return "queued" if accepted else "dropped"
        # boundary: settlement delivery is best effort across duck-typed parent hosts
        except Exception:
            logger.warning(
                "failed to deliver settlement notice for subagent %s",
                child_id,
                exc_info=True,
            )
            return "dropped"
        logger.warning(
            "dropping settlement notice for subagent %s: no parent agent is bound",
            child_id,
        )
        return "dropped"

    def _result_for(
        self,
        activation: SubagentActivation,
        *,
        text: str = "",
        is_error: bool = False,
        error: str = "",
    ) -> SubagentResult:
        return SubagentResult(
            child_id=activation.child_id,
            session_id=activation.session_id,
            label=activation.label,
            provider=activation.provider,
            model=activation.model,
            text=text,
            is_error=is_error,
            error=error,
            mode=activation.mode,
        )

    def _next_label(self, name: str | None) -> str:
        explicit = _text(name)
        if explicit:
            return explicit
        self._labels += 1
        return f"subagent-{self._labels}"

    def _release(self, child_id: str) -> None:
        if child_id in self._active_ids:
            self._active_ids.discard(child_id)
            self._live = max(0, self._live - 1)

    def _on_task_done(self, child_id: str, done: asyncio.Task[SubagentResult]) -> None:
        self._release(child_id)


__all__ = ["NativeSubagentService"]
