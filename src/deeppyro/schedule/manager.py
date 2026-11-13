"""Scheduled task manager: persistence, one asyncio timer, delivery.

The manager owns the catalog and the timing rules and never touches sessions
directly.  A ScheduleHost implementation (the TUI runtime, the headless runtime
or a test double) checks sessions and performs delivery, so the manager can be
constructed before any agent runtime exists and attached later through
configure_host().

Each manager runs exactly one timer task.  It sleeps until the earliest
scheduled instant (capped at 60 seconds so wall-clock jumps and OS sleep are
picked up) and runs one due scan per wake.  A recurring task that missed
occurrences (process was down, machine asleep) is delivered once and then
advanced to the first occurrence in the future, matching the reference
implementation's catch-up semantics.  Delivery failures do not advance the
schedule; the next scan retries after a short back-off.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from .store import ScheduleStore
from .timeutil import (
    first_occurrence,
    following_occurrence,
    format_instant,
    format_local,
    occurrence_time_zone,
    parse_clock_time,
    parse_instant,
)
from .types import (
    KIND_AT,
    KIND_CRON,
    KIND_DAILY,
    KIND_RRULE,
    KIND_WEEKLY,
    MAX_AFTER_SECONDS,
    MAX_EVERY_SECONDS,
    MAX_TITLE_CHARS,
    MIN_EVERY_SECONDS,
    STATUS_ACTIVE,
    STATUS_ENDED,
    STATUS_PAUSED,
    TARGET_NEW,
    TARGET_NEW_ONCE,
    TARGET_SESSION,
    AfterTiming,
    AtTiming,
    CronTiming,
    DailyTiming,
    DeliveryReceipt,
    EveryTiming,
    NewOnceTarget,
    NewTarget,
    ScheduleError,
    ScheduleInputError,
    ScheduleRecord,
    ScheduleStoreError,
    SessionCreateSpec,
    SessionTarget,
    Timing,
    WeeklyTiming,
    target_session_id,
    target_to_dict,
    timing_from_dict,
    timing_kind,
    timing_to_dict,
)

log = logging.getLogger(__name__)

#: Request keys accepted as a shorthand for a timing object, in priority order.
_SHORTHAND_TIMING_KEYS: tuple[str, ...] = (
    "rrule",
    "cron",
    "weekly",
    "daily",
    "at",
    "every_seconds",
    "after_seconds",
)


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """Runtime limits loaded from the [schedule] configuration table."""

    delivery_history_days: int = 30
    delivery_history_records: int = 200
    new_session_interval_seconds: int = 60
    scan_interval_seconds: int = 5
    default_time_zone: str = ""

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> ScheduleConfig:
        data = raw or {}
        return cls(
            delivery_history_days=_config_int(data, "delivery_history_days", 30),
            delivery_history_records=_config_int(data, "delivery_history_records", 200),
            new_session_interval_seconds=_config_int(data, "new_session_interval_seconds", 60),
            scan_interval_seconds=_config_int(data, "scan_interval_seconds", 5, minimum=1),
            default_time_zone=str(data.get("default_time_zone") or ""),
        )


def _config_int(data: Mapping[str, Any], name: str, default: int, *, minimum: int = 0) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScheduleInputError("invalid_rule", f"schedule.{name} must be an integer")
    if value < minimum:
        raise ScheduleInputError("invalid_rule", f"schedule.{name} must be >= {minimum}")
    return value


@runtime_checkable
class ScheduleHost(Protocol):
    """The thin bridge between the schedule manager and a session runtime."""

    async def session_exists(self, session_id: str) -> bool:
        """Whether the target session can still receive messages."""
        ...

    async def delegation_depth(self, session_id: str) -> int:
        """Nesting depth of the calling session (0 for a user-facing session)."""
        ...

    async def deliver(self, task: ScheduleRecord, message: str) -> DeliveryReceipt | None:
        """Deliver one occurrence; return a receipt or None when the host does not need one."""
        ...


class NullScheduleHost:
    """Host used before the runtime attaches (and by tests): never delivers."""

    async def session_exists(self, session_id: str) -> bool:
        return False

    async def delegation_depth(self, session_id: str) -> int:
        return 0

    async def deliver(self, task: ScheduleRecord, message: str) -> DeliveryReceipt | None:
        return None


def _require_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScheduleInputError("invalid_rule", f"{field} must be an integer")
    return value


def _require_text(value: Any, *, field: str, limit: int = 0, code: str = "invalid_selector") -> str:
    if not isinstance(value, str):
        raise ScheduleInputError(code, f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ScheduleInputError(code, f"{field} must not be empty")
    if limit and len(text) > limit:
        raise ScheduleInputError(code, f"{field} must be at most {limit} characters")
    return text


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScheduleInputError("invalid_rule", f"{field} must be an object")
    return value


def _timing_from_request(request: Mapping[str, Any]) -> Timing:
    raw = request.get("timing")
    if raw is not None:
        data = _require_mapping(raw, field="timing")
        kind = data.get("kind") or data.get("type")
        if not isinstance(kind, str) or not kind:
            raise ScheduleInputError("invalid_rule", "timing object must declare a kind")
        return timing_from_dict(kind, data)
    for key in _SHORTHAND_TIMING_KEYS:
        if key not in request or request[key] is None:
            continue
        value = request[key]
        if key == "rrule":
            return timing_from_dict(KIND_RRULE, {"rrule": value})
        if key == "cron":
            if isinstance(value, str):
                return timing_from_dict(
                    KIND_CRON, {"expression": value, "time_zone": request.get("time_zone") or ""}
                )
            return timing_from_dict(KIND_CRON, dict(_require_mapping(value, field="cron")))
        if key == "weekly":
            return timing_from_dict(KIND_WEEKLY, dict(_require_mapping(value, field="weekly")))
        if key == "daily":
            return timing_from_dict(KIND_DAILY, dict(_require_mapping(value, field="daily")))
        if key == "at":
            return timing_from_dict(KIND_AT, {"at": value})
        if key == "every_seconds":
            return EveryTiming(every_seconds=_require_int(value, field="every_seconds"))
        if key == "after_seconds":
            return AfterTiming(after_seconds=_require_int(value, field="after_seconds"))
    kind = request.get("kind")
    if isinstance(kind, str) and kind:
        return timing_from_dict(kind, request)
    raise ScheduleInputError(
        "invalid_rule",
        "schedule requires a timing (rrule, after_seconds, every_seconds, at, daily, weekly or cron)",
    )


def _validate_timing(timing: Timing) -> None:
    if isinstance(timing, AfterTiming):
        if not 1 <= timing.after_seconds <= MAX_AFTER_SECONDS:
            raise ScheduleInputError(
                "invalid_rule", f"after_seconds must be between 1 and {MAX_AFTER_SECONDS}"
            )
    elif isinstance(timing, EveryTiming):
        if not MIN_EVERY_SECONDS <= timing.every_seconds <= MAX_EVERY_SECONDS:
            raise ScheduleInputError(
                "invalid_rule",
                f"every_seconds must be between {MIN_EVERY_SECONDS} and {MAX_EVERY_SECONDS}",
            )
    elif isinstance(timing, AtTiming):
        parse_instant(timing.at, field="at")
    elif isinstance(timing, DailyTiming):
        parse_clock_time(timing.time)
    elif isinstance(timing, WeeklyTiming):
        parse_clock_time(timing.time)
        days = list(timing.weekdays)
        if not days or any(day < 1 or day > 7 for day in days):
            raise ScheduleInputError(
                "invalid_rule", "weekly weekdays must be between 1 (Monday) and 7 (Sunday)"
            )
        if days != sorted(set(days)):
            raise ScheduleInputError("invalid_rule", "weekly weekdays must be sorted and unique")
    elif isinstance(timing, CronTiming) and len(timing.expression.split()) != 5:
        raise ScheduleInputError("invalid_rule", "cron expression must have five fields")


def _ensure_executable(timing: Timing) -> None:
    if isinstance(timing, CronTiming):
        raise ScheduleInputError(
            "unsupported_timing", "cron timings cannot be executed in this build; use rrule instead"
        )


def _create_spec_from(value: Any) -> SessionCreateSpec:
    if value is None:
        return SessionCreateSpec()
    data = _require_mapping(value, field="target create")
    cwd = data.get("cwd")
    preset = data.get("agent_preset")
    if preset is None:
        preset = data.get("agentPreset")
    return SessionCreateSpec(
        cwd="" if cwd is None else str(cwd),
        agent_preset="" if preset is None else str(preset),
    )


def _parse_target(raw: Any, actor_session_id: str | None) -> Any:
    if raw is None:
        if actor_session_id:
            return SessionTarget(session_id=actor_session_id)
        raise ScheduleInputError(
            "invalid_selector", "schedule target is required when there is no current session"
        )
    if isinstance(raw, str):
        if raw == "this_session":
            if not actor_session_id:
                raise ScheduleInputError("invalid_selector", "this_session target requires a current session")
            return SessionTarget(session_id=actor_session_id)
        if raw == TARGET_NEW_ONCE:
            return NewOnceTarget()
        if raw == TARGET_NEW:
            return NewTarget()
        raise ScheduleInputError("invalid_selector", f"unknown schedule target {raw!r}")
    data = _require_mapping(raw, field="target")
    kind = data.get("kind") or data.get("type")
    if kind == "this_session":
        if not actor_session_id:
            raise ScheduleInputError("invalid_selector", "this_session target requires a current session")
        return SessionTarget(session_id=actor_session_id)
    if kind == TARGET_SESSION:
        session_id = data.get("session_id") or data.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ScheduleInputError("invalid_selector", "session target requires a session_id")
        return SessionTarget(session_id=session_id)
    if kind == TARGET_NEW_ONCE:
        return NewOnceTarget(
            session_id=str(data.get("session_id") or ""), create=_create_spec_from(data.get("create"))
        )
    if kind == TARGET_NEW:
        return NewTarget(create=_create_spec_from(data.get("create")))
    raise ScheduleInputError("invalid_selector", f"unknown schedule target kind {kind!r}")


class ScheduleManager:
    """Async facade over the schedule catalog and the delivery timer."""

    def __init__(
        self,
        store: ScheduleStore | None = None,
        *,
        host: ScheduleHost | None = None,
        config: ScheduleConfig | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store if store is not None else ScheduleStore()
        self.config = config if config is not None else ScheduleConfig()
        self._now = now_fn if now_fn is not None else (lambda: datetime.now(UTC))
        self._tasks: dict[str, ScheduleRecord] = {}
        self._host: ScheduleHost | None = host
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._stopping = False
        self._retry_after: datetime | None = None
        try:
            self._tasks = self.store.load()
        except ScheduleStoreError as exc:
            log.warning("cannot load schedule store: %s", exc)
            try:
                self.store.quarantine()
            except ScheduleStoreError as quarantine_error:
                log.warning("cannot quarantine corrupted schedule store: %s", quarantine_error)
            self._tasks = {}

    # -- lifecycle ---------------------------------------------------------

    @property
    def host(self) -> ScheduleHost | None:
        return self._host

    def configure_host(self, host: ScheduleHost | None) -> None:
        """Attach the runtime bridge and re-evaluate pending deliveries."""
        self._host = host
        self._retry_after = None
        self._wake.set()

    @property
    def records(self) -> tuple[ScheduleRecord, ...]:
        return tuple(sorted(self._tasks.values(), key=lambda record: (record.scheduled_at, record.id)))

    async def start(self) -> None:
        """Start the single delivery timer (idempotent)."""
        if self._runner is not None and not self._runner.done():
            return
        self._stopping = False
        self._runner = asyncio.create_task(self._run(), name="deeppyro-schedule-timer")

    async def stop(self) -> None:
        """Stop the delivery timer; safe to call more than once."""
        self._stopping = True
        self._wake.set()
        runner = self._runner
        self._runner = None
        if runner is None or runner.done():
            return
        try:
            await asyncio.wait_for(runner, timeout=5.0)
        except TimeoutError:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    async def _run(self) -> None:
        try:
            while not self._stopping:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._next_delay())
                except TimeoutError:
                    pass
                self._wake.clear()
                if self._stopping:
                    break
                try:
                    await self.tick()
                except Exception:
                    log.exception("schedule timer tick failed")
        finally:
            self._wake.clear()

    def _now_utc(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _next_delay(self) -> float:
        scan = float(max(self.config.scan_interval_seconds, 1))
        if self._host is None:
            return scan
        now = self._now_utc()
        if self._retry_after is not None and self._retry_after > now:
            return min((self._retry_after - now).total_seconds(), 60.0)
        instants: list[datetime] = []
        for record in self._tasks.values():
            if record.status != STATUS_ACTIVE:
                continue
            try:
                instants.append(parse_instant(record.scheduled_at))
            except ScheduleError:
                continue
        if not instants:
            return scan
        delay = (min(instants) - now).total_seconds()
        if delay <= 0:
            return 0.0
        return min(delay, 60.0)

    # -- delivery ----------------------------------------------------------

    async def tick(self, *, now: datetime | None = None) -> list[DeliveryReceipt]:
        """Deliver every due task once; returns the receipts (deterministic for tests)."""
        async with self._lock:
            current = now if now is not None else self._now_utc()
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
            current = current.astimezone(UTC)
            if self._host is None:
                return []
            due: list[tuple[datetime, ScheduleRecord]] = []
            for record in self._tasks.values():
                if record.status != STATUS_ACTIVE:
                    continue
                try:
                    scheduled = parse_instant(record.scheduled_at)
                except ScheduleError:
                    continue
                if scheduled <= current:
                    due.append((scheduled, record))
            due.sort(key=lambda item: (item[0], item[1].id))
            receipts: list[DeliveryReceipt] = []
            changed = False
            failed = False
            for _scheduled, record in due:
                receipt, state_changed = await self._deliver(record, current)
                if state_changed:
                    changed = True
                if receipt is not None:
                    receipts.append(receipt)
                else:
                    failed = True
            if changed:
                self._persist(strict=False)
            if failed:
                self._retry_after = current + timedelta(seconds=max(self.config.scan_interval_seconds, 1))
            return receipts

    async def _deliver(
        self, record: ScheduleRecord, current: datetime
    ) -> tuple[DeliveryReceipt | None, bool]:
        host = self._host
        if host is None:
            return None, False
        target_session = target_session_id(record.target)
        if isinstance(record.target, SessionTarget):
            try:
                exists = await host.session_exists(record.target.session_id)
            except Exception:
                log.warning("schedule session lookup failed for %s", record.id, exc_info=True)
                return None, False
            if not exists:
                ended = replace(record, status=STATUS_ENDED, updated_at=format_instant(current))
                self._tasks[ended.id] = ended
                log.info(
                    "schedule %s ended because session %s no longer exists",
                    record.id,
                    record.target.session_id,
                )
                return None, True
        message = self.render_message(record)
        try:
            receipt = await host.deliver(record, message)
        except Exception:
            log.warning("schedule delivery of %s failed", record.id, exc_info=True)
            return None, False
        if receipt is None:
            receipt = DeliveryReceipt(
                scheduled_at=record.scheduled_at,
                delivered_at=format_instant(current),
                target_session_id=target_session or "",
            )
        elif not receipt.scheduled_at:
            receipt = replace(receipt, scheduled_at=record.scheduled_at)
        history = self._trim_history((*record.delivery_history, receipt), current)
        updated = replace(
            record,
            last_delivery=receipt,
            delivery_history=history,
            updated_at=format_instant(current),
        )
        try:
            previous = parse_instant(record.scheduled_at)
        except ScheduleError:
            previous = current
        try:
            next_at = following_occurrence(
                record.timing,
                previous=previous,
                now=current,
                default_time_zone=self.config.default_time_zone,
            )
        except ScheduleError:
            log.warning("schedule %s has an invalid rule; ending it", record.id, exc_info=True)
            next_at = None
        if next_at is None:
            updated = replace(updated, status=STATUS_ENDED)
        else:
            updated = replace(updated, scheduled_at=format_instant(next_at))
        self._tasks[updated.id] = updated
        return receipt, True

    def _trim_history(
        self, history: tuple[DeliveryReceipt, ...], now: datetime
    ) -> tuple[DeliveryReceipt, ...]:
        entries = list(history)
        days = self.config.delivery_history_days
        if days > 0:
            cutoff = now - timedelta(days=days)
            kept: list[DeliveryReceipt] = []
            for receipt in entries:
                try:
                    delivered = parse_instant(receipt.delivered_at)
                except ScheduleError:
                    kept.append(receipt)
                    continue
                if delivered >= cutoff:
                    kept.append(receipt)
            entries = kept
        limit = self.config.delivery_history_records
        if limit > 0 and len(entries) > limit:
            entries = entries[-limit:]
        return tuple(entries)

    def _persist(self, *, strict: bool) -> None:
        try:
            self.store.save(self._tasks)
        except ScheduleStoreError:
            log.exception("cannot persist schedule store %s", self.store.path)
            if strict:
                raise

    # -- catalog -----------------------------------------------------------

    async def create(
        self, request: Mapping[str, Any], *, actor_session_id: str | None = None
    ) -> ScheduleRecord:
        async with self._lock:
            now = self._now_utc()
            title = _require_text(
                request.get("title"), field="title", limit=MAX_TITLE_CHARS, code="invalid_selector"
            )
            prompt = _require_text(request.get("prompt"), field="prompt", code="invalid_prompt")
            timing = _timing_from_request(request)
            _validate_timing(timing)
            _ensure_executable(timing)
            target = _parse_target(request.get("target"), actor_session_id)
            scheduled = first_occurrence(timing, now=now, default_time_zone=self.config.default_time_zone)
            stamp = format_instant(now)
            record = ScheduleRecord(
                id=f"schedule-{uuid4().hex}",
                title=title,
                prompt=prompt,
                kind=timing_kind(timing),
                timing=timing,
                scheduled_at=format_instant(scheduled),
                status=STATUS_ACTIVE,
                target=target,
                created_at=stamp,
                updated_at=stamp,
            )
            self._tasks[record.id] = record
            self._persist(strict=True)
            self._wake.set()
            return record

    async def list(self, *, active: bool | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        if limit < 1 or limit > 500:
            raise ScheduleInputError("invalid_selector", "limit must be between 1 and 500")
        if offset < 0:
            raise ScheduleInputError("invalid_selector", "offset must not be negative")
        records = list(self.records)
        if active is not None:
            if active:
                records = [record for record in records if record.status == STATUS_ACTIVE]
            else:
                records = [record for record in records if record.status != STATUS_ACTIVE]
        total = len(records)
        page = records[offset : offset + limit]
        return {
            "tasks": [self.task_model(record) for record in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def view(self, task_id: str) -> ScheduleRecord | None:
        return self._tasks.get(str(task_id))

    async def update(
        self,
        task_id: str,
        patch: Mapping[str, Any],
        *,
        expected_updated_at: str | None = None,
        actor_session_id: str | None = None,
    ) -> ScheduleRecord:
        async with self._lock:
            record = self._tasks.get(str(task_id))
            if record is None:
                raise ScheduleInputError("unknown_task", f"schedule {task_id!r} does not exist")
            if expected_updated_at is not None and expected_updated_at != record.updated_at:
                raise ScheduleInputError(
                    "schedule_conflict",
                    f"schedule {task_id!r} changed since updatedAt {expected_updated_at!r}",
                )
            if not isinstance(patch, Mapping):
                raise ScheduleInputError("invalid_selector", "schedule update must be an object")
            now = self._now_utc()
            changes: dict[str, Any] = {}
            if "title" in patch:
                changes["title"] = _require_text(
                    patch.get("title"), field="title", limit=MAX_TITLE_CHARS, code="invalid_selector"
                )
            if "prompt" in patch:
                changes["prompt"] = _require_text(patch.get("prompt"), field="prompt", code="invalid_prompt")
            new_timing: Timing | None = None
            if patch.get("timing") is not None or any(
                patch.get(key) is not None for key in _SHORTHAND_TIMING_KEYS
            ):
                new_timing = _timing_from_request(patch)
                _validate_timing(new_timing)
                _ensure_executable(new_timing)
                scheduled = first_occurrence(
                    new_timing, now=now, default_time_zone=self.config.default_time_zone
                )
                changes["timing"] = new_timing
                changes["kind"] = timing_kind(new_timing)
                changes["scheduled_at"] = format_instant(scheduled)
            if patch.get("target") is not None:
                changes["target"] = _parse_target(patch.get("target"), actor_session_id)
            active = patch.get("active")
            if active is not None:
                if not isinstance(active, bool):
                    raise ScheduleInputError("invalid_rule", "active must be a boolean")
                if active:
                    changes["status"] = STATUS_ACTIVE
                    if new_timing is None and record.status == STATUS_PAUSED:
                        try:
                            previous = parse_instant(record.scheduled_at)
                        except ScheduleError:
                            previous = now
                        if record.is_recurring():
                            try:
                                next_at = following_occurrence(
                                    record.timing,
                                    previous=previous,
                                    now=now,
                                    default_time_zone=self.config.default_time_zone,
                                )
                            except ScheduleError:
                                next_at = None
                            if next_at is None:
                                changes["status"] = STATUS_ENDED
                            else:
                                changes["scheduled_at"] = format_instant(next_at)
                else:
                    changes["status"] = STATUS_PAUSED
            elif new_timing is not None and record.status != STATUS_ACTIVE:
                changes["status"] = STATUS_ACTIVE
            if not changes:
                raise ScheduleInputError(
                    "invalid_selector", "schedule update requires at least one changed field"
                )
            changes["updated_at"] = format_instant(now)
            updated = replace(record, **changes)
            self._tasks[updated.id] = updated
            self._persist(strict=True)
            self._wake.set()
            return updated

    async def delete(self, task_id: str) -> bool:
        async with self._lock:
            existing = self._tasks.pop(str(task_id), None)
            if existing is None:
                return False
            self._persist(strict=True)
            self._wake.set()
            return True

    # -- reporting ---------------------------------------------------------

    def _local_time(self, record: ScheduleRecord) -> str:
        try:
            zone = occurrence_time_zone(record.timing, self.config.default_time_zone)
            return format_local(parse_instant(record.scheduled_at), zone)
        except ScheduleError:
            return record.scheduled_at

    def task_model(self, record: ScheduleRecord) -> dict[str, Any]:
        """Camel-cased task object returned by the tool and the inspect surfaces."""
        return {
            "id": record.id,
            "title": record.title,
            "prompt": record.prompt,
            "kind": record.kind,
            "timing": timing_to_dict(record.timing),
            "target": target_to_dict(record.target),
            "status": record.status,
            "scheduledAt": record.scheduled_at,
            "scheduledAtLocal": self._local_time(record),
            "timeZone": occurrence_time_zone(record.timing, self.config.default_time_zone),
            "createdAt": record.created_at,
            "updatedAt": record.updated_at,
            "lastDelivery": record.last_delivery.to_dict() if record.last_delivery else None,
            "deliveryHistory": [receipt.to_dict() for receipt in record.delivery_history],
        }

    def render_message(self, record: ScheduleRecord) -> str:
        """Message text handed to the host for one occurrence."""
        local = self._local_time(record)
        return f"Scheduled task: {record.title} (occurrence {local})\n\n{record.prompt}"

    def next_wakeup(self) -> str | None:
        instants: list[datetime] = []
        for record in self._tasks.values():
            if record.status != STATUS_ACTIVE:
                continue
            try:
                instants.append(parse_instant(record.scheduled_at))
            except ScheduleError:
                continue
        return format_instant(min(instants)) if instants else None

    async def delegation_depth(self, session_id: str) -> int:
        host = self._host
        if host is None or not session_id:
            return 0
        try:
            return int(await host.delegation_depth(session_id))
        except Exception:
            log.warning("schedule delegation depth lookup failed", exc_info=True)
            return 0


__all__ = ["NullScheduleHost", "ScheduleConfig", "ScheduleHost", "ScheduleManager"]
