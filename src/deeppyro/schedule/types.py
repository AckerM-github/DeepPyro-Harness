"""Domain types for scheduled tasks.

A schedule is a prompt plus a timing rule and a target.  The same record type is
written to disk (camelCase JSON, see :mod:`deeppyro.schedule.store`), returned by
the manager and rendered by the schedule tool.

Timings are modelled as one frozen dataclass per kind so the executor can reason
about one shape at a time instead of a bag of optional fields.  All seven kinds
from the reference implementation are modelled here; cron is accepted by the
store and by the type layer but the P1 manager refuses to create it (execution
deferred, see docs/spec/schedule.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: On-disk schema version for ~/.dpro/schedules.json.
STORE_VERSION = 1

KIND_AFTER = "after"
KIND_AT = "at"
KIND_EVERY = "every"
KIND_DAILY = "daily"
KIND_WEEKLY = "weekly"
KIND_CRON = "cron"
KIND_RRULE = "rrule"
KINDS: tuple[str, ...] = (KIND_AFTER, KIND_AT, KIND_EVERY, KIND_DAILY, KIND_WEEKLY, KIND_CRON, KIND_RRULE)

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_ENDED = "ended"
STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_ENDED)

TARGET_SESSION = "session"
TARGET_NEW_ONCE = "new_once"
TARGET_NEW = "new"
TARGET_KINDS: tuple[str, ...] = (TARGET_SESSION, TARGET_NEW_ONCE, TARGET_NEW)

MAX_TITLE_CHARS = 120
MIN_EVERY_SECONDS = 60
MAX_EVERY_SECONDS = 604_800
MAX_AFTER_SECONDS = 31_536_000
MAX_COUNT = 1000

#: Kinds whose next occurrence is derived from a rule (they never end by
#: themselves); after/at deliver once and cron is not executed yet.
RECURRING_KINDS: tuple[str, ...] = (KIND_EVERY, KIND_DAILY, KIND_WEEKLY, KIND_RRULE)
ONE_SHOT_KINDS: tuple[str, ...] = (KIND_AFTER, KIND_AT)


class ScheduleError(RuntimeError):
    """Base class for schedule failures."""


class ScheduleInputError(ScheduleError):
    """Invalid schedule input; carries the stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ScheduleStoreError(ScheduleError):
    """The schedules file is unreadable, corrupt or from a newer version."""


# -- timing ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AfterTiming:
    """Deliver once this many seconds after creation."""

    after_seconds: int


@dataclass(frozen=True, slots=True)
class AtTiming:
    """Deliver once at an absolute RFC3339 instant."""

    at: str


@dataclass(frozen=True, slots=True)
class EveryTiming:
    """Deliver every N seconds."""

    every_seconds: int


@dataclass(frozen=True, slots=True)
class DailyTiming:
    """Deliver every day at a local wall-clock time."""

    time: str
    time_zone: str = ""


@dataclass(frozen=True, slots=True)
class WeeklyTiming:
    """Deliver on selected ISO weekdays (1=Monday..7=Sunday) at a local time."""

    time: str
    weekdays: tuple[int, ...]
    time_zone: str = ""


@dataclass(frozen=True, slots=True)
class CronTiming:
    """Five-field cron expression (modelled but not executed in P1)."""

    expression: str
    time_zone: str = ""


@dataclass(frozen=True, slots=True)
class RRuleTiming:
    """DTSTART + RRULE pair (strict subset, see :mod:`deeppyro.schedule.rrule`)."""

    rrule: str


type Timing = AfterTiming | AtTiming | EveryTiming | DailyTiming | WeeklyTiming | CronTiming | RRuleTiming


def timing_kind(timing: Timing) -> str:
    if isinstance(timing, AfterTiming):
        return KIND_AFTER
    if isinstance(timing, AtTiming):
        return KIND_AT
    if isinstance(timing, EveryTiming):
        return KIND_EVERY
    if isinstance(timing, DailyTiming):
        return KIND_DAILY
    if isinstance(timing, WeeklyTiming):
        return KIND_WEEKLY
    if isinstance(timing, CronTiming):
        return KIND_CRON
    if isinstance(timing, RRuleTiming):
        return KIND_RRULE
    raise ScheduleInputError("invalid_selector", f"unknown timing object {timing!r}")


def timing_time_zone(timing: Timing) -> str:
    """Return the explicit time zone of a timing, or an empty string."""

    return getattr(timing, "time_zone", "") or ""


def timing_to_dict(timing: Timing) -> dict[str, Any]:
    kind = timing_kind(timing)
    if isinstance(timing, AfterTiming):
        return {"after_seconds": timing.after_seconds}
    if isinstance(timing, AtTiming):
        return {"at": timing.at}
    if isinstance(timing, EveryTiming):
        return {"every_seconds": timing.every_seconds}
    if isinstance(timing, DailyTiming):
        return {"time": timing.time, "time_zone": timing.time_zone}
    if isinstance(timing, WeeklyTiming):
        return {"time": timing.time, "weekdays": list(timing.weekdays), "time_zone": timing.time_zone}
    if isinstance(timing, CronTiming):
        return {"expression": timing.expression, "time_zone": timing.time_zone}
    if isinstance(timing, RRuleTiming):
        return {"rrule": timing.rrule}
    raise ScheduleInputError("invalid_selector", f"unknown timing kind {kind!r}")


def _as_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScheduleInputError("invalid_rule", f"{field_name} must be an integer")
    return value


def timing_from_dict(kind: str, data: Mapping[str, Any]) -> Timing:
    """Reconstruct a timing from the persisted field dict."""

    if kind == KIND_AFTER:
        return AfterTiming(after_seconds=_as_int(data.get("after_seconds"), field_name="after_seconds"))
    if kind == KIND_AT:
        at = data.get("at")
        if not isinstance(at, str) or not at:
            raise ScheduleInputError("invalid_rule", "at must be a non-empty string")
        return AtTiming(at=at)
    if kind == KIND_EVERY:
        return EveryTiming(every_seconds=_as_int(data.get("every_seconds"), field_name="every_seconds"))
    if kind == KIND_DAILY:
        time = data.get("time")
        if not isinstance(time, str) or not time:
            raise ScheduleInputError("invalid_rule", "daily time must be a non-empty string")
        zone = str(data.get("time_zone") or "")
        return DailyTiming(time=time, time_zone=zone)
    if kind == KIND_WEEKLY:
        time = data.get("time")
        if not isinstance(time, str) or not time:
            raise ScheduleInputError("invalid_rule", "weekly time must be a non-empty string")
        raw_days = data.get("weekdays")
        if not isinstance(raw_days, (list, tuple)) or not raw_days:
            raise ScheduleInputError("invalid_rule", "weekly weekdays must be a non-empty list")
        days = tuple(_as_int(day, field_name="weekday") for day in raw_days)
        zone = str(data.get("time_zone") or "")
        return WeeklyTiming(time=time, weekdays=days, time_zone=zone)
    if kind == KIND_CRON:
        expression = data.get("expression")
        if not isinstance(expression, str) or not expression:
            raise ScheduleInputError("invalid_rule", "cron expression must be a non-empty string")
        return CronTiming(expression=expression, time_zone=str(data.get("time_zone") or ""))
    if kind == KIND_RRULE:
        rrule = data.get("rrule")
        if not isinstance(rrule, str) or not rrule.strip():
            raise ScheduleInputError("invalid_rule", "rrule must be a non-empty string")
        return RRuleTiming(rrule=rrule)
    raise ScheduleInputError("invalid_rule", f"unknown timing kind {kind!r}")


# -- targets ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionCreateSpec:
    """Options used when a target has to create a fresh session."""

    cwd: str = ""
    agent_preset: str = ""


@dataclass(frozen=True, slots=True)
class SessionTarget:
    """Deliver into an existing session."""

    session_id: str


@dataclass(frozen=True, slots=True)
class NewOnceTarget:
    """Create one session on the first delivery and reuse it afterwards."""

    session_id: str = ""
    create: SessionCreateSpec = SessionCreateSpec()


@dataclass(frozen=True, slots=True)
class NewTarget:
    """Create a fresh session for every delivery."""

    create: SessionCreateSpec = SessionCreateSpec()


type Target = SessionTarget | NewOnceTarget | NewTarget


def target_kind(target: Target) -> str:
    if isinstance(target, SessionTarget):
        return TARGET_SESSION
    if isinstance(target, NewOnceTarget):
        return TARGET_NEW_ONCE
    if isinstance(target, NewTarget):
        return TARGET_NEW
    raise ScheduleInputError("invalid_selector", f"unknown target object {target!r}")


def target_session_id(target: Target) -> str | None:
    if isinstance(target, SessionTarget):
        return target.session_id
    if isinstance(target, NewOnceTarget):
        return target.session_id or None
    return None


def _create_dict(spec: SessionCreateSpec) -> dict[str, Any]:
    return {"cwd": spec.cwd, "agent_preset": spec.agent_preset}


def _create_from_dict(data: Any) -> SessionCreateSpec:
    if data is None:
        return SessionCreateSpec()
    if not isinstance(data, Mapping):
        raise ScheduleInputError("invalid_selector", "target create must be an object")
    return SessionCreateSpec(cwd=str(data.get("cwd") or ""), agent_preset=str(data.get("agent_preset") or ""))


def target_to_dict(target: Target) -> dict[str, Any]:
    if isinstance(target, SessionTarget):
        return {"kind": TARGET_SESSION, "session_id": target.session_id}
    if isinstance(target, NewOnceTarget):
        return {
            "kind": TARGET_NEW_ONCE,
            "session_id": target.session_id,
            "create": _create_dict(target.create),
        }
    return {"kind": TARGET_NEW, "create": _create_dict(target.create)}


def target_from_dict(data: Mapping[str, Any]) -> Target:
    kind = data.get("kind") or data.get("type")
    if kind == TARGET_SESSION:
        session_id = data.get("session_id") or data.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ScheduleInputError("invalid_selector", "session target requires session_id")
        return SessionTarget(session_id=session_id)
    if kind == TARGET_NEW_ONCE:
        session_id = data.get("session_id") or data.get("sessionId") or ""
        return NewOnceTarget(session_id=str(session_id), create=_create_from_dict(data.get("create")))
    if kind == TARGET_NEW:
        return NewTarget(create=_create_from_dict(data.get("create")))
    raise ScheduleInputError("invalid_selector", f"unknown target kind {kind!r}")


# -- delivery records ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """One successful delivery of a schedule occurrence."""

    scheduled_at: str
    delivered_at: str
    message_id: str = ""
    target_session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduledAt": self.scheduled_at,
            "deliveredAt": self.delivered_at,
            "messageId": self.message_id,
            "targetSessionId": self.target_session_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DeliveryReceipt:
        return cls(
            scheduled_at=str(data.get("scheduledAt") or data.get("scheduled_at") or ""),
            delivered_at=str(data.get("deliveredAt") or data.get("delivered_at") or ""),
            message_id=str(data.get("messageId") or data.get("message_id") or ""),
            target_session_id=str(data.get("targetSessionId") or data.get("target_session_id") or ""),
        )


# -- schedule record -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """One persisted scheduled task."""

    id: str
    title: str
    prompt: str
    kind: str
    timing: Timing
    scheduled_at: str
    status: str
    target: Target
    created_at: str
    updated_at: str
    last_delivery: DeliveryReceipt | None = None
    delivery_history: tuple[DeliveryReceipt, ...] = ()

    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def is_recurring(self) -> bool:
        return self.kind in RECURRING_KINDS


def record_to_dict(record: ScheduleRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "title": record.title,
        "prompt": record.prompt,
        "kind": record.kind,
        "timing": timing_to_dict(record.timing),
        "scheduledAt": record.scheduled_at,
        "status": record.status,
        "target": target_to_dict(record.target),
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
        "lastDelivery": record.last_delivery.to_dict() if record.last_delivery is not None else None,
        "deliveryHistory": [receipt.to_dict() for receipt in record.delivery_history],
    }


def record_from_dict(data: Mapping[str, Any]) -> ScheduleRecord:
    """Parse one persisted record; the caller wraps errors with the task id."""

    if not isinstance(data, Mapping):
        raise ScheduleInputError("invalid_selector", "schedule record must be an object")
    record_id = data.get("id")
    if not isinstance(record_id, str) or not record_id:
        raise ScheduleInputError("invalid_selector", "schedule record requires an id")
    title = data.get("title")
    prompt = data.get("prompt")
    if not isinstance(title, str) or not title:
        raise ScheduleInputError("invalid_selector", "schedule record requires a title")
    if not isinstance(prompt, str) or not prompt:
        raise ScheduleInputError("invalid_prompt", "schedule record requires a prompt")
    kind = data.get("kind")
    if kind not in KINDS:
        raise ScheduleInputError("invalid_rule", f"schedule {record_id!r} has unknown kind {kind!r}")
    timing_data = data.get("timing")
    if not isinstance(timing_data, Mapping):
        raise ScheduleInputError("invalid_rule", f"schedule {record_id!r} has no timing object")
    timing = timing_from_dict(kind, timing_data)
    scheduled_at = data.get("scheduledAt") or data.get("scheduled_at")
    if not isinstance(scheduled_at, str) or not scheduled_at:
        raise ScheduleInputError("invalid_rule", f"schedule {record_id!r} has no scheduledAt")
    status = data.get("status")
    if status not in STATUSES:
        raise ScheduleInputError("invalid_selector", f"schedule {record_id!r} has unknown status {status!r}")
    target_data = data.get("target")
    if not isinstance(target_data, Mapping):
        raise ScheduleInputError("invalid_selector", f"schedule {record_id!r} has no target object")
    target = target_from_dict(target_data)
    created_at = str(data.get("createdAt") or data.get("created_at") or "")
    updated_at = str(data.get("updatedAt") or data.get("updated_at") or "")
    if not created_at or not updated_at:
        raise ScheduleInputError("invalid_selector", f"schedule {record_id!r} is missing timestamps")
    last_delivery_data = data.get("lastDelivery")
    last_delivery = (
        DeliveryReceipt.from_dict(last_delivery_data) if isinstance(last_delivery_data, Mapping) else None
    )
    raw_history = data.get("deliveryHistory")
    history: list[DeliveryReceipt] = []
    if isinstance(raw_history, list):
        history = [DeliveryReceipt.from_dict(item) for item in raw_history if isinstance(item, Mapping)]
    return ScheduleRecord(
        id=record_id,
        title=title,
        prompt=prompt,
        kind=kind,
        timing=timing,
        scheduled_at=scheduled_at,
        status=status,
        target=target,
        created_at=created_at,
        updated_at=updated_at,
        last_delivery=last_delivery,
        delivery_history=tuple(history),
    )


__all__ = [
    "KINDS",
    "KIND_AFTER",
    "KIND_AT",
    "KIND_CRON",
    "KIND_DAILY",
    "KIND_EVERY",
    "KIND_RRULE",
    "KIND_WEEKLY",
    "MAX_AFTER_SECONDS",
    "MAX_COUNT",
    "MAX_EVERY_SECONDS",
    "MAX_TITLE_CHARS",
    "MIN_EVERY_SECONDS",
    "ONE_SHOT_KINDS",
    "RECURRING_KINDS",
    "STATUSES",
    "STATUS_ACTIVE",
    "STATUS_ENDED",
    "STATUS_PAUSED",
    "STORE_VERSION",
    "TARGET_KINDS",
    "TARGET_NEW",
    "TARGET_NEW_ONCE",
    "TARGET_SESSION",
    "AfterTiming",
    "AtTiming",
    "CronTiming",
    "DailyTiming",
    "DeliveryReceipt",
    "EveryTiming",
    "NewOnceTarget",
    "NewTarget",
    "RRuleTiming",
    "ScheduleError",
    "ScheduleInputError",
    "ScheduleRecord",
    "ScheduleStoreError",
    "SessionCreateSpec",
    "SessionTarget",
    "Target",
    "Timing",
    "WeeklyTiming",
    "record_from_dict",
    "record_to_dict",
    "target_from_dict",
    "target_kind",
    "target_session_id",
    "target_to_dict",
    "timing_from_dict",
    "timing_kind",
    "timing_time_zone",
    "timing_to_dict",
]
