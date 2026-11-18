"""Agent team service: subagent members, messaging and the shared task board.

The team service is owned by the lead session.  It spawns one child session per
member (delegationDepth 1, origin "subagent"), runs a private AgentSession for
it, forwards messages, and records DSH compatible team/subagent events on the
parent session log.  Everything that touches the kernel is injected, so the
service is unit-testable without a provider or a kernel boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.api import CHILD_DISABLED_TOOLS, DEFAULT_PRESET_ID
from deeppyro.core.ids import new_session_id, now_ms
from deeppyro.core.protocols import LlmProvider
from deeppyro.core.wire import LogRecord, SessionHeader
from deeppyro.session.fork import open_turn_closers
from deeppyro.session.layout import flat_session_path
from deeppyro.session.session import Session
from deeppyro.session.writer import write_new_session
from deeppyro.subagent.types import SubagentUsage, sum_usages, usage_from_records
from deeppyro.team.errors import TeamError
from deeppyro.team.types import (
    LIVE_PHASES,
    MAX_MEMBERS,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_RUNNING,
    PHASE_SPAWNING,
    SETTLED_PHASES,
    TASK_ACTIONS,
    MemberInfo,
    MemberRun,
    TaskInfo,
)
from deeppyro.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_DEFAULT_PRESET = DEFAULT_PRESET_ID
_DEFAULT_CONTEXT_WINDOW = 32768
_DEFAULT_OWNER = "lead"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TeamError("expected a string value")
    return value.strip()


def _required_text(value: Any, field_name: str) -> str:
    text = _text(value)
    if not text:
        raise TeamError(f"{field_name} must be a non-empty string")
    return text


def _string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise TeamError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in items:
        text = _required_text(item, field_name)
        result.append(text)
    return result


@dataclass(slots=True)
class _Member:
    id: str
    name: str
    description: str
    provider: str
    model: str
    preset: str
    session_id: str
    created_at: int
    phase: str = PHASE_SPAWNING
    last_reply: str = ""
    error: str | None = None
    agent: Any = None
    restored_session: Any = None
    session: Any = None
    task: asyncio.Task[None] | None = None
    queue: list[str] = field(default_factory=list)
    rest: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    baseline: SubagentUsage | None = None


class _MemberSink:
    """Swallow child agent stream events: members are inspected through the team API."""

    def emit(self, event: Any) -> None:
        pass


def _member_payload(member: _Member) -> dict[str, Any]:
    return {
        "id": member.id,
        "name": member.name,
        "description": member.description,
        "provider": member.provider,
        "context": member.preset,
        "phase": member.phase,
        "model": member.model,
    }


def _member_info(member: _Member) -> MemberInfo:
    return MemberInfo(
        id=member.id,
        name=member.name,
        description=member.description,
        provider=member.provider,
        model=member.model,
        preset=member.preset,
        phase=member.phase,
        session_id=member.session_id,
        last_reply=member.last_reply,
        error=member.error,
        created_at=member.created_at,
    )


class TeamService:
    """Lead-owned team: up to eight child agents plus a shared task board."""

    def __init__(
        self,
        *,
        agents: Any = None,
        sessions: Any = None,
        sandbox: Any = None,
        team_id: str = "",
        max_members: int = MAX_MEMBERS,
    ) -> None:
        self._agents = agents
        self._sessions = sessions
        self._sandbox = sandbox
        self._explicit_team_id = team_id
        self._max_members = int(max_members)
        self._cwd: Path | None = None
        self._provider: Any = None
        self._registry: Any = None
        self._permission: Any = None
        self._model: str = ""
        self._context_window: int = _DEFAULT_CONTEXT_WINDOW
        self._preset: str = _DEFAULT_PRESET
        self._preset_revision: int | None = None
        self._services: dict[str, Any] = {}
        self._ask: Any = None
        self._parent: Session | None = None
        self._members: dict[str, _Member] = {}
        self._board: dict[str, TaskInfo] = {}
        self._runs: dict[str, MemberRun] = {}
        self._usage_sink: Any = None
        self._restored = False

    # -- configuration --------------------------------------------------
    @property
    def team_id(self) -> str:
        if self._explicit_team_id:
            return self._explicit_team_id
        parent_id = self.parent_session_id
        return parent_id or "team"

    @property
    def parent_session_id(self) -> str:
        parent = self._parent
        if parent is None:
            return ""
        session_id = parent.session_id
        if session_id:
            return session_id
        header_id = parent.header.id
        return header_id if isinstance(header_id, str) else ""

    @property
    def max_members(self) -> int:
        return self._max_members

    def configure(
        self,
        *,
        cwd: Any,
        provider: Any,
        registry: Any,
        permission: Any,
        model: str,
        context_window: int,
        preset: str = _DEFAULT_PRESET,
        preset_revision: int | None = None,
        services: dict[str, Any] | None = None,
        ask: Any = None,
        usage_sink: Any = None,
    ) -> None:
        """Attach the live lead-session context used to spawn members.

        usage_sink is any object with a UsageSink style record() method; the
        team never imports the concrete usage service, it only reports to the
        optional sink the host wires in.
        """
        self._cwd = Path(cwd)
        self._provider = provider
        self._registry = registry
        self._permission = permission
        self._model = _text(model) or ""
        self._context_window = int(context_window)
        self._preset = _text(preset) or _DEFAULT_PRESET
        self._preset_revision = preset_revision
        self._services = dict(services or {})
        self._ask = ask
        if usage_sink is not None:
            self._usage_sink = usage_sink

    def bind_session(self, session: Session) -> None:
        """Bind the lead session that receives this team's records."""
        self._parent = session
        self._restored = False

    def set_ask(self, ask: Any) -> None:
        """Set the approval callback used by child permission gates."""
        self._ask = ask

    # -- members --------------------------------------------------------
    def spawn(
        self,
        name: str,
        description: str,
        prompt: str,
        *,
        provider: Any = None,
        model: str | None = None,
        preset: str | None = None,
    ) -> MemberInfo:
        """Create one child session/agent and start its first turn in the background."""
        member_name = _required_text(name, "name")
        member_description = _text(description)
        first_prompt = _required_text(prompt, "prompt")
        live = [item for item in self._members.values() if item.phase in LIVE_PHASES]
        if len(live) >= self._max_members:
            raise TeamError(f"team is full: at most {self._max_members} live members are allowed")
        if any(item.name == member_name for item in live):
            raise TeamError(f"team member name {member_name!r} is already in use")
        if self._parent is None:
            raise TeamError("team service has no bound parent session")
        if self._agents is None or self._sessions is None:
            raise TeamError("team service requires the agents and sessions services")
        if self._cwd is None:
            raise TeamError("team service is not configured")
        preset_id = _text(preset) or self._preset or _DEFAULT_PRESET
        preset_revision = self._preset_revision if preset_id == self._preset else None
        child_session = self._create_child_session(preset_id)
        agent = self._create_agent(
            child_session,
            provider=provider,
            model=_text(model) or self._model,
            preset=preset_id,
            preset_revision=preset_revision,
        )
        member = _Member(
            id=child_session.session_id,
            name=member_name,
            description=member_description,
            provider=_provider_name(self._resolve_provider(provider)),
            model=_text(model) or self._model,
            preset=preset_id,
            session_id=child_session.session_id,
            created_at=now_ms(),
            agent=agent,
            session=child_session,
        )
        self._members[member.id] = member
        self._record_subagent(member)
        self._record_member(member)
        try:
            member.task = asyncio.create_task(self._run(member, first_prompt, initial=True))
        except RuntimeError as exc:
            self._members.pop(member.id, None)
            raise TeamError("team members require a running event loop") from exc
        return _member_info(member)

    def members(self) -> list[MemberInfo]:
        return [_member_info(item) for item in self._members.values()]

    def get_member(self, member_id: Any) -> MemberInfo | None:
        member = self._members.get(str(member_id))
        return None if member is None else _member_info(member)

    def _require_member(self, member_id: Any) -> _Member:
        member = self._members.get(str(member_id))
        if member is None:
            raise TeamError(f"unknown team member {member_id!r}")
        return member

    def send(self, member_id: Any, message: Any) -> str:
        """Deliver a message: return "started" for a new turn or "queued" while busy."""
        member = self._require_member(member_id)
        text = _required_text(message, "message")
        if member.phase in (PHASE_FAILED, PHASE_CANCELLED):
            raise TeamError(f"team member {member.id!r} is {member.phase} and cannot receive messages")
        if member.phase in (PHASE_SPAWNING, PHASE_RUNNING):
            member.queue.append(text)
            self._record_message(member, "team/message/queued", text)
            return "queued"
        if member.agent is None:
            try:
                self._load_agent(member)
            # boundary: agents are built by the duck-typed agents plugin; a failed member is recorded, not raised
            except Exception as exc:
                member.error = f"{type(exc).__name__}: {exc}"
                member.phase = PHASE_FAILED
                self._record_member(member)
                raise TeamError(f"cannot restore team member {member.id!r}: {exc}") from exc
        member.rest.clear()
        member.phase = PHASE_RUNNING
        self._record_member(member)
        try:
            member.task = asyncio.create_task(self._run(member, text, initial=False))
        except RuntimeError as exc:
            raise TeamError("team members require a running event loop") from exc
        return "started"

    async def wait(self, member_id: Any, timeout_s: float | None = None) -> MemberInfo:
        """Wait until the member is idle/completed/failed/cancelled."""
        member = self._require_member(member_id)
        if member.phase not in SETTLED_PHASES:
            try:
                if timeout_s is None:
                    await member.rest.wait()
                else:
                    await asyncio.wait_for(member.rest.wait(), max(0.0, float(timeout_s)))
            except TimeoutError as exc:
                raise TeamError(f"timed out waiting for team member {member.id!r}") from exc
        return _member_info(member)

    async def cancel(self, member_id: Any) -> bool:
        """Interrupt a member, drop queued messages and settle it as cancelled."""
        member = self._members.get(str(member_id))
        if member is None:
            return False
        if member.phase in (PHASE_CANCELLED, PHASE_FAILED):
            return False
        member.cancelled = True
        member.queue.clear()
        member.phase = PHASE_CANCELLED
        self._record_member(member)
        member.rest.set()
        task = member.task
        if task is not None and not task.done():
            task.cancel()
        interrupt = getattr(member.agent, "interrupt", None)
        if interrupt is not None:
            # cancellation must not raise
            with contextlib.suppress(Exception):
                result = interrupt()
                if inspect.isawaitable(result):
                    await result
        return True

    async def close(self) -> None:
        """Cancel running members and close their agents (runtime shutdown)."""
        tasks = [
            member.task
            for member in self._members.values()
            if member.task is not None and not member.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for member in self._members.values():
            await self._close_agent(member.agent)

    # -- cold restore ---------------------------------------------------
    async def restore(self) -> tuple[MemberInfo, ...]:
        """Rebuild the roster, task board and inbox from the lead session log.

        A fresh process binds the lead session and calls restore() once: the
        persistent records are folded back into memory, then every member with a
        queued message resumes it in the background.  Repeated calls return the
        current roster instead of replaying the same records twice.
        """
        if self._restored:
            return tuple(_member_info(item) for item in self._members.values())
        parent = self._parent
        records = getattr(parent, "records", None) if parent is not None else None
        if not records:
            return ()
        self._restored = True
        order, members, queues, tasks = _fold_team_records(records, self.team_id)
        self._members.clear()
        self._board.clear()
        for member_id in order:
            member = self._restore_member(member_id, members[member_id])
            member.queue = queues.get(member_id, [])
            self._members[member_id] = member
        for task_id, payload in tasks.items():
            self._board[task_id] = _task_from_record(task_id, payload)
        self._restore_runs(records)
        for member in self._members.values():
            self._resume_member(member)
        return tuple(_member_info(item) for item in self._members.values())

    def _restore_member(self, member_id: str, payload: dict[str, Any]) -> _Member:
        phase = _safe_text(payload.get("phase")) or PHASE_IDLE
        if phase in (PHASE_SPAWNING, PHASE_RUNNING):
            phase = PHASE_IDLE
        member = _Member(
            id=member_id,
            name=_safe_text(payload.get("name")),
            description=_safe_text(payload.get("description")),
            provider=_safe_text(payload.get("provider")),
            model=_safe_text(payload.get("model")),
            preset=_safe_text(payload.get("context")) or self._preset,
            session_id=member_id,
            created_at=_safe_int(payload.get("createdAt")),
            phase=phase,
        )
        try:
            member.restored_session = self._open_member_session(member)
            member.session = member.restored_session
            member.baseline = self._cumulative_usage(member)
        # boundary: the session store is the plugin sessions backend; one lost member must not break restore
        except Exception as exc:  # noqa: BLE001 - one lost session must not break restore
            member.error = f"cannot restore team member session: {exc}"
            member.phase = PHASE_FAILED
        return member

    def _restore_runs(self, records: Iterable[LogRecord]) -> None:
        self._runs.clear()
        for run_id, payload in _fold_team_usage(records, self.team_id).items():
            try:
                self._runs[run_id] = MemberRun.from_dict(payload)
            except (TypeError, ValueError):
                logger.warning("skipping malformed team usage record %s", run_id)

    def _resume_member(self, member: _Member) -> None:
        if not member.queue or member.phase in (PHASE_FAILED, PHASE_CANCELLED):
            return
        text = member.queue.pop(0)
        member.phase = PHASE_RUNNING
        try:
            self._load_agent(member)
            member.task = asyncio.create_task(self._run(member, text, initial=False))
        # boundary: _load_agent and task creation touch the plugin agents service; resume is best effort
        except Exception as exc:  # noqa: BLE001 - resume is best effort
            member.queue.insert(0, text)
            member.error = f"{type(exc).__name__}: {exc}"
            member.phase = PHASE_FAILED
            self._record_member(member)

    def _load_agent(self, member: _Member) -> Any:
        if member.agent is not None:
            return member.agent
        session = member.restored_session
        if session is None:
            session = self._open_member_session(member)
            member.restored_session = session
        member.session = session
        self._close_open_turn(session)
        preset = member.preset or self._preset
        member.agent = self._create_agent(
            session,
            provider=member.provider or None,
            model=member.model,
            preset=preset,
            preset_revision=self._preset_revision if preset == self._preset else None,
        )
        return member.agent

    def _open_member_session(self, member: _Member) -> Session:
        store = getattr(self._sessions, "store", None)
        opener = getattr(store, "open", None)
        if not callable(opener):
            raise TeamError("sessions service store does not support opening sessions")
        return opener(member.session_id)

    def _close_open_turn(self, session: Session) -> None:
        records = session.records
        try:
            closers = open_turn_closers(records, {"kind": "interrupted"})
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.warning("failed to build team member turn closers", exc_info=True)
            return
        for closer in closers:
            try:
                envelope: dict[str, Any] = {}
                if closer.get("surfaceOp") is not None:
                    envelope["surfaceOp"] = closer["surfaceOp"]
                if closer.get("sourceEventSeqs") is not None:
                    envelope["sourceEventSeqs"] = closer["sourceEventSeqs"]
                if closer.get("time") is not None:
                    envelope["time"] = closer["time"]
                session.append(closer["type"], closer["data"], **envelope)
            # boundary: sessions come from the plugin store backend; append failures are best effort
            except Exception:
                logger.warning("failed to close team member open turn", exc_info=True)
                return

    async def _close_agent(self, agent: Any) -> None:
        if agent is None:
            return
        aclose = getattr(agent, "aclose", None)
        if not callable(aclose):
            return
        # shutdown must not raise
        with contextlib.suppress(Exception):
            result = aclose()
            if inspect.isawaitable(result):
                await result

    # -- child wiring ---------------------------------------------------
    def _create_child_session(self, preset_id: str) -> Session:
        store = getattr(self._sessions, "store", None)
        root = getattr(store, "root", None)
        if root is None:
            raise TeamError("sessions service does not expose store.root")
        parent_id = self.parent_session_id
        if not parent_id:
            raise TeamError("team service has no bound parent session")
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
            # boundary: the llm service's provider() factory is plugin code
            except Exception as exc:
                raise TeamError(f"unknown llm provider {provider!r}: {exc}") from exc
        raise TeamError(f"unknown llm provider {provider!r}")

    def _child_registry(self) -> Any:
        """Return the tool registry a member agent may use.

        Members must not delegate further: both the team tools and the one-shot
        `subagent` tool are removed from the registry handed to their agent.
        Opaque registry doubles without a `without` method or iterable tools are
        passed through unchanged.
        """
        registry = self._registry
        if registry is None:
            return None
        without = getattr(registry, "without", None)
        if callable(without):
            return without(CHILD_DISABLED_TOOLS)
        try:
            tools = list(registry)
        except TypeError:
            return registry
        if not all(getattr(tool, "name", None) for tool in tools):
            return registry
        return ToolRegistry(tool for tool in tools if tool.name not in CHILD_DISABLED_TOOLS)

    def _child_permission(self, session: Session) -> Any:
        """One gate per member session, inheriting the lead's current knobs.

        DSH keeps sandbox mode and approval policy per session, so a member
        gets its own log; it starts from the lead's standing values instead of
        the host defaults.
        """
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
        resolved = self._resolve_provider(provider)
        try:
            options = self._agents.options(model=model, context_window=self._context_window)
        # boundary: the agents service is a duck-typed plugin that builds the member's options
        except Exception as exc:
            raise TeamError(f"cannot build member agent options: {exc}") from exc
        try:
            return self._agents.create(
                session=child_session,
                provider=resolved,
                registry=self._child_registry(),
                options=options,
                sink=_MemberSink(),
                permission=self._child_permission(child_session),
                cwd=str(self._cwd),
                services=dict(self._services),
                preset=preset,
                preset_revision=preset_revision,
            )
        # boundary: the agents plugin creates the member's agent and session
        except Exception as exc:
            raise TeamError(f"cannot create member agent: {exc}") from exc

    # -- member runtime -------------------------------------------------
    async def _run(self, member: _Member, text: str, *, initial: bool) -> None:
        current = text
        first_turn = initial
        try:
            while True:
                if member.cancelled:
                    return
                self._set_phase(member, PHASE_RUNNING)
                if not first_turn:
                    self._record_message(member, "team/message/delivered", current)
                reply = await self._send(member, current)
                member.last_reply = reply if isinstance(reply, str) else str(reply or "")
                if member.cancelled:
                    return
                resting = PHASE_COMPLETED if first_turn else PHASE_IDLE
                self._set_phase(member, resting)
                first_turn = False
                if not member.queue:
                    return
                current = member.queue.pop(0)
        except asyncio.CancelledError:
            if member.phase != PHASE_CANCELLED:
                member.phase = PHASE_CANCELLED
                self._record_member(member)
            raise
        # boundary: member.agent.send() runs a full agent turn with LLM, tools and hooks
        except Exception as exc:  # noqa: BLE001 - one failed member must not break the team
            member.error = f"{type(exc).__name__}: {exc}"
            self._set_phase(member, PHASE_FAILED)
        finally:
            member.task = None
            member.rest.set()

    async def _send(self, member: _Member, text: str) -> Any:
        """Run one member turn and record the provider usage it really consumed.

        The usage is measured from the member's own session log, so a failed or
        cancelled turn still records whatever the provider already charged.
        """
        started = time.perf_counter()
        ok = False
        cancelled = False
        error = ""
        try:
            reply = await member.agent.send(text)
        except asyncio.CancelledError:
            cancelled = True
            raise
        # boundary: the member turn is a full agent turn; the failure is recorded with its partial usage
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        else:
            ok = True
            return reply
        finally:
            self._record_usage(member, started, ok=ok, cancelled=cancelled, error=error)

    def _set_phase(self, member: _Member, phase: str) -> None:
        member.phase = phase
        self._record_member(member)

    # -- records --------------------------------------------------------
    def _append_record(self, record_type: str, data: dict[str, Any], *, ignorable: bool = False) -> None:
        parent = self._parent
        append = getattr(parent, "append", None)
        if append is None:
            return
        envelope: dict[str, Any] = {"ignorable": True} if ignorable else {}
        # logging must not break team ops
        with contextlib.suppress(Exception):
            append(record_type, data, **envelope)

    def _record_member(self, member: _Member) -> None:
        self._append_record(
            "team/member",
            {"version": 1, "teamId": self.team_id, "member": _member_payload(member)},
        )

    def _record_subagent(self, member: _Member) -> None:
        self._append_record(
            "subagent/catalog",
            {
                "version": 1,
                "childId": member.id,
                "childCreatedAt": member.created_at,
                "mode": "spawn",
                "label": member.name,
            },
        )
        self._append_record(
            "subagent/descriptor",
            {
                "version": 1,
                "childId": member.id,
                "mode": "spawn",
                "provider": member.provider,
                "label": member.name,
                "agentProvider": member.provider,
                "agentModel": member.model,
            },
        )

    def _record_message(self, member: _Member, record_type: str, text: str) -> None:
        self._append_record(
            record_type,
            {
                "version": 1,
                "teamId": self.team_id,
                "memberId": member.id,
                "message": {"text": text},
            },
        )

    # -- usage ----------------------------------------------------------
    def runs(self, member_id: Any = None) -> list[MemberRun]:
        """Every measured member turn, oldest first, optionally per member."""
        if member_id is None:
            return list(self._runs.values())
        target = str(member_id)
        return [run for run in self._runs.values() if run.member_id == target]

    def run(self, run_id: Any) -> MemberRun | None:
        return self._runs.get(str(run_id))

    def run_usage(self, run_id: Any) -> SubagentUsage | None:
        run = self.run(run_id)
        return None if run is None else run.usage

    def usage(self, member_id: Any = None) -> SubagentUsage:
        """Aggregate provider usage of all recorded runs (or one member's)."""
        return sum_usages(run.usage for run in self.runs(member_id))

    def _member_records(self, member: _Member) -> list[Any]:
        session = member.session or member.restored_session
        if session is None:
            try:
                session = self._open_member_session(member)
            # boundary: the session store is plugin code; a missing log only means no usage
            except Exception:  # noqa: BLE001 - usage measurement must not break a run
                return []
            member.restored_session = session
        return list(getattr(session, "records", ()) or ())

    def _cumulative_usage(self, member: _Member) -> SubagentUsage:
        return usage_from_records(
            self._member_records(member),
            model=member.model,
            provider=member.provider,
        )

    def _measure_usage(self, member: _Member, started: float) -> SubagentUsage:
        records = self._member_records(member)
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        usage = usage_from_records(
            records,
            model=member.model,
            provider=member.provider,
            latency_ms=latency_ms,
            baseline=member.baseline,
        )
        member.baseline = usage_from_records(
            records,
            model=member.model,
            provider=member.provider,
        )
        return usage

    def _record_usage(
        self,
        member: _Member,
        started: float,
        *,
        ok: bool,
        cancelled: bool,
        error: str,
    ) -> None:
        usage = self._measure_usage(member, started)
        run = MemberRun(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            member_id=member.id,
            usage=usage,
            ok=ok,
            cancelled=cancelled,
            error=error,
            created_at=now_ms(),
        )
        self._runs[run.run_id] = run
        self._append_record(
            "team/usage",
            {
                "version": 1,
                "teamId": self.team_id,
                "runId": run.run_id,
                "memberId": run.member_id,
                "usage": usage.to_dict(),
                "ok": run.ok,
                "cancelled": run.cancelled,
                "error": run.error,
                "createdAt": run.created_at,
            },
            ignorable=True,
        )
        self._report_usage(member, usage)

    def _report_usage(self, member: _Member, usage: SubagentUsage) -> None:
        """Optionally report to a usage sink (host wired, never imported here)."""
        sink = self._usage_sink or self._services.get("usage")
        record = getattr(sink, "record", None)
        if not callable(record):
            return
        if not (usage.input_tokens or usage.output_tokens or usage.cached_tokens or usage.retries):
            return
        try:
            record(
                provider=usage.provider or member.provider,
                model=usage.model or member.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
            )
        # boundary: any object with a UsageSink shape may be wired in; reporting is best effort
        except Exception:
            logger.warning("failed to report team member usage", exc_info=True)

    # -- task board -----------------------------------------------------
    def tasks(self) -> list[TaskInfo]:
        return list(self._board.values())

    def get_task(self, task_id: Any) -> TaskInfo | None:
        return self._board.get(str(task_id))

    def create_task(
        self,
        subject: str,
        description: str = "",
        *,
        blocked_by: Any = (),
        write_scopes: Any = (),
    ) -> TaskInfo:
        task_subject = _required_text(subject, "subject")
        task_description = _text(description)
        blockers = _string_list(blocked_by, "blocked_by")
        scopes = _string_list(write_scopes, "write_scopes")
        self._validate_blockers(blockers, exclude_id=None)
        task = TaskInfo(
            id=f"task-{uuid.uuid4().hex[:12]}",
            revision=1,
            subject=task_subject,
            description=task_description,
            status="pending",
            blocked_by=tuple(blockers),
            write_scopes=tuple(scopes),
            owner="",
            created_at=now_ms(),
        )
        self._board[task.id] = task
        self._record_task(task)
        return task

    def update_task(
        self,
        task_id: Any,
        action: str,
        *,
        subject: str | None = None,
        description: str | None = None,
        blocked_by: Any = None,
        write_scopes: Any = None,
        owner: str | None = None,
    ) -> TaskInfo:
        task = self._board.get(str(task_id))
        if task is None:
            raise TeamError(f"unknown team task {task_id!r}")
        if action not in TASK_ACTIONS:
            expected = ", ".join(TASK_ACTIONS)
            raise TeamError(f"unknown task action {action!r}; expected one of {expected}")
        changes: dict[str, Any] = {}
        if subject is not None:
            changes["subject"] = _required_text(subject, "subject")
        if description is not None:
            changes["description"] = _text(description)
        if blocked_by is not None:
            blockers = _string_list(blocked_by, "blocked_by")
            self._validate_blockers(blockers, exclude_id=task.id)
            changes["blocked_by"] = tuple(blockers)
        if write_scopes is not None:
            changes["write_scopes"] = tuple(_string_list(write_scopes, "write_scopes"))
        if owner is not None:
            changes["owner"] = _text(owner)
        status = task.status
        if action == "edit":
            if not changes:
                raise TeamError("task edit requires at least one changed field")
        elif action in ("claim", "start"):
            if task.status == "completed":
                raise TeamError(f"team task {task.id!r} is already completed")
            blockers = self._blocked_refs(task)
            if blockers:
                joined = ", ".join(blockers)
                raise TeamError(f"team task {task.id!r} is blocked by {joined}")
            status = "in_progress"
            changes["owner"] = changes.get("owner") or task.owner or _DEFAULT_OWNER
        elif action == "complete":
            status = "completed"
        elif action == "reopen":
            status = "pending"
        updated = dataclasses.replace(task, revision=task.revision + 1, status=status, **changes)
        self._board[updated.id] = updated
        self._record_task(updated)
        return updated

    def _record_task(self, task: TaskInfo) -> None:
        self._append_record(
            "team/task",
            {
                "version": 1,
                "teamId": self.team_id,
                "task": {
                    "id": task.id,
                    "revision": task.revision,
                    "subject": task.subject,
                    "description": task.description,
                    "status": task.status,
                    "blockedBy": list(task.blocked_by),
                    "writeScopes": list(task.write_scopes),
                    "owner": task.owner,
                    "createdAt": task.created_at,
                },
            },
        )

    def _find_ref(self, ref: str) -> TaskInfo | None:
        task = self._board.get(ref)
        if task is not None:
            return task
        for item in self._board.values():
            if item.subject == ref:
                return item
        return None

    def _validate_blockers(self, refs: list[str], *, exclude_id: str | None) -> None:
        for ref in refs:
            found = self._find_ref(ref)
            if found is None:
                raise TeamError(f"unknown blocker {ref!r}")
            if exclude_id is not None and found.id == exclude_id:
                raise TeamError(f"team task {exclude_id!r} cannot block itself")

    def _blocked_refs(self, task: TaskInfo) -> list[str]:
        blocked: list[str] = []
        for ref in task.blocked_by:
            found = self._find_ref(ref)
            if found is not None and found.status != "completed":
                blocked.append(ref)
        return blocked


def _safe_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _safe_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _safe_strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _revision(value: Any) -> int:
    return _safe_int(value)


def _message_text(data: dict[str, Any]) -> str:
    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    text = message.get("text")
    return text if isinstance(text, str) else ""


def _fold_team_records(
    records: Iterable[LogRecord],
    team_id: str,
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, list[str]], dict[str, dict[str, Any]]]:
    order: list[str] = []
    members: dict[str, dict[str, Any]] = {}
    queues: dict[str, list[str]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    for record in records:
        data = record.data
        if not isinstance(data, dict) or data.get("teamId") != team_id:
            continue
        record_type = record.type
        if record_type == "team/member":
            payload = data.get("member")
            if not isinstance(payload, dict):
                continue
            member_id = payload.get("id")
            if not isinstance(member_id, str) or not member_id:
                continue
            if member_id not in members:
                order.append(member_id)
            members[member_id] = payload
        elif record_type == "team/task":
            payload = data.get("task")
            if not isinstance(payload, dict):
                continue
            task_id = payload.get("id")
            if not isinstance(task_id, str) or not task_id:
                continue
            current = tasks.get(task_id)
            if current is None or _revision(payload.get("revision")) >= _revision(current.get("revision")):
                tasks[task_id] = payload
        elif record_type == "team/message/queued":
            member_id = data.get("memberId")
            text = _message_text(data)
            if isinstance(member_id, str) and member_id and text:
                queues.setdefault(member_id, []).append(text)
        elif record_type == "team/message/delivered":
            member_id = data.get("memberId")
            text = _message_text(data)
            queue = queues.get(member_id) if isinstance(member_id, str) else None
            if queue is not None and text in queue:
                queue.remove(text)
    return order, members, queues, tasks


def _fold_team_usage(records: Iterable[LogRecord], team_id: str) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.type != "team/usage":
            continue
        data = record.data
        if not isinstance(data, dict) or data.get("teamId") != team_id:
            continue
        run_id = data.get("runId")
        if isinstance(run_id, str) and run_id:
            runs[run_id] = data
    return runs


def _task_from_record(task_id: str, payload: dict[str, Any]) -> TaskInfo:
    return TaskInfo(
        id=task_id,
        revision=_revision(payload.get("revision")),
        subject=_safe_text(payload.get("subject")),
        description=_safe_text(payload.get("description")),
        status=_safe_text(payload.get("status")) or "pending",
        blocked_by=tuple(_safe_strings(payload.get("blockedBy"))),
        write_scopes=tuple(_safe_strings(payload.get("writeScopes"))),
        owner=_safe_text(payload.get("owner")),
        created_at=_safe_int(payload.get("createdAt")),
    )


def _provider_name(provider: LlmProvider | None) -> str:
    if provider is None:
        return ""
    name = provider.name
    return name if isinstance(name, str) else ""


def _inherit_permission(sandbox: Any, parent: Any, session: Any) -> None:
    """Seed a child session with the lead's standing sandbox and approval knobs."""
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


__all__ = ["TeamService"]
