"""Native activity projection: fold native plugin facts into activity records.

The v4 ActivityService projects team and subagent agents from agent-session
records (team/member, subagent/descriptor, subagent/catalog). The native runtime
keeps the same information as plugin events on the native session, so this module
maps those facts onto the record shapes the service already understands. It also
exposes the subagent runs with their measured usage (or an explicit "not
recorded") for the agents panel; nothing here is estimated.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from deeppyro.tui.native_facts import native_view

TEAM_MEMBER_EVENT = "plugin.team.member.changed"
SUBAGENT_STARTED_EVENT = "plugin.subagent.started"
SUBAGENT_FINISHED_EVENT = "plugin.subagent.finished"

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

_SETTLED_STATUSES = {
    "completed": COMPLETED,
    "complete": COMPLETED,
    "done": COMPLETED,
    "finished": COMPLETED,
    "failed": FAILED,
    "failure": FAILED,
    "error": FAILED,
    "cancelled": CANCELLED,
    "canceled": CANCELLED,
}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _count(value: Any) -> int:
    return value if type(value) is int and value > 0 else 0


@dataclass(frozen=True, slots=True)
class NativeSubagentRun:
    """One native subagent run folded from the parent session facts."""

    child_id: str
    label: str = ""
    status: str = RUNNING
    provider: str = ""
    model: str = ""
    mode: str = ""
    created_at: int = 0
    usage: Any = None

    @property
    def usage_recorded(self) -> bool:
        """True when the subagent service measured token usage for this run."""
        usage = self.usage
        if usage is None:
            return False
        return bool(
            _count(getattr(usage, "input_tokens", 0))
            or _count(getattr(usage, "output_tokens", 0))
            or _count(getattr(usage, "cached_tokens", 0))
        )

    def usage_text(self) -> str:
        """Measured usage, or an explicit not-recorded marker."""
        usage = self.usage
        if usage is None:
            return "usage not recorded"
        input_tokens = _count(getattr(usage, "input_tokens", 0))
        output_tokens = _count(getattr(usage, "output_tokens", 0))
        cached_tokens = _count(getattr(usage, "cached_tokens", 0))
        if not self.usage_recorded:
            return "usage not recorded (no tokens reported)"
        text = f"usage {input_tokens} in / {output_tokens} out"
        if cached_tokens:
            text += f" / {cached_tokens} cached"
        return text


def session_events(session: Any) -> tuple[Any, ...]:
    """The folded plugin events of a native session (empty when unavailable)."""
    if session is None:
        return ()
    view = native_view(session)
    if view is None:
        return ()
    fold = getattr(view, "fold", None)
    if not callable(fold):
        return ()
    try:
        folded = fold()
    # boundary: activity projection must never break a redraw
    except Exception:  # noqa: BLE001
        return ()
    return tuple(getattr(folded, "plugin_events", ()) or ())


def _event_data(event: Any) -> dict[str, Any]:
    data = getattr(event, "data", None)
    return data if isinstance(data, dict) else {}


def team_activity_records(session: Any) -> list[Any]:
    """Team member facts replayed into v4 team/member activity records."""
    members: dict[str, dict[str, Any]] = {}
    for event in session_events(session):
        if _text(getattr(event, "type", "")) != TEAM_MEMBER_EVENT:
            continue
        data = _event_data(event)
        member_id = _text(data.get("member_id"))
        if not member_id:
            continue
        members[member_id] = {
            "id": member_id,
            "name": _text(data.get("name")) or member_id,
            "description": _text(data.get("description")),
            "phase": _text(data.get("phase")),
            "provider": _text(data.get("provider")),
            "model": _text(data.get("model")),
        }
    return [SimpleNamespace(type="team/member", data={"member": member}) for member in members.values()]


def _subagent_status(fact: dict[str, Any] | None) -> str:
    if fact is None:
        return RUNNING
    if bool(fact.get("is_error")):
        return FAILED
    status = _text(fact.get("status")).lower()
    return _SETTLED_STATUSES.get(status, COMPLETED if status else RUNNING)


def subagent_runs(session: Any, service: Any = None) -> tuple[NativeSubagentRun, ...]:
    """Every native subagent run folded from started/finished parent facts."""
    started: dict[str, dict[str, Any]] = {}
    finished: dict[str, dict[str, Any]] = {}
    for event in session_events(session):
        event_type = _text(getattr(event, "type", ""))
        if event_type not in (SUBAGENT_STARTED_EVENT, SUBAGENT_FINISHED_EVENT):
            continue
        data = _event_data(event)
        child_id = _text(data.get("child_session_id")) or _text(data.get("childSessionId"))
        if not child_id:
            continue
        if event_type == SUBAGENT_STARTED_EVENT:
            started.setdefault(child_id, data)
        else:
            finished[child_id] = data
    runs: list[NativeSubagentRun] = []
    for child_id, fact in started.items():
        usage_getter = getattr(service, "usage", None)
        usage = None
        if callable(usage_getter):
            try:
                usage = usage_getter(child_id)
            # boundary: a missing child must not hide the rest of the roster
            except Exception:  # noqa: BLE001
                usage = None
        runs.append(
            NativeSubagentRun(
                child_id=child_id,
                label=_text(fact.get("label")) or child_id,
                status=_subagent_status(finished.get(child_id)),
                provider=_text(fact.get("provider")),
                model=_text(fact.get("model")),
                mode=_text(fact.get("mode")),
                created_at=_count(fact.get("created_at")) or _count(fact.get("createdAt")),
                usage=usage,
            )
        )
    return tuple(runs)


def activity_records(session: Any) -> list[Any]:
    """V4-shaped activity records for every native team member and subagent."""
    records = team_activity_records(session)
    for run in subagent_runs(session):
        descriptor = {
            "childId": run.child_id,
            "label": run.label,
            "mode": run.mode,
            "agentProvider": run.provider,
            "agentModel": run.model,
        }
        records.append(SimpleNamespace(type="subagent/descriptor", data=descriptor))
        if run.status != RUNNING:
            records.append(SimpleNamespace(type="subagent/catalog", data=dict(descriptor)))
    return records


def refresh_activity(service: Any, session: Any) -> Any:
    """Refresh the activity service from native facts (None when unavailable)."""
    if service is None:
        return None
    return service.refresh(SimpleNamespace(records=activity_records(session)))


__all__ = [
    "CANCELLED",
    "COMPLETED",
    "FAILED",
    "RUNNING",
    "NativeSubagentRun",
    "activity_records",
    "refresh_activity",
    "session_events",
    "subagent_runs",
    "team_activity_records",
]
