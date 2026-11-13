"""Bundled session projections plugin (P1 feature B).

The plugin owns one :class:`ProjectionRegistry` and one
:class:`ProjectionCheckpoints`, registers the builtin activity units and wires
them to a live session: :meth:`ProjectionsService.attach` restores the sidecar
and subscribes to the session append observer, :meth:`save` writes the
write-behind checkpoint on close/flush boundaries.

The four activity definitions are the production counterparts of
`tests/projection/activity_defs.py`; their wire values must stay identical to
`ActivityService.refresh` (covered by tests/projection/test_activity_parity.py).
"""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.builtin.activity import AgentInfo, JobInfo, TodoItem
from deeppyro.projection import (
    ProjectionCheckpoints,
    ProjectionDefinition,
    ProjectionHandle,
    ProjectionRegistry,
)

TODO_STATUSES = ("pending", "in_progress", "completed")
PROJECTION_KEYS = (
    "activity/todos",
    "activity/jobs",
    "activity/agents",
    "activity/plan_mode",
)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _todos_state(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        content = _as_str(entry.get("content")).strip()
        status = _as_str(entry.get("status"))
        if not content or status not in TODO_STATUSES or content in seen:
            continue
        seen.add(content)
        items.append({"content": content, "status": status})
    return items


def _todos_apply(state: list[dict[str, str]], record: Any) -> list[dict[str, str]]:
    if record.type != "todo/write":
        return state
    payload = record.data if isinstance(record.data, dict) else {}
    next_items = _todos_state(payload.get("todos"))
    return state if next_items == state else next_items


def _todos_wire(state: list[dict[str, str]]) -> tuple[TodoItem, ...]:
    return tuple(TodoItem(content=item["content"], status=item["status"]) for item in state)


def _jobs_apply(state: dict[str, dict[str, Any]], record: Any) -> dict[str, dict[str, Any]]:
    if record.type not in ("job/registered", "job/settled"):
        return state
    data = record.data if isinstance(record.data, dict) else {}
    job_id = _as_str(data.get("jobId")).strip()
    if not job_id:
        return state
    previous = state.get(job_id)
    if record.type == "job/registered":
        tool_call = data.get("toolCall")
        call_id = _as_str(tool_call.get("callId")) if isinstance(tool_call, dict) else ""
        row: dict[str, Any] = {
            "id": job_id,
            "kind": _as_str(data.get("kind")),
            "label": _as_str(data.get("label")),
            "status": "running",
            "owner": _as_str(data.get("owner")),
            "started_at": _as_int(data.get("startedAt")),
            "finished_at": None,
            "call_id": call_id,
        }
    else:
        row = {
            "id": job_id,
            "kind": _as_str(data.get("kind"), previous["kind"] if previous else ""),
            "label": _as_str(data.get("label"), previous["label"] if previous else ""),
            "status": _as_str(data.get("status"), "done"),
            "owner": _as_str(data.get("owner"), previous["owner"] if previous else ""),
            "started_at": _as_int(data.get("startedAt"), previous["started_at"] if previous else 0),
            "finished_at": _as_int(data.get("finishedAt")) or None,
            "call_id": previous["call_id"] if previous else "",
        }
    if previous == row:
        return state
    jobs = dict(state)
    jobs[job_id] = row
    return jobs


def _jobs_wire(state: dict[str, dict[str, Any]]) -> tuple[JobInfo, ...]:
    return tuple(JobInfo(**row) for row in state.values())


def _agents_apply(state: dict[str, dict[str, str]], record: Any) -> dict[str, dict[str, str]]:
    data = record.data if isinstance(record.data, dict) else {}
    if record.type == "team/member":
        member = data.get("member")
        if not isinstance(member, dict):
            return state
        member_id = _as_str(member.get("id")).strip()
        if not member_id:
            return state
        row = {
            "id": member_id,
            "name": _as_str(member.get("name"), member_id),
            "description": _as_str(member.get("description")),
            "phase": _as_str(member.get("phase")),
            "provider": _as_str(member.get("provider")),
            "model": _as_str(member.get("model")),
            "parent": "",
        }
    elif record.type == "subagent/descriptor":
        child_id = _as_str(data.get("childId")).strip()
        if not child_id:
            return state
        row = {
            "id": child_id,
            "name": _as_str(data.get("label"), child_id),
            "description": _as_str(data.get("mode")),
            "phase": "running",
            "provider": _as_str(data.get("agentProvider")),
            "model": _as_str(data.get("agentModel")),
            "parent": "",
        }
    elif record.type == "subagent/catalog":
        child_id = _as_str(data.get("childId")).strip()
        if not child_id:
            return state
        row = {
            "id": child_id,
            "name": _as_str(data.get("label"), child_id),
            "description": _as_str(data.get("mode")),
            "phase": "",
            "provider": "",
            "model": "",
            "parent": "",
        }
    else:
        return state
    if state.get(row["id"]) == row:
        return state
    agents = dict(state)
    agents[row["id"]] = row
    return agents


def _agents_wire(state: dict[str, dict[str, str]]) -> tuple[AgentInfo, ...]:
    return tuple(AgentInfo(**row) for row in state.values())


def _plan_apply(state: str | None, record: Any) -> str | None:
    if record.type != "plan/mode":
        return state
    data = record.data if isinstance(record.data, dict) else {}
    mode = _as_str(data.get("mode")) or None
    return state if mode == state else mode


def definitions() -> list[ProjectionDefinition]:
    """The four builtin activity projection definitions."""
    return [
        ProjectionDefinition(
            key="activity/todos",
            state_version=1,
            init=lambda header, inherited: [],
            apply=_todos_apply,
            wire=_todos_wire,
        ),
        ProjectionDefinition(
            key="activity/jobs",
            state_version=1,
            init=lambda header, inherited: {},
            apply=_jobs_apply,
            wire=_jobs_wire,
        ),
        ProjectionDefinition(
            key="activity/agents",
            state_version=1,
            init=lambda header, inherited: {},
            apply=_agents_apply,
            wire=_agents_wire,
        ),
        ProjectionDefinition(
            key="activity/plan_mode",
            state_version=1,
            init=lambda header, inherited: None,
            apply=_plan_apply,
            wire=lambda state: state,
        ),
    ]


def register_all(registry: ProjectionRegistry) -> dict[str, ProjectionHandle]:
    """Register every builtin definition and return handles by key."""
    return {definition.key: registry.register(definition) for definition in definitions()}


class ProjectionsService:
    """Session projections plus durable sidecar checkpoints."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._registry = ProjectionRegistry()
        self._checkpoints = ProjectionCheckpoints(self._registry, enabled=enabled)
        self._handles = register_all(self._registry)
        self._watched: dict[int, Any] = {}

    @property
    def registry(self) -> ProjectionRegistry:
        return self._registry

    @property
    def checkpoints(self) -> ProjectionCheckpoints:
        return self._checkpoints

    @property
    def keys(self) -> tuple[str, ...]:
        return PROJECTION_KEYS

    def notify(self, session: Any, record: Any) -> None:
        """Advance the registry for one record (manual wiring/tests)."""
        self._registry.notify(session, record)

    def snapshot(self, session: Any, *, keys: Any = None) -> dict[str, Any]:
        return self._registry.snapshot(session, keys=keys)

    def checkpoint(self, session: Any) -> dict[str, Any]:
        return self._registry.checkpoint(session)

    def attach(self, session: Any) -> bool:
        """Restore the sidecar and observe every append of session.

        Returns True when a checkpoint was restored.  Idempotent per session.
        """
        restored = self._checkpoints.restore_if_available(session)
        observer = getattr(session, "add_append_observer", None)
        if callable(observer) and id(session) not in self._watched:
            self._watched[id(session)] = observer(self._registry.notify)
        return bool(restored)

    def detach(self, session: Any) -> None:
        """Stop observing session (its sidecar is untouched)."""
        dispose = self._watched.pop(id(session), None)
        if callable(dispose):
            dispose()

    def save(self, session: Any) -> Any:
        """Write the session's projection sidecar (write-behind)."""
        return self._checkpoints.save(session)

    def path_for(self, session: Any) -> Any:
        return self._checkpoints.path_for(session)


class ProjectionsPlugin(Plugin):
    """Kernel plugin exposing the shared projections service."""

    plugin_id = "projections"
    version = "0.1.0"
    description = "session projections and durable sidecar checkpoints"
    inject: ClassVar[list[str]] = []

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.service = ProjectionsService()

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("projections", self.service)


__all__ = [
    "PROJECTION_KEYS",
    "ProjectionsPlugin",
    "ProjectionsService",
    "definitions",
    "register_all",
]
