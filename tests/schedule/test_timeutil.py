"""Time helpers: RFC3339 instants, local formatting and DST-aware wall times."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from deeppyro.schedule import ScheduleInputError
from deeppyro.schedule.timeutil import (
    first_occurrence,
    following_occurrence,
    format_clock_time,
    format_instant,
    format_local,
    occurrence_time_zone,
    parse_clock_time,
    parse_instant,
    resolve_time_zone,
    validate_time_zone,
)
from deeppyro.schedule.types import (
    AfterTiming,
    AtTiming,
    DailyTiming,
    EveryTiming,
    RRuleTiming,
    WeeklyTiming,
)


def test_parse_instant_roundtrip() -> None:
    value = parse_instant("2026-11-14T02:01:05.123Z")
    assert value == datetime(2026, 11, 14, 2, 1, 5, 123000, tzinfo=UTC)
    assert format_instant(value) == "2026-11-14T02:01:05.123Z"


def test_parse_instant_accepts_offsets_and_missing_zone() -> None:
    assert parse_instant("2026-11-14T10:01:00+08:00") == datetime(2026, 11, 14, 2, 1, tzinfo=UTC)
    assert parse_instant("2026-11-14T02:01:00") == datetime(2026, 11, 14, 2, 1, tzinfo=UTC)
    assert format_instant(datetime(2026, 11, 14, 2, 1, 0)) == "2026-11-14T02:01:00.000Z"


def test_parse_instant_rejects_garbage() -> None:
    with pytest.raises(ScheduleInputError, match="RFC3339"):
        parse_instant("tomorrow")


def test_format_instant_truncates_to_milliseconds() -> None:
    value = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=UTC)
    assert format_instant(value) == "2026-01-01T00:00:00.123Z"


def test_format_local_uses_named_zone_and_dst() -> None:
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert format_local(winter, "America/New_York") == "2026-01-15T07:00:00-05:00"
    assert format_local(summer, "America/New_York") == "2026-07-15T08:00:00-04:00"
    assert format_local(winter, "Asia/Shanghai") == "2026-01-15T20:00:00+08:00"


def test_resolve_time_zone_rejects_unknown_zone() -> None:
    with pytest.raises(ScheduleInputError, match="time zone"):
        resolve_time_zone("Mars/Olympus")
    with pytest.raises(ScheduleInputError):
        validate_time_zone("Not/AZone")
    assert resolve_time_zone("UTC") is UTC


def test_parse_clock_time_normalizes() -> None:
    assert parse_clock_time("9:05") == (9, 5, 0)
    assert parse_clock_time("09:05:07") == (9, 5, 7)
    assert parse_clock_time("23:59:59.123") == (23, 59, 59)
    assert format_clock_time(9, 5) == "09:05:00"
    with pytest.raises(ScheduleInputError, match="out of range"):
        parse_clock_time("24:00")
    with pytest.raises(ScheduleInputError, match="HH:MM"):
        parse_clock_time("nine")


def test_first_occurrence_after_and_every() -> None:
    now = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
    assert first_occurrence(AfterTiming(after_seconds=90), now=now) == datetime(
        2026, 11, 14, 2, 1, 30, tzinfo=UTC
    )
    assert first_occurrence(EveryTiming(every_seconds=3600), now=now) == datetime(
        2026, 11, 14, 3, 0, tzinfo=UTC
    )


def test_first_occurrence_at() -> None:
    now = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
    assert first_occurrence(AtTiming(at="2026-11-14T02:30:00Z"), now=now) == datetime(
        2026, 11, 14, 2, 30, tzinfo=UTC
    )
    with pytest.raises(ScheduleInputError, match="not in the future"):
        first_occurrence(AtTiming(at="2026-11-14T01:00:00Z"), now=now)


def test_first_occurrence_daily_time_zone() -> None:
    now = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)  # 09:00 in Shanghai
    timing = DailyTiming(time="10:00:00", time_zone="Asia/Shanghai")
    assert first_occurrence(timing, now=now) == datetime(2026, 11, 14, 2, 0, tzinfo=UTC)


def test_first_occurrence_weekly() -> None:
    now = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)  # Sunday 09:00 in Shanghai
    timing = WeeklyTiming(time="09:00", weekdays=(1,), time_zone="Asia/Shanghai")
    assert first_occurrence(timing, now=now) == datetime(2026, 11, 16, 1, 0, tzinfo=UTC)


def test_daily_skips_nonexistent_dst_wall_time() -> None:
    # 2026-03-08 02:30 does not exist in America/New_York (spring forward).
    now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    timing = DailyTiming(time="02:30:00", time_zone="America/New_York")
    assert first_occurrence(timing, now=now) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_following_occurrence_every_catches_up() -> None:
    previous = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
    now = datetime(2026, 11, 14, 2, 10, 30, tzinfo=UTC)
    next_at = following_occurrence(EveryTiming(every_seconds=60), previous=previous, now=now)
    assert next_at == datetime(2026, 11, 14, 2, 11, tzinfo=UTC)


def test_following_occurrence_daily_after_missed_days() -> None:
    previous = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
    now = datetime(2026, 11, 17, 3, 0, tzinfo=UTC)  # 11:00 in Shanghai, after 10:00
    timing = DailyTiming(time="10:00:00", time_zone="Asia/Shanghai")
    next_at = following_occurrence(timing, previous=previous, now=now)
    assert next_at == datetime(2026, 11, 18, 2, 0, tzinfo=UTC)


def test_following_occurrence_one_shot_returns_none() -> None:
    now = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
    assert following_occurrence(AfterTiming(after_seconds=60), previous=now, now=now) is None
    assert following_occurrence(AtTiming(at="2026-11-14T02:30:00Z"), previous=now, now=now) is None


def test_occurrence_time_zone_reports_explicit_zone() -> None:
    timing = DailyTiming(time="10:00:00", time_zone="Asia/Shanghai")
    assert occurrence_time_zone(timing) == "Asia/Shanghai"
    assert occurrence_time_zone(AfterTiming(after_seconds=1)) == ""
    rule = RRuleTiming(rrule="DTSTART;TZID=Asia/Shanghai:20260901T080000\nRRULE:FREQ=DAILY;COUNT=2")
    assert occurrence_time_zone(rule) == "Asia/Shanghai"
    assert occurrence_time_zone(rule, "UTC") == "Asia/Shanghai"
    assert occurrence_time_zone(RRuleTiming(rrule="DTSTART:20260901T080000Z\nRRULE:FREQ=DAILY")) == ""
