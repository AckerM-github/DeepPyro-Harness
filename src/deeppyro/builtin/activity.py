"""Session activity feed: plan, jobs and agent/team state.

The service is a pure projection over the durable session log. It never owns
facts: it scans append-only records, keeps the newest state and notifies
optional listeners. TUI widgets consume :class:`ActivityState`; background
services can call :meth:`ActivityService.refresh` after appending a record.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from deeppyro.api import Plugin

TODO_STATUSES = ("pending", "in_progress", "completed")


@dataclass(frozen=True, slots=True)
class TodoItem:
    """One item of the session plan."""

    content: str
    status: str


@dataclass(frozen=True, slots=True)
class JobInfo:
    """One background job known to the session."""

    id: str
    kind: str = ""
    label: str = ""
    status: str = "running"
    owner: str = ""
    started_at: int = 0
    finished_at: int | None = None
    call_id: str = ""


@dataclass(frozen=True, slots=True)
class AgentInfo:
    """One member of the agent team (or one subagent)."""

    id: str
    name: str = ""
    description: str = ""
    phase: str = ""
    provider: str = ""
    model: str = ""
    parent: str = ""


@dataclass(frozen=True, slots=True)
class ActivityState:
    """Immutable snapshot consumed by the TUI activity bar and panels."""

    todos: tuple[TodoItem, ...] = ()
    jobs: tuple[JobInfo, ...] = ()
    agents: tuple[AgentInfo, ...] = ()
    plan_mode: str | None = None
    revision: int = 0

    @property
    def has_items(self) -> bool:
        return bool(self.todos or self.jobs or self.agents)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_todos(raw: Any) -> tuple[TodoItem, ...]:
    """Normalise a todo/write payload, dropping malformed entries."""
    if not isinstance(raw, list):
        return ()
    items: list[TodoItem] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        content = _as_str(entry.get("content")).strip()
        status = _as_str(entry.get("status"))
        if not content or status not in TODO_STATUSES or content in seen:
            continue
        seen.add(content)
        items.append(TodoItem(content=content, status=status))
    return tuple(items)


def plan_counts(todos: tuple[TodoItem, ...]) -> dict[str, int]:
    """Counts by status plus a total, used by the activity bar."""
    counts = {status: 0 for status in TODO_STATUSES}
    for item in todos:
        counts[item.status] = counts.get(item.status, 0) + 1
    counts["total"] = len(todos)
    return counts


def current_todo(todos: tuple[TodoItem, ...]) -> TodoItem | None:
    """The first in-progress item, else the first pending item."""
    for item in todos:
        if item.status == "in_progress":
            return item
    for item in todos:
        if item.status == "pending":
            return item
    return None


class ActivityService:
    """Projection of todo/job/team records with optional change listeners."""

    def __init__(self) -> None:
        self._state = ActivityState()
        self._listeners: list[Callable[[ActivityState], None]] = []

    # -- state ----------------------------------------------------------
    @property
    def state(self) -> ActivityState:
        return self._state

    def subscribe(self, listener: Callable[[ActivityState], None]) -> Callable[[], None]:
        """Register a change callback and return its unsubscribe function."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    # -- projection -----------------------------------------------------
    def refresh(self, session: Any) -> ActivityState:
        """Rebuild state from *session* records and notify on change."""
        records = getattr(session, "records", None)
        if not isinstance(records, list):
            records = []
        todos: tuple[TodoItem, ...] = ()
        jobs: dict[str, JobInfo] = {}
        agents: dict[str, AgentInfo] = {}
        plan_mode: str | None = None
        for record in records:
            record_type = _as_str(getattr(record, "type", ""))
            data = getattr(record, "data", None)
            if not isinstance(data, dict):
                continue
            if record_type == "todo/write":
                todos = parse_todos(data.get("todos"))
            elif record_type == "job/registered":
                job_id = _as_str(data.get("jobId")).strip()
                if job_id:
                    tool_call = data.get("toolCall")
                    call_id = ""
                    if isinstance(tool_call, dict):
                        call_id = _as_str(tool_call.get("callId"))
                    jobs[job_id] = JobInfo(
                        id=job_id,
                        kind=_as_str(data.get("kind")),
                        label=_as_str(data.get("label")),
                        status="running",
                        owner=_as_str(data.get("owner")),
                        started_at=_as_int(data.get("startedAt")),
                        call_id=call_id,
                    )
            elif record_type == "job/settled":
                job_id = _as_str(data.get("jobId")).strip()
                if not job_id:
                    continue
                previous = jobs.get(job_id)
                jobs[job_id] = JobInfo(
                    id=job_id,
                    kind=_as_str(data.get("kind"), previous.kind if previous else ""),
                    label=_as_str(data.get("label"), previous.label if previous else ""),
                    status=_as_str(data.get("status"), "done"),
                    owner=_as_str(data.get("owner"), previous.owner if previous else ""),
                    started_at=_as_int(data.get("startedAt"), previous.started_at if previous else 0),
                    finished_at=_as_int(data.get("finishedAt")) or None,
                    call_id=previous.call_id if previous else "",
                )
            elif record_type == "team/member":
                member = data.get("member")
                if isinstance(member, dict):
                    member_id = _as_str(member.get("id")).strip()
                    if member_id:
                        agents[member_id] = AgentInfo(
                            id=member_id,
                            name=_as_str(member.get("name"), member_id),
                            description=_as_str(member.get("description")),
                            phase=_as_str(member.get("phase")),
                            provider=_as_str(member.get("provider")),
                            model=_as_str(member.get("model")),
                        )
            elif record_type == "subagent/descriptor":
                child_id = _as_str(data.get("childId")).strip()
                if child_id:
                    agents[child_id] = AgentInfo(
                        id=child_id,
                        name=_as_str(data.get("label"), child_id),
                        description=_as_str(data.get("mode")),
                        phase="running",
                        provider=_as_str(data.get("agentProvider")),
                        model=_as_str(data.get("agentModel")),
                    )
            elif record_type == "subagent/catalog":
                child_id = _as_str(data.get("childId")).strip()
                if child_id:
                    agents[child_id] = AgentInfo(
                        id=child_id,
                        name=_as_str(data.get("label"), child_id),
                        description=_as_str(data.get("mode")),
                        phase="",
                    )
            elif record_type == "plan/mode":
                plan_mode = _as_str(data.get("mode")) or None
        previous = self._state
        state = ActivityState(
            todos=todos,
            jobs=tuple(jobs.values()),
            agents=tuple(agents.values()),
            plan_mode=plan_mode,
            revision=previous.revision,
        )
        if self._same_payload(previous, state):
            return previous
        state = ActivityState(
            todos=state.todos,
            jobs=state.jobs,
            agents=state.agents,
            plan_mode=state.plan_mode,
            revision=previous.revision + 1,
        )
        self._state = state
        self._notify(state)
        return state

    @staticmethod
    def _same_payload(left: ActivityState, right: ActivityState) -> bool:
        return (
            left.todos == right.todos
            and left.jobs == right.jobs
            and left.agents == right.agents
            and left.plan_mode == right.plan_mode
        )

    def _notify(self, state: ActivityState) -> None:
        for listener in list(self._listeners):
            try:
                listener(state)
            except Exception:  # noqa: BLE001, S110 - a bad listener must not break refresh
                pass


class ActivityPlugin(Plugin):
    """Kernel plugin exposing the shared activity service."""

    plugin_id = "activity"
    version = "0.1.0"
    description = "session activity projection: plan, jobs, agent team"
    inject: ClassVar[list[str]] = []

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.service = ActivityService()

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("activity", self.service)


__all__ = [
    "ActivityPlugin",
    "ActivityService",
    "ActivityState",
    "AgentInfo",
    "JobInfo",
    "TodoItem",
    "current_todo",
    "parse_todos",
    "plan_counts",
]
