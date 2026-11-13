"""Default subagent service: spawn one child agent for one self-contained task.

The service mirrors the team layer's child session conventions (a seeded
delegationDepth 1 session with origin "subagent") but keeps no background
member: a run is a single await that returns the child's final reply.  The
child agent is wired with a private sink and a tool registry that excludes
every orchestration tool, so child agents cannot spawn further agents or team
members.  Everything that touches the kernel is injected, which keeps the
service unit-testable without a provider or a kernel boot.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path
from typing import Any

from deeppyro.api.types import CHILD_DISABLED_TOOLS, DEFAULT_PRESET_ID
from deeppyro.core.ids import new_session_id, now_ms
from deeppyro.core.wire import SessionHeader
from deeppyro.session.layout import flat_session_path
from deeppyro.session.session import Session
from deeppyro.session.writer import write_new_session
from deeppyro.subagent.errors import SubagentError
from deeppyro.subagent.types import DEFAULT_CONTEXT_WINDOW, MAX_LIVE_RUNS, SubagentResult

_ORPHAN = ""


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
    if provider is None:
        return _ORPHAN
    name = getattr(provider, "name", None)
    if isinstance(name, str):
        return name
    return str(name or "")


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
    """One-shot child agents for a single configured parent session."""

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
        self._parent: Any = None
        self._tasks: dict[str, asyncio.Task[Any]] = {}
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
        session_id = getattr(parent, "session_id", "")
        if isinstance(session_id, str) and session_id:
            return session_id
        header = getattr(parent, "header", None)
        header_id = getattr(header, "id", "")
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

    def bind_session(self, session: Any) -> None:
        """Bind the parent session that receives subagent catalog/descriptor records."""
        self._parent = session

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
        text = _required_text(prompt, "prompt")
        self._ensure_ready()
        if self._live >= self._max_runs:
            raise SubagentError(f"at most {self._max_runs} concurrent subagent runs are allowed")
        resolved_provider = self._resolve_provider(provider)
        provider_name = _provider_name(resolved_provider)
        child_id = f"subagent-{uuid.uuid4().hex[:12]}"
        label = _text(name)
        if not label:
            self._labels += 1
            label = f"subagent-{self._labels}"
        preset_id = _text(preset) or self._preset
        preset_revision = self._preset_revision if preset_id == self._preset else None
        model_id = _text(model) or self._model
        current = asyncio.current_task()
        if current is not None:
            self._tasks[child_id] = current
        self._live += 1
        agent: Any = None
        try:
            child_session = self._create_child_session(preset_id)
            agent = self._create_agent(
                child_session,
                provider=resolved_provider,
                model=model_id,
                preset=preset_id,
                preset_revision=preset_revision,
            )
            self._record_subagent(
                child_id=child_id,
                child_created_at=int(child_session.header.createdAt),
                label=label,
                provider=provider_name,
                model=model_id,
            )
            try:
                reply = await agent.send(text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - report a failed run in the result
                return SubagentResult(
                    child_id=child_id,
                    session_id=child_session.session_id,
                    label=label,
                    provider=provider_name,
                    model=model_id,
                    is_error=True,
                    error=f"{type(exc).__name__}: {exc}",
                )
            return SubagentResult(
                child_id=child_id,
                session_id=child_session.session_id,
                label=label,
                provider=provider_name,
                model=model_id,
                text=reply if isinstance(reply, str) else str(reply or ""),
            )
        finally:
            self._live -= 1
            self._tasks.pop(child_id, None)
            await self._close_agent(agent)

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
        return len(tasks)

    async def close(self) -> None:
        """Cancel in-flight runs and refuse new ones (used by runtime shutdown)."""
        self._closed = True
        await self.cancel_all()

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
            except Exception as exc:
                raise SubagentError(f"unknown llm provider {provider!r}: {exc}") from exc
        raise SubagentError(f"unknown llm provider {provider!r}")

    def _child_permission(self) -> Any:
        sandbox = self._sandbox
        parent = self._permission
        if sandbox is None or not hasattr(sandbox, "gate"):
            return parent
        try:
            gate = sandbox.gate(self._cwd, ask=self._ask)
        except TypeError:
            gate = sandbox.gate(self._cwd)
        preset = getattr(parent, "preset", None)
        approval = getattr(parent, "approval", None)
        if preset is not None and hasattr(gate, "set_preset"):
            gate.set_preset(preset)
        if approval is not None and hasattr(gate, "set_approval"):
            gate.set_approval(approval)
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
        except Exception as exc:
            raise SubagentError(f"cannot build subagent options: {exc}") from exc
        try:
            agent = self._agents.create(
                session=child_session,
                provider=provider,
                registry=filter_child_tools(self._registry),
                options=options,
                sink=_ChildSink(),
                permission=self._child_permission(),
                cwd=str(self._cwd),
                services=dict(self._services),
                preset=preset,
                preset_revision=preset_revision,
            )
        except Exception as exc:
            raise SubagentError(f"cannot create subagent agent: {exc}") from exc
        return agent

    async def _close_agent(self, agent: Any) -> None:
        if agent is None:
            return
        aclose = getattr(agent, "aclose", None)
        if not callable(aclose):
            return
        try:
            result = aclose()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001, S110 - cleanup must not mask the run result
            pass

    # -- records --------------------------------------------------------
    def _append_record(self, record_type: str, data: dict[str, Any]) -> None:
        parent = self._parent
        append = getattr(parent, "append", None)
        if append is None:
            return
        try:
            append(record_type, data)
        except Exception:  # noqa: BLE001, S110 - logging must not break a run
            pass

    def _record_subagent(
        self,
        *,
        child_id: str,
        child_created_at: int,
        label: str,
        provider: str,
        model: str,
    ) -> None:
        self._append_record(
            "subagent/catalog",
            {
                "version": 1,
                "childId": child_id,
                "childCreatedAt": child_created_at,
                "mode": "spawn",
                "label": label,
            },
        )
        self._append_record(
            "subagent/descriptor",
            {
                "version": 1,
                "childId": child_id,
                "mode": "spawn",
                "provider": provider,
                "label": label,
                "agentProvider": provider,
                "agentModel": model,
            },
        )


__all__ = ["SubagentService", "filter_child_tools"]
