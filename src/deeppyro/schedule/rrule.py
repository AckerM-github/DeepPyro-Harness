"""Minimal RFC 5545 recurrence rules for scheduled tasks.

DeepPyro ships its own small RRULE engine instead of delegating to a
dateutil-style library: the schedule feature needs a strict, validated subset
of RRULE that can be checked before a task is stored and expanded
deterministically afterwards. The supported subset is cross-checked against
python-dateutil in the tests, but the runtime has no dateutil dependency.
A rule is exactly two lines of text::

    DTSTART;TZID=Asia/Shanghai:20260901T080000
    RRULE:FREQ=DAILY;INTERVAL=2;COUNT=5

Supported parts: FREQ (DAILY/WEEKLY/MONTHLY/YEARLY), INTERVAL, COUNT, UNTIL,
WKST, BYDAY, BYMONTHDAY, BYMONTH, BYHOUR, BYMINUTE and a single BYSECOND.
BYYEARDAY, BYWEEKNO, BYSETPOS, sub-daily frequencies and multiple BYSECOND
values are rejected (they are outside the supported subset, see
docs/spec/schedule.md for the exact grammar and the documented deviations).

Expansion notes:

* occurrences are computed by iterating whole periods from DTSTART, so the
  DTSTART occurrence itself counts towards COUNT when it matches the pattern
  (RFC 5545 behaviour);
* wall-clock times are converted through the rule's time zone, nonexistent
  local times are skipped, ambiguous ones resolve to the earlier instant;
* expansion is bounded to 100 years of periods, which is the documented
  horizon (rules with no COUNT/UNTIL past that horizon simply end).
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

from ._zones import resolve_zone, wall_to_utc
from .types import MAX_COUNT, ScheduleInputError

_DAY_NAMES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_SUPPORTED_FREQS = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})
_SUBDAILY_FREQS = frozenset({"SECONDLY", "MINUTELY", "HOURLY"})
_UNSUPPORTED_PARTS = frozenset({"BYYEARDAY", "BYWEEKNO", "BYSETPOS"})
_KNOWN_PARTS = frozenset(
    {
        "FREQ",
        "INTERVAL",
        "COUNT",
        "UNTIL",
        "WKST",
        "BYDAY",
        "BYMONTHDAY",
        "BYMONTH",
        "BYHOUR",
        "BYMINUTE",
        "BYSECOND",
    }
)
_MAX_PERIOD_CANDIDATES = 10_080
_MAX_PERIODS = {"DAILY": 366 * 100, "WEEKLY": 53 * 100, "MONTHLY": 1200, "YEARLY": 200}
_DTSTART_RE = re.compile(
    r"^DTSTART(?P<params>(?:;[A-Za-z][A-Za-z0-9-]*=[^;:]*)*):(?P<value>[^:]+)$",
    re.IGNORECASE,
)
_RRULE_RE = re.compile(r"^RRULE:(?P<body>.+)$", re.IGNORECASE)
_VALUE_RE = re.compile(r"^(?P<date>\d{8})T(?P<time>\d{6})(?P<z>Z?)$", re.IGNORECASE)
_BYDAY_RE = re.compile(r"^(?P<ordinal>[+-]?\d{1,2})?(?P<day>MO|TU|WE|TH|FR|SA|SU)$", re.IGNORECASE)
_UNTIL_RE = re.compile(r"^(?P<date>\d{8})T(?P<time>\d{6})Z$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RRule:
    """A validated recurrence rule."""

    dtstart_utc: datetime
    dtstart_local: datetime
    tz: tzinfo
    freq: str
    interval: int = 1
    count: int | None = None
    until: datetime | None = None
    wkst: int = 0
    by_day: tuple[tuple[int | None, int], ...] = ()
    by_month_day: tuple[int, ...] = ()
    by_month: tuple[int, ...] = ()
    by_hour: tuple[int, ...] = ()
    by_minute: tuple[int, ...] = ()
    by_second: tuple[int, ...] = ()

    def occurrences(self) -> Iterator[datetime]:
        """Yield occurrences in ascending order (bounded to the rule horizon)."""
        times = tuple(
            (hour, minute, second)
            for hour in (self.by_hour or (self.dtstart_local.hour,))
            for minute in (self.by_minute or (self.dtstart_local.minute,))
            for second in (self.by_second or (self.dtstart_local.second,))
        )
        emitted = 0
        for period in range(_MAX_PERIODS[self.freq]):
            days = self._days_for(period)
            if days is None:
                return
            candidates: list[datetime] = []
            for day in days:
                for hour, minute, second in times:
                    utc = wall_to_utc(
                        self.tz,
                        datetime(day.year, day.month, day.day, hour, minute, second),
                    )
                    if utc is None or utc < self.dtstart_utc:
                        continue
                    if self.until is not None and utc > self.until:
                        continue
                    candidates.append(utc)
            for occurrence in sorted(set(candidates)):
                if self.count is not None and emitted >= self.count:
                    return
                emitted += 1
                yield occurrence

    def first_after(self, instant: datetime) -> datetime | None:
        """Return the first occurrence strictly after the given instant."""
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        instant = instant.astimezone(UTC)
        for occurrence in self.occurrences():
            if occurrence > instant:
                return occurrence
        return None

    def _days_for(self, period: int) -> list[date] | None:
        """Dates covered by one rule period, or None past the supported horizon."""
        if self.freq == "DAILY":
            day = self.dtstart_local.date() + timedelta(days=period * self.interval)
            if day.year > 9999:
                return None
            if self.by_month and day.month not in self.by_month:
                return []
            weekday_set = {weekday for ordinal, weekday in self.by_day if ordinal is None}
            if weekday_set and day.weekday() not in weekday_set:
                return []
            return [day]
        if self.freq == "WEEKLY":
            base = self.dtstart_local.date()
            start = base - timedelta(days=(base.weekday() - self.wkst) % 7)
            start += timedelta(weeks=period * self.interval)
            if start.year > 9999:
                return None
            weekday_set = {weekday for ordinal, weekday in self.by_day if ordinal is None}
            if not weekday_set:
                weekday_set = {self.dtstart_local.weekday()}
            days: list[date] = []
            for index in range(7):
                day = start + timedelta(days=index)
                if day.weekday() not in weekday_set:
                    continue
                if self.by_month and day.month not in self.by_month:
                    continue
                days.append(day)
            return days
        if self.freq == "MONTHLY":
            month_index = (
                (self.dtstart_local.year - 1) * 12 + (self.dtstart_local.month - 1) + period * self.interval
            )
            if month_index < 0:
                return []
            year, month = divmod(month_index, 12)
            year += 1
            month += 1
            if year > 9999:
                return None
            if self.by_month and month not in self.by_month:
                return []
            return self._month_days(year, month)
        year = self.dtstart_local.year + period * self.interval
        if year > 9999:
            return None
        if self.by_day and all(ordinal is not None for ordinal, _ in self.by_day) and not self.by_month:
            days = []
            for ordinal, weekday in self.by_day:
                day = _nth_weekday_of_year(year, weekday, ordinal)
                if day is not None:
                    days.append(day)
            return sorted(set(days))
        months: tuple[int, ...]
        if self.by_month:
            months = self.by_month
        elif self.by_month_day or any(ordinal is None for ordinal, _ in self.by_day):
            months = tuple(range(1, 13))
        else:
            months = (self.dtstart_local.month,)
        days = []
        for month in months:
            days.extend(self._month_days(year, month))
        return sorted(set(days))

    def _month_days(self, year: int, month: int) -> list[date]:
        """Dates selected inside one month by BYMONTHDAY / BYDAY / DTSTART day."""
        days_in_month = calendar.monthrange(year, month)[1]
        numbers: set[int] = set()
        if self.by_month_day:
            for item in self.by_month_day:
                if item > 0:
                    if item <= days_in_month:
                        numbers.add(item)
                else:
                    candidate = days_in_month + item + 1
                    if 1 <= candidate <= days_in_month:
                        numbers.add(candidate)
        plain = {weekday for ordinal, weekday in self.by_day if ordinal is None}
        if plain:
            first = date(year, month, 1)
            for offset in range(days_in_month):
                day = first + timedelta(days=offset)
                if day.weekday() in plain:
                    numbers.add(day.day)
        for ordinal, weekday in self.by_day:
            if ordinal is None:
                continue
            day = _nth_weekday_of_month(year, month, weekday, ordinal)
            if day is not None:
                numbers.add(day.day)
        if not self.by_month_day and not self.by_day and self.dtstart_local.day <= days_in_month:
            numbers.add(self.dtstart_local.day)
        return [date(year, month, day) for day in sorted(numbers)]


def _nth_weekday_of_month(year: int, month: int, weekday: int, ordinal: int) -> date | None:
    days_in_month = calendar.monthrange(year, month)[1]
    if ordinal > 0:
        first = date(year, month, 1)
        day_number = 1 + (weekday - first.weekday()) % 7 + (ordinal - 1) * 7
        if day_number > days_in_month:
            return None
        return date(year, month, day_number)
    last = date(year, month, days_in_month)
    day_number = days_in_month - (last.weekday() - weekday) % 7 - (abs(ordinal) - 1) * 7
    if day_number < 1:
        return None
    return date(year, month, day_number)


def _nth_weekday_of_year(year: int, weekday: int, ordinal: int) -> date | None:
    if ordinal > 0:
        first = date(year, 1, 1)
        day = first + timedelta(days=(weekday - first.weekday()) % 7 + (ordinal - 1) * 7)
        return day if day.year == year else None
    last = date(year, 12, 31)
    day = last - timedelta(days=(last.weekday() - weekday) % 7 + (abs(ordinal) - 1) * 7)
    return day if day.year == year else None


def parse_rrule(text: str, *, default_time_zone: str = "") -> RRule:
    """Parse and validate a two-line DTSTART/RRULE text rule."""
    if not isinstance(text, str):
        raise ScheduleInputError("invalid_rule", "rrule must be a two-line DTSTART/RRULE string")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 2:
        raise ScheduleInputError(
            "invalid_rule",
            "rrule must be exactly two non-empty lines: DTSTART then RRULE",
        )
    if not lines[0].upper().startswith("DTSTART"):
        raise ScheduleInputError("invalid_rule", "the first rrule line must start with DTSTART")
    if not lines[1].upper().startswith("RRULE"):
        raise ScheduleInputError("invalid_rule", "the second rrule line must start with RRULE")
    dtstart_utc, dtstart_local, zone = _parse_dtstart(lines[0], default_time_zone)
    parts = _parse_parts(lines[1])
    freq = _parse_freq(parts)
    for key in parts:
        if key in _UNSUPPORTED_PARTS:
            raise ScheduleInputError("invalid_rule", f"RRULE part {key} is not supported")
        if key not in _KNOWN_PARTS:
            raise ScheduleInputError("invalid_rule", f"unknown RRULE part {key!r}")
    interval = _parse_int(parts.get("INTERVAL"), name="INTERVAL", minimum=1, maximum=100_000, default=1)
    count = _parse_int(parts.get("COUNT"), name="COUNT", minimum=1, maximum=MAX_COUNT, default=None)
    if parts.get("COUNT") is not None and count is None:
        raise ScheduleInputError("invalid_rule", "COUNT must be an integer")
    until = _parse_until(parts.get("UNTIL"))
    if count is not None and until is not None:
        raise ScheduleInputError("invalid_rule", "COUNT and UNTIL cannot be combined")
    wkst = _parse_wkst(parts.get("WKST"), freq)
    by_day = _parse_byday(parts.get("BYDAY"), freq)
    by_month_day = _parse_month_days(parts.get("BYMONTHDAY"), freq)
    by_month = _parse_int_list(parts.get("BYMONTH"), name="BYMONTH", minimum=1, maximum=12)
    by_hour = _parse_int_list(parts.get("BYHOUR"), name="BYHOUR", minimum=0, maximum=23)
    by_minute = _parse_int_list(parts.get("BYMINUTE"), name="BYMINUTE", minimum=0, maximum=59)
    by_second = _parse_int_list(parts.get("BYSECOND"), name="BYSECOND", minimum=0, maximum=59)
    if len(by_second) > 1:
        raise ScheduleInputError("invalid_rule", "only one BYSECOND value is supported")
    _check_frequency(freq, by_day, by_month_day, by_hour, by_minute, by_second)
    return RRule(
        dtstart_utc=dtstart_utc,
        dtstart_local=dtstart_local,
        tz=zone,
        freq=freq,
        interval=interval,
        count=count,
        until=until,
        wkst=wkst,
        by_day=by_day,
        by_month_day=by_month_day,
        by_month=by_month,
        by_hour=by_hour,
        by_minute=by_minute,
        by_second=by_second,
    )


def _parse_dtstart(line: str, default_time_zone: str) -> tuple[datetime, datetime, tzinfo]:
    match = _DTSTART_RE.match(line)
    if match is None:
        raise ScheduleInputError(
            "invalid_rule",
            "DTSTART must look like DTSTART[;TZID=Zone]:YYYYMMDDTHHMMSS[Z]",
        )
    tzid = ""
    for chunk in match.group("params").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        if key.strip().upper() != "TZID" or not value.strip():
            raise ScheduleInputError("invalid_rule", f"unsupported DTSTART parameter {chunk!r}")
        if tzid:
            raise ScheduleInputError("invalid_rule", "DTSTART may declare only one TZID")
        tzid = value.strip()
    value = match.group("value").strip()
    value_match = _VALUE_RE.match(value)
    if value_match is None:
        raise ScheduleInputError(
            "invalid_rule", f"DTSTART value {value!r} is not a YYYYMMDDTHHMMSS[Z] timestamp"
        )
    has_zulu = bool(value_match.group("z"))
    if tzid and has_zulu:
        raise ScheduleInputError("invalid_rule", "DTSTART with TZID must not use the Z suffix")
    naive = _parse_value(value_match, field="DTSTART")
    if has_zulu:
        utc = naive.replace(tzinfo=UTC)
        return utc, naive, UTC
    zone = resolve_zone(tzid, default=default_time_zone)
    utc = wall_to_utc(zone, naive)
    if utc is None:
        raise ScheduleInputError(
            "invalid_rule",
            f"DTSTART {value!r} is not a real local time in {zone}",
        )
    return utc, naive, zone


def _parse_value(match: re.Match[str], *, field: str) -> datetime:
    raw_date = match.group("date")
    raw_time = match.group("time")
    try:
        return datetime(
            int(raw_date[0:4]),
            int(raw_date[4:6]),
            int(raw_date[6:8]),
            int(raw_time[0:2]),
            int(raw_time[2:4]),
            int(raw_time[4:6]),
        )
    except ValueError as exc:
        raise ScheduleInputError(
            "invalid_rule", f"{field} is not a valid timestamp: {match.group(0)!r}"
        ) from exc


def _parse_parts(line: str) -> dict[str, str]:
    match = _RRULE_RE.match(line.strip())
    if match is None:
        raise ScheduleInputError("invalid_rule", "the second rrule line must look like RRULE:FREQ=...")
    body = match.group("body")
    parts: dict[str, str] = {}
    for chunk in body.split(";"):
        chunk = chunk.strip()
        if not chunk:
            raise ScheduleInputError("invalid_rule", "RRULE contains an empty part")
        key, separator, value = chunk.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if not separator or not key or not value:
            raise ScheduleInputError("invalid_rule", f"RRULE part {chunk!r} is invalid")
        if key in parts:
            raise ScheduleInputError("invalid_rule", f"duplicate RRULE part {key}")
        parts[key] = value
    return parts


def _parse_freq(parts: dict[str, str]) -> str:
    raw = parts.get("FREQ")
    if not raw:
        raise ScheduleInputError("invalid_rule", "RRULE requires FREQ")
    freq = raw.strip().upper()
    if freq in _SUBDAILY_FREQS:
        raise ScheduleInputError("invalid_rule", f"{freq} frequencies are not supported")
    if freq not in _SUPPORTED_FREQS:
        raise ScheduleInputError(
            "invalid_rule",
            f"unsupported FREQ {freq!r}; supported values are DAILY, WEEKLY, MONTHLY and YEARLY",
        )
    return freq


def _parse_int(
    raw: str | None,
    *,
    name: str,
    minimum: int,
    maximum: int,
    default: int | None,
    code: str = "invalid_rule",
) -> int | None:
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ScheduleInputError(code, f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum or value > maximum:
        raise ScheduleInputError(code, f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _parse_int_list(
    raw: str | None,
    *,
    name: str,
    minimum: int,
    maximum: int,
    allow_negative: bool = False,
) -> tuple[int, ...]:
    if raw is None:
        return ()
    values: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            raise ScheduleInputError("invalid_rule", f"{name} contains an empty value")
        try:
            value = int(piece)
        except ValueError as exc:
            raise ScheduleInputError("invalid_rule", f"{name} value {piece!r} is not an integer") from exc
        if not allow_negative and value < 0:
            raise ScheduleInputError("invalid_rule", f"{name} value {value} must not be negative")
        if value == 0 and allow_negative:
            raise ScheduleInputError("invalid_rule", f"{name} value must not be zero")
        if value < minimum or value > maximum:
            raise ScheduleInputError(
                "invalid_rule",
                f"{name} value {value} is outside the supported range {minimum}..{maximum}",
            )
        values.add(value)
    return tuple(sorted(values))


def _parse_until(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    match = _UNTIL_RE.match(raw.strip())
    if match is None:
        raise ScheduleInputError("invalid_rule", "UNTIL must be a UTC timestamp like 20260101T000000Z")
    return _parse_value(match, field="UNTIL").replace(tzinfo=UTC)


def _parse_wkst(raw: str | None, freq: str) -> int:
    if raw is None:
        return 0
    day = raw.strip().upper()
    if day not in _DAY_NAMES:
        raise ScheduleInputError("invalid_rule", f"WKST value {raw!r} is invalid")
    if freq != "WEEKLY":
        raise ScheduleInputError("invalid_rule", "WKST is only supported with FREQ=WEEKLY")
    return _DAY_NAMES[day]


def _parse_byday(raw: str | None, freq: str) -> tuple[tuple[int | None, int], ...]:
    if raw is None:
        return ()
    entries: list[tuple[int | None, int]] = []
    for piece in raw.split(","):
        piece = piece.strip().upper()
        match = _BYDAY_RE.match(piece)
        if match is None:
            raise ScheduleInputError("invalid_rule", f"BYDAY value {piece!r} is invalid")
        ordinal_text = match.group("ordinal")
        weekday = _DAY_NAMES[match.group("day")]
        ordinal: int | None = None
        if ordinal_text:
            ordinal = int(ordinal_text)
            if ordinal == 0:
                raise ScheduleInputError("invalid_rule", "BYDAY ordinal must not be zero")
            if freq not in {"MONTHLY", "YEARLY"}:
                raise ScheduleInputError("invalid_rule", "numeric BYDAY requires FREQ=MONTHLY or FREQ=YEARLY")
            if abs(ordinal) > 53:
                raise ScheduleInputError("invalid_rule", "BYDAY ordinal is out of range")
        entry = (ordinal, weekday)
        if entry not in entries:
            entries.append(entry)
    return tuple(entries)


def _parse_month_days(raw: str | None, freq: str) -> tuple[int, ...]:
    if raw is None:
        return ()
    if freq not in {"MONTHLY", "YEARLY"}:
        raise ScheduleInputError("invalid_rule", "BYMONTHDAY requires FREQ=MONTHLY or FREQ=YEARLY")
    values = _parse_int_list(raw, name="BYMONTHDAY", minimum=-31, maximum=31, allow_negative=True)
    return values


def _check_frequency(
    freq: str,
    by_day: tuple[tuple[int | None, int], ...],
    by_month_day: tuple[int, ...],
    by_hour: tuple[int, ...],
    by_minute: tuple[int, ...],
    by_second: tuple[int, ...],
) -> None:
    times = (len(by_hour) or 1) * (len(by_minute) or 1) * (len(by_second) or 1)
    if freq == "DAILY":
        day_slots = 1
    elif freq == "WEEKLY":
        day_slots = len({weekday for _, weekday in by_day}) or 1
    elif freq == "MONTHLY":
        day_slots = max(len(by_month_day), 5 * len(by_day), 1)
    else:
        day_slots = 366
    if day_slots * times > _MAX_PERIOD_CANDIDATES:
        raise ScheduleInputError(
            "frequency_too_high",
            f"rule expands to more than {_MAX_PERIOD_CANDIDATES} candidates per period",
        )


__all__ = ["RRule", "parse_rrule"]
