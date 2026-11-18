"""Default subagent service: one-shot and forked child agents.

A spawn runs one child agent to completion for a self-contained task.  A fork
starts from the parent session's last completed turn boundary and runs in the
background: the tool returns an activation immediately and the parent receives a
settlement notice when the child finishes.  Both modes keep the same child
wiring conventions (a seeded delegationDepth 1 session with origin "subagent", a
private sink, and a tool registry without orchestration tools), and both can be
awaited through run()/wait_children().
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from pathlib import Path
from typing import Any

from deeppyro.api.types import CHILD_DISABLED_TOOLS, DEFAULT_PRESET_ID
from deeppyro.core.ids import new_session_id, now_ms
from deeppyro.core.protocols import LlmProvider
from deeppyro.core.wire import SessionHeader
from deeppyro.session.fork import build_fork_seed
from deeppyro.session.layout import flat_session_path
from deeppyro.session.session import Session
from deeppyro.session.writer import write_new_session
from deeppyro.subagent.errors import SubagentError
from deeppyro.subagent.types import (
    DEFAULT_CONTEXT_WINDOW,
    MAX_LIVE_RUNS,
    USAGE_RECORD_TYPE,
    USAGE_VERSION,
    SubagentActivation,
    SubagentResult,
    SubagentUsage,
    sum_usages,
    usage_from_records,
)

_ORPHAN = ""
_ACTIVATION_MODES = ("spawn", "fork")
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


def _provider_name(provider: LlmProvider | None) -> str:
    if provider is None:
        return _ORPHAN
    name = provider.name
    if isinstance(name, str):
        return name
    return str(name or "")


def _activation_mode(mode: Any) -> str:
    if not isinstance(mode, str) or mode not in _ACTIVATION_MODES:
        raise SubagentError(f"unsupported subagent mode: {mode!r}")
    return mode


def filter_child_tools(registry: Any) -> Any:
    """Return a tool registry without the orchestration tools a child must not receive."""
    without = getattr(registry, "without", None)
    if callable(without):
        return without(CHILD_DISABLED_TOOLS)
    # Local fallback while ToolRegistry.without is not available yet.  The
    # import stays local so the module does not depend on deeppyro.tools being
    # initialized first (tools.builtin imports the subagent tool at runtime).
    from deeppyro.tools.registry import ToolRegistry

    return ToolRegistry([tool for tool in registry if getattr(tool, "name", "") not in CHILD_DISABLED_TOOLS])


class _ChildSink:
    """Swallow child agent stream events: the caller only receives the final reply."""

    def emit(self, event: Any) -> None:
        pass


class SubagentService:
    """One-shot and background child agents for a single configured parent session."""

    def __init__(
        self,
        *,
        agents: Any = None,
        sessions: Any = None,
        sandbox: Any = None,
        max_runs: int = MAX_LIVE_RUNS,
    ) -> None:
        self._agents = agents
        self._sessions = sessions
        self._sandbox = sandbox
        self._max_runs = int(max_runs)
        self._cwd: Path | None = None
        self._provider: Any = None
        self._registry: Any = None
        self._permission: Any = None
        self._model: str = ""
        self._context_window: int = DEFAULT_CONTEXT_WINDOW
        self._preset: str = DEFAULT_PRESET_ID
        self._preset_revision: int | None = None
        self._services: dict[str, Any] = {}
        self._ask: Any = None
        self._parent: Session | None = None
        self._parent_agent: Any = None
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._child_agents: dict[str, Any] = {}
        self._child_sessions: dict[str, Session] = {}
        self._activations: dict[str, SubagentActivation] = {}
        self._active_ids: set[str] = set()
        self._usages: dict[str, SubagentUsage] = {}
        self._baselines: dict[str, SubagentUsage] = {}
        self._usage_sink: Any = None
        self._live = 0
        self._labels = 0
        self._closed = False

    # -- configuration --------------------------------------------------
    @property
    def max_runs(self) -> int:
        return self._max_runs

    @property
    def live_runs(self) -> int:
        return self._live

    @property
    def parent_session_id(self) -> str:
        parent = self._parent
        if parent is None:
            return _ORPHAN
        session_id = parent.session_id
        if session_id:
            return session_id
        header_id = parent.header.id
        return header_id if isinstance(header_id, str) else _ORPHAN

    def configure(
        self,
        *,
        cwd: Any,
        provider: Any,
        registry: Any,
        permission: Any,
        model: str,
        context_window: int,
        preset: str = DEFAULT_PRESET_ID,
        preset_revision: int | None = None,
        services: dict[str, Any] | None = None,
        ask: Any = None,
        usage_sink: Any = None,
    ) -> None:
        """Attach the live parent-session context used to spawn children."""
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

    def bind_session(self, session: Session | None) -> None:
        """Bind the parent session that receives subagent catalog/descriptor records."""
        self._parent = session
        self._parent_agent = None

    def bind_parent(self, agent: Any) -> None:
        """Bind the parent agent that receives background settlement notices."""
        self._parent_agent = agent

    def set_ask(self, ask: Any) -> None:
        """Set the approval callback used by the child permission gate."""
        self._ask = ask

    # -- runs -----------------------------------------------------------
    async def run(
        self,
        prompt: str,
        *,
        description: str,
        name: str | None = None,
        provider: Any = None,
        model: str | None = None,
        preset: str | None = None,
    ) -> SubagentResult:
        """Run one child agent to completion and return its final reply."""
        activation = await self._start(
            prompt,
            description=description,
            name=name,
            provider=provider,
            model=model,
            preset=preset,
            mode="spawn",
            notify=False,
        )
        task = self._tasks.get(activation.child_id)
        if task is None:
            raise SubagentError("subagent background task disappeared")
        try:
            result = await task
        finally:
            self._tasks.pop(activation.child_id, None)
        return result

    async def start(
        self,
        prompt: str,
        *,
        description: str,
        name: str | None = None,
        provider: Any = None,
        model: str | None = None,
        preset: str | None = None,
        mode: str = "spawn",
    ) -> SubagentActivation:
        """Start a child agent in the background and return its activation immediately."""
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
        self._ensure_ready()
        mode = _activation_mode(mode)
        if self._live >= self._max_runs:
            raise SubagentError(f"at most {self._max_runs} concurrent subagent runs are allowed")
        if mode == "fork":
            if provider is not None or model is not None or preset is not None:
                raise SubagentError("fork does not support provider, model, or preset overrides")
            resolved_provider = self._provider
            preset_id = self._preset
            preset_revision = self._preset_revision
            model_id = self._model
            child_session, inherited_event_count = self._create_fork_session(preset_id)
        else:
            resolved_provider = self._resolve_provider(provider)
            preset_id = _text(preset) or self._preset
            preset_revision = self._preset_revision if preset_id == self._preset else None
            model_id = _text(model) or self._model
            child_session = self._create_child_session(preset_id)
            inherited_event_count = None
        provider_name = _provider_name(resolved_provider)
        child_id = child_session.session_id
        # a fork starts from seed records inherited from the parent; only records
        # appended during this run count towards the child's usage
        self._baselines[child_id] = usage_from_records(list(getattr(child_session, "records", ()) or ()))
        self._child_sessions[child_id] = child_session
        label = self._next_label(name)
        agent = self._create_agent(
            child_session,
            provider=resolved_provider,
            model=model_id,
            preset=preset_id,
            preset_revision=preset_revision,
        )
        self._record_subagent(
            child_id=child_id,
            child_session=child_session,
            child_created_at=int(child_session.header.createdAt),
            label=label,
            provider=provider_name,
            model=model_id,
            mode=mode,
            inherited_event_count=inherited_event_count,
        )
        activation = SubagentActivation(
            child_id=child_id,
            session_id=child_id,
            label=label,
            provider=provider_name,
            model=model_id,
            mode=mode,
            created_at=int(child_session.header.createdAt),
        )
        self._activations[child_id] = activation
        self._live += 1
        self._active_ids.add(child_id)
        self._child_agents[child_id] = agent
        task = asyncio.create_task(
            self._activate(text, agent, activation, notify=notify),
            name=f"subagent:{child_id}",
        )
        self._tasks[child_id] = task
        task.add_done_callback(lambda done, cid=child_id: self._on_task_done(cid, done))
        return activation

    async def _activate(
        self,
        prompt: str,
        agent: Any,
        activation: SubagentActivation,
        *,
        notify: bool,
    ) -> SubagentResult:
        started = time.perf_counter()
        result: SubagentResult | None = None
        try:
            try:
                reply = await agent.send(prompt)
            except asyncio.CancelledError:
                raise
            # boundary: agent.send() runs a full child turn with LLM, tools and hooks
            except Exception as exc:  # noqa: BLE001 - report a failed run in the result
                result = SubagentResult(
                    child_id=activation.child_id,
                    session_id=activation.session_id,
                    label=activation.label,
                    provider=activation.provider,
                    model=activation.model,
                    mode=activation.mode,
                    is_error=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                result = SubagentResult(
                    child_id=activation.child_id,
                    session_id=activation.session_id,
                    label=activation.label,
                    provider=activation.provider,
                    model=activation.model,
                    mode=activation.mode,
                    text=reply if isinstance(reply, str) else str(reply or ""),
                )
            usage = self._measure_usage(agent, activation, started)
            result.usage = usage
            self._publish_usage(activation, usage)
            if notify:
                await self._notify_parent(result)
            return result
        finally:
            if result is None:
                # a cancelled child still reports the usage it already spent
                with contextlib.suppress(Exception):
                    self._publish_usage(activation, self._measure_usage(agent, activation, started))
            self._release(activation.child_id)
            self._activations.pop(activation.child_id, None)
            self._baselines.pop(activation.child_id, None)
            self._child_sessions.pop(activation.child_id, None)
            agent = self._child_agents.pop(activation.child_id, None)
            await self._close_agent(agent)

    # -- usage ----------------------------------------------------------
    def usage(self, child_id: str | None = None) -> SubagentUsage | None:
        """Provider usage of one child run, or the aggregate over every finished run.

        Args:
            child_id: A child id from a SubagentActivation.  Omit to aggregate.

        Returns:
            The measured usage, or None when no run has finished yet.
        """
        if child_id is not None:
            return self._usages.get(child_id)
        if not self._usages:
            return None
        return sum_usages(self._usages.values())

    def _child_records(self, agent: Any, child_id: str) -> list[Any]:
        """The child session records, preferring the live session the agent appends to."""
        session = getattr(agent, "session", None)
        if session is None:
            session = self._child_sessions.get(child_id)
        records = getattr(session, "records", None)
        if records is None:
            store = getattr(self._sessions, "store", None)
            opener = getattr(store, "open", None)
            if callable(opener):
                try:
                    records = getattr(opener(child_id), "records", None)
                # boundary: the sessions store is a duck-typed plugin backend
                except Exception:  # noqa: BLE001
                    records = None
        return list(records or ())

    def _measure_usage(self, agent: Any, activation: SubagentActivation, started: float) -> SubagentUsage:
        """Fold the child session's provider records into usage; latency is measured here."""
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        try:
            records = self._child_records(agent, activation.child_id)
        # boundary: measuring usage must never mask the child's result
        except Exception:  # noqa: BLE001
            records = []
        return usage_from_records(
            records,
            model=activation.model,
            provider=activation.provider,
            latency_ms=latency_ms,
            baseline=self._baselines.get(activation.child_id),
        )

    def _publish_usage(self, activation: SubagentActivation, usage: SubagentUsage) -> None:
        """Remember one run's usage, journal it as a parent fact and offer it to the sink."""
        self._usages[activation.child_id] = usage
        self._append_record(
            USAGE_RECORD_TYPE,
            {
                "version": USAGE_VERSION,
                "childId": activation.child_id,
                "sessionId": activation.session_id,
                "mode": activation.mode,
                "usage": usage.to_dict(),
                "at": now_ms(),
            },
            ignorable=True,
        )
        self._report_usage(usage)

    def _report_usage(self, usage: SubagentUsage) -> None:
        sink = self._usage_sink
        if sink is None:
            sink = self._services.get("usage")
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

    def _publish_usage_fallback(self, child_id: str) -> None:
        """Record usage for a child cancelled before its run body ever executed."""
        activation = self._activations.pop(child_id, None)
        child_session = self._child_sessions.pop(child_id, None)
        baseline = self._baselines.pop(child_id, None)
        if activation is None or child_id in self._usages:
            return
        records = list(getattr(child_session, "records", ()) or ())
        usage = usage_from_records(
            records,
            model=activation.model,
            provider=activation.provider,
            baseline=baseline,
        )
        self._publish_usage(activation, usage)

    def _on_task_done(self, child_id: str, task: asyncio.Task[Any]) -> None:
        self._release(child_id)

    async def wait_children(self, timeout: float | None = None) -> None:
        """Wait for every background activation task; timeout does not cancel them."""
        current = asyncio.current_task()
        tasks = [task for task in self._tasks.values() if task is not current and not task.done()]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            raise SubagentError(f"timed out waiting for {len(pending)} background subagent(s)")
        await asyncio.gather(*done, return_exceptions=True)

    async def cancel_all(self) -> int:
        """Cancel every in-flight run and return how many were cancelled."""
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._tasks.values()
            if task is not None and task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for child_id, task in list(self._tasks.items()):
            if task not in tasks:
                continue
            self._release(child_id)
            agent = self._child_agents.pop(child_id, None)
            if agent is not None:
                await self._close_agent(agent)
            self._publish_usage_fallback(child_id)
            self._tasks.pop(child_id, None)
        return len(tasks)

    async def close(self) -> None:
        """Cancel in-flight runs and refuse new ones (used by runtime shutdown)."""
        self._closed = True
        await self.cancel_all()
        self._parent_agent = None

    # -- readiness ------------------------------------------------------
    def _ensure_ready(self) -> None:
        if self._closed:
            raise SubagentError("subagent service is closed")
        if self._agents is None or self._sessions is None:
            raise SubagentError("subagent service requires the agents and sessions services")
        if self._cwd is None:
            raise SubagentError("subagent service is not configured")
        if self._parent is None:
            raise SubagentError("subagent service has no bound parent session")
        if self._registry is None:
            raise SubagentError("subagent service requires a tool registry")

    def _next_label(self, name: Any) -> str:
        label = _text(name)
        if label:
            return label
        self._labels += 1
        return f"subagent-{self._labels}"

    def _release(self, child_id: str) -> None:
        if child_id in self._active_ids:
            self._active_ids.discard(child_id)
            self._live = max(0, self._live - 1)

    # -- child wiring ---------------------------------------------------
    def _create_child_session(self, preset_id: str) -> Session:
        store = getattr(self._sessions, "store", None)
        root = getattr(store, "root", None)
        if root is None:
            raise SubagentError("sessions service does not expose store.root")
        parent_id = self.parent_session_id
        if not parent_id:
            raise SubagentError("subagent service has no bound parent session")
        session_id = new_session_id()
        path = flat_session_path(Path(root), session_id)
        header = SessionHeader(
            id=session_id,
            createdAt=now_ms(),
            cwd=str(self._cwd),
            isSeeded=True,
            delegationDepth=1,
            agentPreset=preset_id,
            parentSession=parent_id,
            origin="subagent",
        )
        write_new_session(path, header, [])
        return Session(path=path, header=header, records=[])

    def _create_fork_session(self, preset_id: str) -> tuple[Session, int]:
        store = getattr(self._sessions, "store", None)
        root = getattr(store, "root", None)
        if root is None:
            raise SubagentError("sessions service does not expose store.root")
        create_seeded = getattr(store, "create_with_seed", None)
        if not callable(create_seeded):
            raise SubagentError("sessions store does not support seeded sessions")
        parent_id = self.parent_session_id
        if not parent_id:
            raise SubagentError("subagent service has no bound parent session")
        parent = self._parent
        if parent is None:
            raise SubagentError("subagent service has no bound parent session")
        records = list(parent.records or [])
        boundary: int | None = None
        for record in reversed(records):
            if record.type != "turn/end":
                continue
            seq = record.seq
            if isinstance(seq, int) and not isinstance(seq, bool):
                boundary = seq
                break
        if boundary is None:
            seed: list[dict[str, Any]] = []
            inherited_event_count = 0
            depth = 1
        else:
            seed = build_fork_seed(records[: boundary + 1], boundary)
            inherited_event_count = boundary + 1
            parent_depth = parent.header.delegationDepth
            depth = (parent_depth if isinstance(parent_depth, int) else 0) + 1
        header = SessionHeader(
            id=new_session_id(),
            createdAt=now_ms(),
            cwd=str(self._cwd),
            isSeeded=True,
            delegationDepth=depth,
            agentPreset=preset_id,
            parentSession=parent_id,
            origin="subagent",
        )
        try:
            session = create_seeded(header, seed)
        # boundary: the sessions store is a duck-typed plugin backend (create_with_seed)
        except Exception as exc:
            raise SubagentError(f"cannot create fork session: {exc}") from exc
        return session, inherited_event_count

    def _resolve_provider(self, provider: Any) -> Any:
        if provider is None or not isinstance(provider, str):
            return self._provider if provider is None else provider
        parent = self._provider
        if _provider_name(parent) == provider:
            return parent
        llm = self._services.get("llm")
        factory = getattr(llm, "provider", None)
        if callable(factory):
            try:
                return factory(provider)
            # boundary: the llm service's provider() factory is plugin code
            except Exception as exc:
                raise SubagentError(f"unknown llm provider {provider!r}: {exc}") from exc
        raise SubagentError(f"unknown llm provider {provider!r}")

    def _child_permission(self, session: Session) -> Any:
        """One gate per child session, inheriting the parent's current knobs."""
        sandbox = self._sandbox
        parent = self._permission
        if sandbox is None or not hasattr(sandbox, "gate"):
            return parent
        try:
            gate = sandbox.gate(self._cwd, ask=self._ask, session=session)
        except TypeError:
            try:
                gate = sandbox.gate(self._cwd, ask=self._ask)
            except TypeError:
                gate = sandbox.gate(self._cwd)
        _inherit_permission(sandbox, parent, session)
        return gate

    def _create_agent(
        self,
        child_session: Session,
        *,
        provider: Any,
        model: str,
        preset: str,
        preset_revision: int | None,
    ) -> Any:
        try:
            options = self._agents.options(model=model, context_window=self._context_window)
        # boundary: the agents service is a duck-typed plugin that builds the subagent's options
        except Exception as exc:
            raise SubagentError(f"cannot build subagent options: {exc}") from exc
        try:
            agent = self._agents.create(
                session=child_session,
                provider=provider,
                registry=filter_child_tools(self._registry),
                options=options,
                sink=_ChildSink(),
                permission=self._child_permission(child_session),
                cwd=str(self._cwd),
                services=dict(self._services),
                preset=preset,
                preset_revision=preset_revision,
            )
        # boundary: the agents plugin creates the subagent's agent and session
        except Exception as exc:
            raise SubagentError(f"cannot create subagent agent: {exc}") from exc
        return agent

    async def _close_agent(self, agent: Any) -> None:
        if agent is None:
            return
        aclose = getattr(agent, "aclose", None)
        if not callable(aclose):
            return
        # cleanup must not mask the run result
        with contextlib.suppress(Exception):
            result = aclose()
            if inspect.isawaitable(result):
                await result

    # -- records --------------------------------------------------------
    def _append_record(self, record_type: str, data: dict[str, Any], *, ignorable: bool = False) -> None:
        self._append_record_on(self._parent, record_type, data, ignorable=ignorable)

    def _append_record_on(
        self,
        session: Any,
        record_type: str,
        data: dict[str, Any],
        *,
        ignorable: bool = False,
    ) -> None:
        append = getattr(session, "append", None)
        if append is None:
            return
        envelope: dict[str, Any] = {"ignorable": True} if ignorable else {}
        try:
            append(record_type, data, **envelope)
        # boundary: the parent session is a duck-typed object; bookkeeping append failures are not fatal
        except Exception:
            logger.warning("failed to append %s record", record_type, exc_info=True)

    def _record_subagent(
        self,
        *,
        child_id: str,
        child_session: Session,
        child_created_at: int,
        label: str,
        provider: str,
        model: str,
        mode: str,
        inherited_event_count: int | None,
    ) -> None:
        self._append_record(
            "subagent/catalog",
            {
                "version": 1,
                "childId": child_id,
                "childCreatedAt": child_created_at,
                "mode": mode,
                "label": label,
            },
        )
        descriptor: dict[str, Any] = {
            "version": 1,
            "childId": child_id,
            "mode": mode,
            "provider": provider,
            "label": label,
            "agentProvider": provider,
            "agentModel": model,
        }
        if inherited_event_count is not None:
            descriptor["inheritedEventCount"] = int(inherited_event_count)
        self._append_record_on(child_session, "subagent/descriptor", dict(descriptor))
        self._append_record("subagent/descriptor", descriptor)

    # -- settlement -----------------------------------------------------
    async def _notify_parent(self, result: SubagentResult) -> None:
        if result.is_error:
            summary = f"Background subagent {result.child_id} failed before it finished."
        else:
            summary = (
                f"Background subagent {result.child_id} finished and will do no further work "
                "unless you send it more."
            )
        closing = result.text.strip()
        if closing:
            notice = f"{summary}\nIts closing message:\n{closing}"
        else:
            notice = f"{summary}\nIt left no closing message."
        parent_agent = self._parent_agent
        if parent_agent is None:
            logger.warning(
                "dropping settlement notice for subagent %s: no parent agent is bound",
                result.child_id,
            )
            return
        source = {
            "kind": "subagent-settled",
            "form": "notice",
            "summary": summary,
            "senderSessionId": result.child_id,
        }
        followup = getattr(parent_agent, "followup", None)
        if not callable(followup):
            logger.warning(
                "dropping settlement notice for subagent %s: parent agent has no followup()",
                result.child_id,
            )
            return
        try:
            outcome = followup(notice, source=source)
            if inspect.isawaitable(outcome):
                await outcome
        # boundary: parent_agent.followup() is a duck-typed agent handle; the notice is best effort
        except Exception:
            logger.warning(
                "failed to deliver settlement notice for subagent %s",
                result.child_id,
                exc_info=True,
            )


def _inherit_permission(sandbox: Any, parent: Any, session: Any) -> None:
    """Seed a child session with the parent's standing sandbox and approval knobs."""
    policy_service = getattr(sandbox, "policy_service", None)
    mode = getattr(getattr(parent, "policy", None), "mode", None)
    if policy_service is not None and isinstance(mode, str) and mode:
        # seeding must not break a spawn
        with contextlib.suppress(Exception):
            policy_service.set_mode(session, mode)
    approval_service = getattr(sandbox, "approval_service", None)
    approval = getattr(parent, "approval", None)
    if approval_service is not None and isinstance(approval, str) and approval:
        # seeding must not break a spawn
        with contextlib.suppress(Exception):
            approval_service.set_policy(session, approval)


__all__ = ["SubagentService", "filter_child_tools"]
