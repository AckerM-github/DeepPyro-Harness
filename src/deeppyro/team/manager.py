"""Agent team service: subagent members, messaging and the shared task board.

The team service is owned by the lead session.  It spawns one child session per
member (delegationDepth 1, origin "subagent"), runs a private AgentSession for
it, forwards messages, and records DSH compatible team/subagent events on the
parent session log.  Everything that touches the kernel is injected, so the
service is unit-testable without a provider or a kernel boot.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.api import CHILD_DISABLED_TOOLS
from deeppyro.core.ids import new_session_id, now_ms
from deeppyro.core.wire import SessionHeader
from deeppyro.session.layout import flat_session_path
from deeppyro.session.session import Session
from deeppyro.session.writer import write_new_session
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
    TaskInfo,
)
from deeppyro.tools.registry import ToolRegistry

_DEFAULT_PRESET = "default"
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
    task: asyncio.Task[None] | None = None
    queue: list[str] = field(default_factory=list)
    rest: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False


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
        self._parent: Any = None
        self._members: dict[str, _Member] = {}
        self._board: dict[str, TaskInfo] = {}

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
        session_id = getattr(parent, "session_id", "")
        if isinstance(session_id, str) and session_id:
            return session_id
        header = getattr(parent, "header", None)
        header_id = getattr(header, "id", "")
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
    ) -> None:
        """Attach the live lead-session context used to spawn members."""
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

    def bind_session(self, session: Any) -> None:
        """Bind the lead session that receives this team's records."""
        self._parent = session

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
            id=f"member-{uuid.uuid4().hex[:12]}",
            name=member_name,
            description=member_description,
            provider=_provider_name(self._resolve_provider(provider)),
            model=_text(model) or self._model,
            preset=preset_id,
            session_id=child_session.session_id,
            created_at=now_ms(),
            agent=agent,
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
            try:
                result = interrupt()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001, S110 - cancellation must not raise
                pass
        return True

    async def close(self) -> None:
        """Cancel every running member task (used by runtime shutdown)."""
        tasks = [
            member.task
            for member in self._members.values()
            if member.task is not None and not member.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
        resolved = self._resolve_provider(provider)
        try:
            options = self._agents.options(model=model, context_window=self._context_window)
        except Exception as exc:
            raise TeamError(f"cannot build member agent options: {exc}") from exc
        try:
            return self._agents.create(
                session=child_session,
                provider=resolved,
                registry=self._child_registry(),
                options=options,
                sink=_MemberSink(),
                permission=self._child_permission(),
                cwd=str(self._cwd),
                services=dict(self._services),
                preset=preset,
                preset_revision=preset_revision,
            )
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
                reply = await member.agent.send(current)
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
        except Exception as exc:  # noqa: BLE001 - one failed member must not break the team
            member.error = f"{type(exc).__name__}: {exc}"
            self._set_phase(member, PHASE_FAILED)
        finally:
            member.task = None
            member.rest.set()

    def _set_phase(self, member: _Member, phase: str) -> None:
        member.phase = phase
        self._record_member(member)

    # -- records --------------------------------------------------------
    def _append_record(self, record_type: str, data: dict[str, Any]) -> None:
        parent = self._parent
        append = getattr(parent, "append", None)
        if append is None:
            return
        try:
            append(record_type, data)
        except Exception:  # noqa: BLE001, S110 - logging must not break team ops
            pass

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


def _provider_name(provider: Any) -> str:
    name = getattr(provider, "name", "")
    return name if isinstance(name, str) else ""


__all__ = ["TeamService"]
