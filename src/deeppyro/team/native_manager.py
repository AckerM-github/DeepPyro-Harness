"""Native team manager: child native sessions, hosts, roster and task board.

This is the backend behind :class:`~deeppyro.team.manager.TeamService`, a thin
native-only subclass of the manager. The lead session is a native session;
every member is a native child session (origin "subagent",
parent id and delegation depth copied from the lead header) driven by a
:class:`NativeAgentHost`. Team state is recorded as plugin facts on the parent
session (plugin.team.member.changed, plugin.team.message.queued / delivered and
plugin.team.task), so a cold resume rebuilds the roster, the pending queue and
the task board from the folded parent log plus the child session logs.

Usage is never written to a session. Live turns are measured from the host
usage counters for the in-memory MemberRun API, while the host usage tap
forwards each turn to the team UsageSink with the child session id and
source="subagent" for the ledger and the traces.

The manager imports no v4 session types: the v4 session stack was deleted in the
0.3 wave and the native session schema is the only session log format.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.agent.native_factory import AgentBuild
from deeppyro.api import DEFAULT_PRESET_ID
from deeppyro.core.ids import now_ms
from deeppyro.core.messages import PluginSource
from deeppyro.observability.protocols import TurnUsage
from deeppyro.session.fold import fold
from deeppyro.session.native_control import NewSession
from deeppyro.subagent.types import SubagentUsage, sum_usages
from deeppyro.team.errors import NativeTeamError, TeamError
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

logger = logging.getLogger(__name__)

_DEFAULT_PRESET = DEFAULT_PRESET_ID
_DEFAULT_CONTEXT_WINDOW = 32768
_DEFAULT_OWNER = "lead"

#: Event kinds this service owns on the parent session.
MEMBER_EVENT = "plugin.team.member.changed"
QUEUED_EVENT = "plugin.team.message.queued"
DELIVERED_EVENT = "plugin.team.message.delivered"
TASK_EVENT = "plugin.team.task"


def native_parts(session: Any) -> tuple[Any, Any]:
    """Return (native session, handle) when session is a native session/handle.

    Returns (None, None) for anything that is not a native session or handle.
    A handle carries a native session; a raw native session has the snake_case
    header, an events property and an append method.
    """
    if session is None or hasattr(session, "records"):
        return None, None
    handle = session if callable(getattr(session, "append_native", None)) else None
    native = getattr(session, "native_session", None)
    if native is None:
        native = session
    if native is None:
        return None, None
    header = getattr(native, "header", None)
    events = getattr(native, "events", None)
    if header is None or events is None or not callable(getattr(native, "append", None)):
        return None, None
    return native, handle


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
        result.append(_required_text(item, field_name))
    return result


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


def _provider_name(provider: Any) -> str:
    if provider is None:
        return ""
    name = getattr(provider, "name", None)
    if isinstance(name, str):
        return name
    return ""


def _member_payload(member: _NativeMember, team_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "team_id": team_id,
        "member_id": member.id,
        "name": member.name,
        "description": member.description,
        "provider": member.provider,
        "model": member.model,
        "preset": member.preset,
        "session_id": member.session_id,
        "phase": member.phase,
        "created_at": member.created_at,
    }
    if member.last_reply:
        payload["last_reply"] = member.last_reply
    if member.error:
        payload["error"] = member.error
    return payload


def _member_info(member: _NativeMember) -> MemberInfo:
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


def _parent_host_of(candidate: Any) -> Any:
    """Return the live parent host when candidate can receive notices.

    The TUI binds a NativeAgentHost (deliver/inject), a NativeSessionHandle
    (inject) or a raw native session (no delivery): only the first two can
    route a member result back to the lead.
    """
    if candidate is None:
        return None
    if callable(getattr(candidate, "deliver", None)) or callable(getattr(candidate, "inject", None)):
        return candidate
    handle = getattr(candidate, "handle", None)
    if handle is not None and callable(getattr(handle, "inject", None)):
        return candidate
    return None


def _fold_deliveries(events: Iterable[Any], team_id: str) -> dict[str, dict[str, Any]]:
    """Fold the queued/delivered message facts into per-member counters."""
    summary: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = getattr(event, "type", None)
        if kind not in (QUEUED_EVENT, DELIVERED_EVENT):
            continue
        data = getattr(event, "data", None)
        if not isinstance(data, Mapping):
            continue
        event_team = _safe_text(data.get("team_id"))
        if event_team and event_team != team_id:
            continue
        member_id = _safe_text(data.get("member_id"))
        if not member_id:
            continue
        row = summary.get(member_id)
        if row is None:
            row = {"member_id": member_id, "queued": 0, "delivered": 0, "last": "", "last_state": ""}
            summary[member_id] = row
        state = "queued" if kind == QUEUED_EVENT else "delivered"
        row[state] = int(row[state]) + 1
        text = _safe_text(data.get("text"))
        if text:
            row["last"] = text
        row["last_state"] = state
    return summary


class _TeamUsageSink:
    """Forward child turn usage to the team ledger with child attribution.

    The native host wraps this sink in its own usage tap, so the host handle
    counters keep working while the team sink receives one TurnUsage per run.
    Sinks that only expose the legacy keyword record() shape still work.
    """

    def __init__(self, target: Any) -> None:
        self._target = target

    def record_turn(self, usage: TurnUsage) -> object:
        rewritten = dataclasses.replace(usage, source="subagent")
        record_turn = getattr(self._target, "record_turn", None)
        if callable(record_turn):
            return record_turn(rewritten)
        record = getattr(self._target, "record", None)
        if callable(record):
            return record(
                provider=rewritten.provider,
                model=rewritten.model,
                input_tokens=rewritten.input_tokens,
                output_tokens=rewritten.output_tokens,
                cached_tokens=rewritten.cached_tokens,
            )
        return None


@dataclass(slots=True)
class _NativeMember:
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
    host: Any = None
    session: Any = None
    task: asyncio.Task[None] | None = None
    queue: list[str] = field(default_factory=list)
    rest: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False


class NativeTeamManager:
    """Lead-owned native team: child sessions plus a shared task board."""

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
        self._permission: Any = None
        self._model: str = ""
        self._context_window: int = _DEFAULT_CONTEXT_WINDOW
        self._preset: str = _DEFAULT_PRESET
        self._preset_revision: int | None = None
        self._services: dict[str, Any] = {}
        self._ask: Any = None
        self._usage_sink: Any = None
        self._parent_native: Any = None
        self._parent_handle: Any = None
        self._parent_host: Any = None
        self._members: dict[str, _NativeMember] = {}
        self._board: dict[str, TaskInfo] = {}
        self._runs: dict[str, MemberRun] = {}
        self._restored = False
        self._stopping = False

    # -- configuration --------------------------------------------------
    @property
    def bound(self) -> bool:
        """Whether a native parent session is bound to this manager."""
        return self._parent_native is not None

    @property
    def team_id(self) -> str:
        if self._explicit_team_id:
            return self._explicit_team_id
        parent_id = self.parent_session_id
        return parent_id or "team"

    @property
    def parent_session_id(self) -> str:
        native = self._parent_native
        if native is None:
            return ""
        session_id = getattr(native, "session_id", None)
        if isinstance(session_id, str) and session_id:
            return session_id
        header = getattr(native, "header", None)
        header_id = getattr(header, "id", None)
        return header_id if isinstance(header_id, str) else ""

    @property
    def parent_delegation_depth(self) -> int:
        header = getattr(self._parent_native, "header", None)
        depth = getattr(header, "delegation_depth", 0)
        return depth if isinstance(depth, int) and not isinstance(depth, bool) else 0

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

        The signature mirrors the historical service.configure so the shared
        plugin wiring feeds this backend unchanged. The registry argument is
        unused: the native agent factory resolves the child tool registry from
        the preset scope.
        """
        self._cwd = Path(cwd)
        self._provider = provider
        self._permission = permission
        self._model = _text(model) or ""
        self._context_window = int(context_window)
        self._preset = _text(preset) or _DEFAULT_PRESET
        self._preset_revision = preset_revision
        self._services = dict(services or {})
        self._ask = ask
        if usage_sink is not None:
            self._usage_sink = usage_sink

    def bind_session(self, session: Any) -> None:
        """Bind the lead native session (or its handle) that owns the facts."""
        native, handle = native_parts(session)
        if native is None:
            raise NativeTeamError("native team manager requires a native parent session")
        self._parent_native = native
        self._parent_handle = handle
        self._restored = False

    def bind_parent(self, parent: Any) -> None:
        """Bind the lead parent, taking either its handle/session or its host.

        The TUI passes the live NativeAgentHost so member results can be routed
        back to the lead notice queue through host.deliver. A handle or raw
        session keeps the previous behaviour.
        """
        host = _parent_host_of(parent)
        self._parent_host = host
        target = getattr(host, "handle", None) if host is not None else None
        self.bind_session(target if target is not None else parent)

    def set_ask(self, ask: Any) -> None:
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
        """Create one native child session/host and start its first turn."""
        member_name = _required_text(name, "name")
        member_description = _text(description)
        first_prompt = _required_text(prompt, "prompt")
        live = [item for item in self._members.values() if item.phase in LIVE_PHASES]
        if len(live) >= self._max_members:
            raise TeamError(f"team is full: at most {self._max_members} live members are allowed")
        if any(item.name == member_name for item in live):
            raise TeamError(f"team member name {member_name!r} is already in use")
        if self._parent_native is None:
            raise TeamError("team service has no bound parent session")
        if self._agents is None or self._sessions is None:
            raise TeamError("team service requires the agents and sessions services")
        if self._cwd is None:
            raise TeamError("team service is not configured")
        preset_id = _text(preset) or self._preset or _DEFAULT_PRESET
        preset_revision = self._preset_revision if preset_id == self._preset else None
        provider_name = self._provider_id(provider)
        model_id = _text(model) or self._model
        child = self._create_child_session(preset_id)
        try:
            host = self._create_host(
                child,
                provider=provider_name,
                model=model_id,
                preset=preset_id,
                preset_revision=preset_revision,
            )
        except Exception as exc:
            self._close_session(child)
            raise TeamError(f"cannot create member agent: {exc}") from exc
        member = _NativeMember(
            id=f"member-{uuid.uuid4().hex[:12]}",
            name=member_name,
            description=member_description,
            provider=provider_name,
            model=model_id,
            preset=preset_id,
            session_id=child.header.id,
            created_at=now_ms(),
            host=host,
            session=child,
        )
        self._members[member.id] = member
        self._record_member(member)
        task = self._start_run(member, first_prompt, initial=True)
        if task is None:
            self._members.pop(member.id, None)
            self._close_session(child)
            raise TeamError("team members require a running event loop")
        return _member_info(member)

    def members(self) -> list[MemberInfo]:
        return [_member_info(item) for item in self._members.values()]

    def get_member(self, member_id: Any) -> MemberInfo | None:
        member = self._members.get(str(member_id))
        return None if member is None else _member_info(member)

    def _require_member(self, member_id: Any) -> _NativeMember:
        member = self._members.get(str(member_id))
        if member is None:
            raise TeamError(f"unknown team member {member_id!r}")
        return member

    def send(self, member_id: Any, message: Any) -> str:
        """Deliver a message: return started for a new turn or queued while busy."""
        member = self._require_member(member_id)
        text = _required_text(message, "message")
        if member.phase in (PHASE_FAILED, PHASE_CANCELLED):
            raise TeamError(f"team member {member.id!r} is {member.phase} and cannot receive messages")
        if member.phase in (PHASE_SPAWNING, PHASE_RUNNING):
            member.queue.append(text)
            self._record_message(member, "queued", text)
            return "queued"
        if member.host is None:
            try:
                self._load_host(member)
            except Exception as exc:
                member.error = f"{type(exc).__name__}: {exc}"
                member.phase = PHASE_FAILED
                self._record_member(member)
                raise TeamError(f"cannot restore team member {member.id!r}: {exc}") from exc
        member.rest.clear()
        member.phase = PHASE_RUNNING
        self._record_member(member)
        task = self._start_run(member, text, initial=False)
        if task is None:
            raise TeamError("team members require a running event loop")
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
        host = member.host
        interrupt = getattr(host, "interrupt", None)
        if callable(interrupt):
            with contextlib.suppress(Exception):
                result = interrupt()
                if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                    await result
        return True

    async def close(self) -> None:
        """Cancel running members, close their hosts and release child logs."""
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
            host = member.host
            member.host = None
            aclose = getattr(host, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(Exception):
                    result = aclose()
                    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                        await result
            self._close_session(member.session)
            member.session = None

    async def stop(self) -> None:
        """Stop the team for /team off without writing facts to the session log.

        close() already releases member hosts and child sessions without
        appending anything; the cancellation path in _run skips its cancelled
        member fact while this flag is set, so stopping the team leaves the
        parent log byte-identically unchanged. The roster, board and runs are
        dropped in memory: /team on restores them from the folded facts.
        """
        self._stopping = True
        try:
            await self.close()
        finally:
            with contextlib.suppress(Exception):
                self._members.clear()
                self._board.clear()
                self._runs.clear()
            self._parent_native = None
            self._parent_handle = None
            self._parent_host = None
            self._restored = True
            self._stopping = False

    def deliveries(self) -> dict[str, dict[str, Any]]:
        """Queued/delivered message counters per member from the folded facts."""
        if self._parent_native is None:
            return {}
        try:
            return _fold_deliveries(self._parent_plugin_events(), self.team_id)
        except NativeTeamError:
            return {}

    # -- cold restore ---------------------------------------------------
    async def restore(self) -> tuple[MemberInfo, ...]:
        """Rebuild roster, queue and board from the folded parent plugin facts."""
        if self._restored:
            return tuple(_member_info(item) for item in self._members.values())
        if self._parent_native is None:
            return ()
        events = self._parent_plugin_events()
        if not events:
            return ()
        self._restored = True
        order, members, queues, tasks = _fold_team_events(events, self.team_id)
        self._members.clear()
        self._board.clear()
        for member_id in order:
            member = self._restore_member(member_id, members[member_id])
            member.queue = queues.get(member_id, [])
            self._members[member_id] = member
        for task_id, payload in tasks.items():
            self._board[task_id] = _task_from_payload(task_id, payload)
        self._runs.clear()
        for member in self._members.values():
            self._resume_member(member)
        return tuple(_member_info(item) for item in self._members.values())

    def _parent_plugin_events(self) -> tuple[Any, ...]:
        handle = self._parent_handle
        if handle is not None and callable(getattr(handle, "fold", None)):
            try:
                return tuple(handle.fold().plugin_events)
            except Exception as exc:
                raise NativeTeamError(f"cannot read the native parent session: {exc}") from exc
        native = self._parent_native
        if native is None:
            return ()
        try:
            return tuple(fold(native.header, native.events).plugin_events)
        except Exception as exc:
            raise NativeTeamError(f"cannot read the native parent session: {exc}") from exc

    def _restore_member(self, member_id: str, payload: Mapping[str, Any]) -> _NativeMember:
        phase = _safe_text(payload.get("phase")) or PHASE_IDLE
        if phase in (PHASE_SPAWNING, PHASE_RUNNING) or phase not in SETTLED_PHASES:
            phase = PHASE_IDLE
        error = _safe_text(payload.get("error")) or None
        member = _NativeMember(
            id=member_id,
            name=_safe_text(payload.get("name")),
            description=_safe_text(payload.get("description")),
            provider=_safe_text(payload.get("provider")),
            model=_safe_text(payload.get("model")),
            preset=_safe_text(payload.get("preset")) or self._preset,
            session_id=_safe_text(payload.get("session_id")),
            created_at=_safe_int(payload.get("created_at")),
            phase=phase,
            last_reply=_safe_text(payload.get("last_reply")),
            error=error,
        )
        try:
            if not member.session_id:
                raise NativeTeamError("member fact has no session_id")
            member.session = self._open_member_session(member)
        # boundary: the sessions backend is plugin code; one lost member must not break restore
        except Exception as exc:  # noqa: BLE001
            member.error = f"cannot restore team member session: {exc}"
            member.phase = PHASE_FAILED
        return member

    def _resume_member(self, member: _NativeMember) -> None:
        if not member.queue or member.phase in (PHASE_FAILED, PHASE_CANCELLED):
            return
        text = member.queue.pop(0)
        member.phase = PHASE_RUNNING
        try:
            self._load_host(member)
        # boundary: host creation is plugin code; the queue is kept and the member marked failed
        except Exception as exc:  # noqa: BLE001
            member.queue.insert(0, text)
            member.error = f"{type(exc).__name__}: {exc}"
            member.phase = PHASE_FAILED
            self._record_member(member)
            return
        task = self._start_run(member, text, initial=False)
        if task is None:
            member.queue.insert(0, text)
            member.error = "team members require a running event loop"
            member.phase = PHASE_FAILED
            self._record_member(member)

    def _open_member_session(self, member: _NativeMember) -> Any:
        opener = getattr(self._sessions, "native_open", None)
        if not callable(opener):
            raise NativeTeamError("sessions service does not support native sessions")
        return opener(member.session_id)

    # -- runtime --------------------------------------------------------
    def _start_run(self, member: _NativeMember, text: str, *, initial: bool) -> asyncio.Task[None] | None:
        coroutine = self._run(member, text, initial=initial)
        try:
            member.task = asyncio.create_task(coroutine)
        except RuntimeError:
            coroutine.close()
            return None
        return member.task

    async def _run(self, member: _NativeMember, text: str, *, initial: bool) -> None:
        current = text
        first_turn = initial
        try:
            while True:
                if member.cancelled:
                    return
                self._set_phase(member, PHASE_RUNNING)
                if not first_turn:
                    self._record_message(member, "delivered", current)
                reply = await self._send(member, current)
                member.last_reply = reply
                if member.cancelled:
                    return
                resting = PHASE_COMPLETED if first_turn else PHASE_IDLE
                self._set_phase(member, resting)
                first_turn = False
                if not member.queue:
                    return
                current = member.queue.pop(0)
        except asyncio.CancelledError:
            # /team off stops the manager without touching the session log
            if member.phase != PHASE_CANCELLED and not self._stopping:
                member.phase = PHASE_CANCELLED
                self._record_member(member)
            raise
        # boundary: one failed member must not break the team; the error is recorded
        except Exception as exc:  # noqa: BLE001
            member.error = f"{type(exc).__name__}: {exc}"
            self._set_phase(member, PHASE_FAILED)
        finally:
            member.task = None
            member.rest.set()
            if not self._stopping:
                self._notify_parent(member)

    async def _send(self, member: _NativeMember, text: str) -> str:
        """Run one member turn on the native host and measure its usage deltas."""
        started = time.perf_counter()
        before = self._usage_counters(member)
        ok = False
        cancelled = False
        error = ""
        result: Any = None
        try:
            host = member.host
            if host is None:
                host = self._load_host(member)
            result = await host.send(text)
        except asyncio.CancelledError:
            cancelled = True
            raise
        # boundary: a member turn is a full agent run; record the failure and its partial usage
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        else:
            ok = True
            return str(getattr(result, "final_text", "") or "")
        finally:
            self._record_usage(
                member,
                before,
                started,
                ok=ok,
                cancelled=cancelled,
                error=error,
                steps=_safe_int(getattr(result, "steps", 0)),
            )

    def _notify_parent(self, member: _NativeMember) -> None:
        """Route one settled member result to the lead through host.deliver.

        The lead sees the notice on its next turn; a busy lead queues it. A
        missing host (torn down runtime) or a failed delivery is ignored: the
        member facts on the log are the durable record.
        """
        host = self._parent_host
        deliver = getattr(host, "deliver", None)
        if not callable(deliver):
            return
        if member.phase == PHASE_FAILED:
            text = f"team member {member.name} failed: {member.error or 'unknown error'}"
        elif member.phase == PHASE_CANCELLED:
            text = f"team member {member.name} was cancelled"
        elif member.last_reply:
            text = f"team member {member.name}: {member.last_reply}"
        else:
            return
        text = text if len(text) <= 2000 else text[:1997] + "..."
        try:
            deliver(text, source=PluginSource(plugin="team"))
        except TypeError:
            with contextlib.suppress(Exception):
                deliver(text)
        except Exception:
            logger.warning("failed to deliver team member result to the lead", exc_info=True)

    def _set_phase(self, member: _NativeMember, phase: str) -> None:
        member.phase = phase
        self._record_member(member)

    def _load_host(self, member: _NativeMember) -> Any:
        host = member.host
        if host is not None and not getattr(host, "closed", False):
            return host
        session = member.session
        if session is None:
            session = self._open_member_session(member)
            member.session = session
        preset_id = member.preset or self._preset
        host = self._create_host(
            session,
            provider=member.provider or self._provider_id(None),
            model=member.model or self._model,
            preset=preset_id,
            preset_revision=self._preset_revision if preset_id == self._preset else None,
        )
        # Repair a run that was interrupted by the previous process; the loop
        # writes an aborted turn without fabricating assistant content.
        with contextlib.suppress(Exception):
            host.restore()
        member.host = host
        return host

    # -- child wiring ---------------------------------------------------
    def _create_child_session(self, preset_id: str) -> Any:
        creator = getattr(self._sessions, "native_create", None)
        if not callable(creator):
            raise NativeTeamError("sessions service does not support native sessions")
        parent_id = self.parent_session_id
        if not parent_id:
            raise TeamError("team service has no bound parent session")
        cwd = self._cwd
        if cwd is None:
            raise TeamError("team service is not configured")
        spec = NewSession(
            cwd=Path(cwd),
            preset=preset_id,
            origin="subagent",
            parent_id=parent_id,
            delegation_depth=self.parent_delegation_depth + 1,
        )
        try:
            return creator(spec)
        except Exception as exc:
            raise TeamError(f"cannot create team member session: {exc}") from exc

    def _provider_id(self, provider: Any) -> str:
        if isinstance(provider, str) and provider:
            return provider
        resolved = self._provider if provider is None else provider
        name = _provider_name(resolved)
        if name:
            return name
        if isinstance(self._provider, str) and self._provider:
            return self._provider
        return ""

    def _create_host(
        self,
        session: Any,
        *,
        provider: str,
        model: str,
        preset: str,
        preset_revision: int | None,
    ) -> Any:
        factory = getattr(self._agents, "host", None)
        if not callable(factory):
            raise NativeTeamError("agents service does not support native agent hosts")
        cwd = self._cwd
        if cwd is None:
            raise TeamError("team service is not configured")
        build = AgentBuild(
            provider=provider,
            model=model,
            cwd=Path(cwd),
            preset=preset,
            preset_revision=preset_revision,
            permission=self._child_permission(session),
            hooks=None,
            services=self._child_services(),
            context_window=self._context_window,
        )
        return factory(session, build)

    def _child_services(self) -> dict[str, Any]:
        services = dict(self._services)
        target = self._usage_sink or services.get("usage")
        if target is not None:
            services["usage"] = _TeamUsageSink(target)
        return services

    def _child_permission(self, session: Any) -> Any:
        """Return the child gate when the sandbox service handles native sessions.

        Native sandbox/approval services are a separate migration. When the
        gate cannot be built for the child log the lead permission object is
        reused, and this fallback is documented in docs/spec/team.md.
        """
        sandbox = self._sandbox
        if sandbox is None or not callable(getattr(sandbox, "gate", None)):
            return self._permission
        try:
            return sandbox.gate(self._cwd, ask=self._ask, session=session)
        except TypeError:
            try:
                return sandbox.gate(self._cwd, ask=self._ask)
            except TypeError:
                return sandbox.gate(self._cwd)
        except Exception:  # noqa: BLE001 - the shared permission gate must not break a spawn
            return self._permission

    # -- facts ----------------------------------------------------------
    def _append_fact(self, kind: str, data: Mapping[str, Any]) -> None:
        # The native session append is the one writer for plugin facts; the
        # handle's append_native maps record types, not plugin kinds. A failed
        # append is logged instead of swallowed so a lost fact is observable.
        native = self._parent_native
        if native is None:
            return
        try:
            native.append(kind, dict(data))
        # boundary: team operations continue, but the loss is reported
        except Exception:
            logger.warning("failed to record team fact %s", kind, exc_info=True)

    def _record_member(self, member: _NativeMember) -> None:
        self._append_fact(MEMBER_EVENT, _member_payload(member, self.team_id))

    def _record_message(self, member: _NativeMember, state: str, text: str) -> None:
        kind = QUEUED_EVENT if state == "queued" else DELIVERED_EVENT
        self._append_fact(
            kind,
            {
                "team_id": self.team_id,
                "member_id": member.id,
                "text": text,
                "created_at": now_ms(),
            },
        )

    def _record_task(self, task: TaskInfo) -> None:
        self._append_fact(
            TASK_EVENT,
            {"version": 1, "team_id": self.team_id, "task": task.to_dict()},
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
        """Aggregate live provider usage of the runs measured in this process."""
        return sum_usages(run.usage for run in self.runs(member_id))

    def _usage_counters(self, member: _NativeMember) -> dict[str, int]:
        host = member.host
        getter = getattr(host, "usage", None)
        if not callable(getter):
            return {}
        try:
            raw = getter()
        except Exception:  # noqa: BLE001 - usage reads must not break a run
            return {}
        if not isinstance(raw, Mapping):
            return {}
        counters: dict[str, int] = {}
        for key in ("input", "output", "cached"):
            value = raw.get(key)
            counters[key] = (
                value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
            )
        return counters

    def _record_usage(
        self,
        member: _NativeMember,
        before: Mapping[str, int],
        started: float,
        *,
        ok: bool,
        cancelled: bool,
        error: str,
        steps: int,
    ) -> None:
        after = self._usage_counters(member)
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        usage = SubagentUsage(
            input_tokens=max(0, after.get("input", 0) - before.get("input", 0)),
            output_tokens=max(0, after.get("output", 0) - before.get("output", 0)),
            cached_tokens=max(0, after.get("cached", 0) - before.get("cached", 0)),
            latency_ms=latency_ms,
            model=member.model,
            provider=member.provider,
            steps=steps,
        )
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

    # -- teardown -------------------------------------------------------
    def _close_session(self, session: Any) -> None:
        if session is None or getattr(session, "closed", True):
            return
        closer = getattr(session, "close", None)
        if callable(closer):
            with contextlib.suppress(Exception):
                closer()


def _fold_team_events(
    events: Iterable[Any],
    team_id: str,
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, list[str]], dict[str, dict[str, Any]]]:
    """Fold plugin.team.* events into roster, queue and task board state."""
    order: list[str] = []
    members: dict[str, dict[str, Any]] = {}
    queues: dict[str, list[str]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = getattr(event, "type", None)
        data = getattr(event, "data", None)
        if not isinstance(kind, str) or not isinstance(data, Mapping):
            continue
        if not kind.startswith("plugin.team."):
            continue
        scope = data.get("team_id")
        if isinstance(scope, str) and scope != team_id:
            continue
        if kind == MEMBER_EVENT:
            payload = data.get("member")
            member_id = data.get("member_id")
            if member_id is None and isinstance(payload, Mapping):
                member_id = payload.get("member_id")
            if not isinstance(member_id, str) or not member_id:
                logger.warning("skipping malformed native team member fact")
                continue
            if member_id not in members:
                order.append(member_id)
            merged: dict[str, Any] = dict(payload) if isinstance(payload, Mapping) else {}
            merged.update({key: value for key, value in data.items() if key != "member"})
            members[member_id] = merged
        elif kind == TASK_EVENT:
            payload = data.get("task")
            if not isinstance(payload, Mapping):
                continue
            task_id = payload.get("id")
            if not isinstance(task_id, str) or not task_id:
                continue
            current = tasks.get(task_id)
            if current is None or _revision(payload.get("revision")) >= _revision(current.get("revision")):
                tasks[task_id] = dict(payload)
        elif kind == QUEUED_EVENT:
            member_id = data.get("member_id")
            text = data.get("text")
            if isinstance(member_id, str) and member_id and isinstance(text, str) and text:
                queues.setdefault(member_id, []).append(text)
        elif kind == DELIVERED_EVENT:
            member_id = data.get("member_id")
            text = data.get("text")
            queue = queues.get(member_id) if isinstance(member_id, str) else None
            if queue is not None and isinstance(text, str) and text in queue:
                queue.remove(text)
    return order, members, queues, tasks


def _task_from_payload(task_id: str, payload: Mapping[str, Any]) -> TaskInfo:
    return TaskInfo(
        id=task_id,
        revision=_revision(payload.get("revision")),
        subject=_safe_text(payload.get("subject")),
        description=_safe_text(payload.get("description")),
        status=_safe_text(payload.get("status")) or "pending",
        blocked_by=tuple(_safe_strings(payload.get("blocked_by"))),
        write_scopes=tuple(_safe_strings(payload.get("write_scopes"))),
        owner=_safe_text(payload.get("owner")),
        created_at=_safe_int(payload.get("created_at")),
    )


__all__ = ["MEMBER_EVENT", "NativeTeamManager", "native_parts"]
