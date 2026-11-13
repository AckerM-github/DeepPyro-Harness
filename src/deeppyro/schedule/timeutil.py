"""Time helpers for scheduled tasks.

Instants are stored as RFC3339 UTC strings with millisecond precision
(\"2026-11-14T01:00:00.000Z\").  Wall-clock rules (daily/weekly/rrule) are
evaluated in a time zone so daylight-saving transitions behave like the
reference implementation: nonexistent local times are skipped and ambiguous
local times resolve to the earlier instant (fold=0).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, tzinfo

from ._zones import resolve_zone, wall_to_utc
from .rrule import parse_rrule
from .types import (
    KIND_DAILY,
    KIND_WEEKLY,
    AfterTiming,
    AtTiming,
    CronTiming,
    DailyTiming,
    EveryTiming,
    RRuleTiming,
    ScheduleInputError,
    Timing,
    WeeklyTiming,
)

_INSTANT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$"
)
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,9}))?$")
_DAILY_SEARCH_DAYS = 366 * 100
_WEEKLY_SEARCH_WEEKS = 53 * 100


def parse_instant(text: str, *, field: str = "instant") -> datetime:
    """Parse an RFC3339 timestamp into an aware UTC datetime.

    A missing offset is interpreted as UTC so logs written on one machine stay
    readable on another; offsets and the Z suffix are otherwise respected.
    """
    if not isinstance(text, str):
        raise ScheduleInputError("invalid_rule", f"{field} must be an RFC3339 string")
    match = _INSTANT_RE.match(text.strip())
    if match is None:
        raise ScheduleInputError("invalid_rule", f"{field} must be an RFC3339 timestamp, got {text!r}")
    year, month, day, hour, minute, second = (int(value) for value in match.groups()[:6])
    fraction = match.group(7) or ""
    microsecond = int((fraction + "000000")[:6]) if fraction else 0
    offset = match.group(8)
    if year < 1:
        raise ScheduleInputError("invalid_rule", f"{field} year is out of range: {year}")
    try:
        value = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)
    except ValueError as exc:
        raise ScheduleInputError(
            "invalid_rule", f"{field} is not a valid timestamp: {text!r} ({exc})"
        ) from exc
    if offset and offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        offset_hours = int(offset[1:3])
        offset_minutes = int(offset[4:6])
        value = value.replace(tzinfo=None) - sign * timedelta(hours=offset_hours, minutes=offset_minutes)
        value = value.replace(tzinfo=UTC)
    return value


def format_instant(value: datetime) -> str:
    """Render an aware datetime as RFC3339 UTC with milliseconds."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def resolve_time_zone(name: str = "", *, default: str = "") -> tzinfo:
    """Resolve an explicit IANA zone, falling back to a default or system local."""
    return resolve_zone(name, default=default)


def validate_time_zone(name: str, *, default: str = "") -> None:
    resolve_time_zone(name, default=default)


def format_local(value: datetime, time_zone: str = "", *, default: str = "") -> str:
    """Render an instant in the given zone with an explicit UTC offset."""
    zone = resolve_time_zone(time_zone, default=default)
    return value.astimezone(zone).isoformat(timespec="seconds")


def parse_clock_time(text: str) -> tuple[int, int, int]:
    """Parse HH:MM[:SS[.fff]] into a normalized (hour, minute, second)."""
    if not isinstance(text, str):
        raise ScheduleInputError("invalid_rule", "time must be an HH:MM string")
    match = _CLOCK_RE.match(text.strip())
    if match is None:
        raise ScheduleInputError("invalid_rule", f"time must look like HH:MM or HH:MM:SS, got {text!r}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise ScheduleInputError("time_out_of_range", f"time is out of range: {text!r}")
    return hour, minute, second


def format_clock_time(hour: int, minute: int, second: int = 0) -> str:
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _wall_to_utc(zone: tzinfo, naive: datetime) -> datetime | None:
    """Convert a local wall time to UTC, or None when DST skipped it."""
    return wall_to_utc(zone, naive)


def _next_daily(timing: DailyTiming, now: datetime, default_time_zone: str) -> datetime:
    zone = resolve_time_zone(timing.time_zone, default=default_time_zone)
    hour, minute, second = parse_clock_time(timing.time)
    local_now = now.astimezone(zone)
    for offset in range(_DAILY_SEARCH_DAYS):
        day = local_now.date() + timedelta(days=offset)
        candidate = _wall_to_utc(zone, datetime(day.year, day.month, day.day, hour, minute, second))
        if candidate is not None and candidate > now:
            return candidate
    raise ScheduleInputError("not_future", "daily rule has no future occurrence")


def _next_weekly(timing: WeeklyTiming, now: datetime, default_time_zone: str) -> datetime:
    zone = resolve_time_zone(timing.time_zone, default=default_time_zone)
    hour, minute, second = parse_clock_time(timing.time)
    weekdays = set(timing.weekdays)
    local_now = now.astimezone(zone)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    for offset in range(_WEEKLY_SEARCH_WEEKS):
        week_start = monday + timedelta(weeks=offset)
        for weekday in sorted(weekdays):
            day = week_start + timedelta(days=weekday - 1)
            candidate = _wall_to_utc(zone, datetime(day.year, day.month, day.day, hour, minute, second))
            if candidate is not None and candidate > now:
                return candidate
    raise ScheduleInputError("not_future", "weekly rule has no future occurrence")


def first_occurrence(timing: Timing, *, now: datetime, default_time_zone: str = "") -> datetime:
    """Return the first occurrence strictly after \"now\" for a new schedule."""
    now = now.astimezone(UTC)
    if isinstance(timing, AfterTiming):
        return now + timedelta(seconds=timing.after_seconds)
    if isinstance(timing, AtTiming):
        at = parse_instant(timing.at, field="at")
        if at <= now:
            raise ScheduleInputError("not_future", f"at {format_instant(at)} is not in the future")
        return at
    if isinstance(timing, EveryTiming):
        return now + timedelta(seconds=timing.every_seconds)
    if isinstance(timing, DailyTiming):
        return _next_daily(timing, now, default_time_zone)
    if isinstance(timing, WeeklyTiming):
        return _next_weekly(timing, now, default_time_zone)
    if isinstance(timing, RRuleTiming):
        rule = parse_rrule(timing.rrule, default_time_zone=default_time_zone)
        occurrence = rule.first_after(now)
        if occurrence is None:
            raise ScheduleInputError("not_future", "rrule has no future occurrence")
        return occurrence
    raise ScheduleInputError("unsupported_timing", "cron timings cannot be executed in this build")


def following_occurrence(
    timing: Timing,
    *,
    previous: datetime,
    now: datetime,
    default_time_zone: str = "",
) -> datetime | None:
    """Return the next occurrence after \"now\" for an already-scheduled task.

    One-shot timings return None (the task ends after delivery).  Recurrence is
    realigned to the same wall-clock rule, and missed windows collapse into one
    delivery followed by the first future occurrence (reference semantics).
    """
    now = now.astimezone(UTC)
    previous = previous.astimezone(UTC)
    if isinstance(timing, EveryTiming):
        candidate = previous
        interval = timedelta(seconds=timing.every_seconds)
        while candidate <= now:
            candidate += interval
        return candidate
    if isinstance(timing, DailyTiming):
        return _next_daily(timing, now, default_time_zone)
    if isinstance(timing, WeeklyTiming):
        return _next_weekly(timing, now, default_time_zone)
    if isinstance(timing, RRuleTiming):
        rule = parse_rrule(timing.rrule, default_time_zone=default_time_zone)
        return rule.first_after(now)
    if isinstance(timing, CronTiming):
        return None
    return None


def occurrence_time_zone(timing: Timing, default_time_zone: str = "") -> str:
    """Explicit zone used for local display, or an empty string for local time.

    Recurrence rules carry their zone in the DTSTART TZID parameter instead of a
    separate field, so the rule text is parsed to recover it.
    """
    from .types import timing_time_zone

    zone = timing_time_zone(timing)
    if not zone and isinstance(timing, RRuleTiming):
        try:
            rule = parse_rrule(timing.rrule, default_time_zone=default_time_zone)
        except ScheduleInputError:
            rule = None
        if rule is not None and rule.tz is not UTC:
            zone = getattr(rule.tz, "key", "") or str(rule.tz)
    return zone or default_time_zone


__all__ = [
    "KIND_DAILY",
    "KIND_WEEKLY",
    "UTC",
    "first_occurrence",
    "following_occurrence",
    "format_clock_time",
    "format_instant",
    "format_local",
    "occurrence_time_zone",
    "parse_clock_time",
    "parse_instant",
    "resolve_time_zone",
    "validate_time_zone",
]
