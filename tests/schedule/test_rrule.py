"""Tests for the RRULE subset engine."""

from datetime import UTC, datetime

import pytest
from dateutil import rrule as dateutil_rrule

from deeppyro.schedule.rrule import RRule, parse_rrule
from deeppyro.schedule.types import ScheduleInputError


def _rule(text: str, *, default_time_zone: str = "") -> RRule:
    return parse_rrule(text, default_time_zone=default_time_zone)


def _occurrences(text: str, *, default_time_zone: str = "") -> list[datetime]:
    return list(_rule(text, default_time_zone=default_time_zone).occurrences())


def test_daily_count_includes_dtstart() -> None:
    occurrences = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;COUNT=3"""
    )
    assert occurrences == [
        datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
    ]


def test_daily_interval() -> None:
    occurrences = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;INTERVAL=2;COUNT=3"""
    )
    assert occurrences == [
        datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 5, 8, 0, tzinfo=UTC),
    ]


def test_daily_byday_skips_non_matching_dtstart() -> None:
    # 2026-09-01 is a Tuesday; only Mondays are emitted.
    occurrences = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;BYDAY=MO;COUNT=2"""
    )
    assert occurrences == [
        datetime(2026, 9, 7, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
    ]


def test_weekly_byday() -> None:
    occurrences = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=WEEKLY;BYDAY=MO,FR;COUNT=4"""
    )
    assert occurrences == [
        datetime(2026, 9, 4, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 7, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
    ]


def test_weekly_interval_defaults_to_dtstart_weekday() -> None:
    occurrences = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=3"""
    )
    assert occurrences == [
        datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 15, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 29, 8, 0, tzinfo=UTC),
    ]


def test_monthly_bymonthday() -> None:
    occurrences = _occurrences(
        """DTSTART:20260115T080000Z
RRULE:FREQ=MONTHLY;BYMONTHDAY=15;COUNT=3"""
    )
    assert occurrences == [
        datetime(2026, 1, 15, 8, 0, tzinfo=UTC),
        datetime(2026, 2, 15, 8, 0, tzinfo=UTC),
        datetime(2026, 3, 15, 8, 0, tzinfo=UTC),
    ]


def test_monthly_ordinal_byday() -> None:
    occurrences = _occurrences(
        """DTSTART:20260101T080000Z
RRULE:FREQ=MONTHLY;BYDAY=2TU;COUNT=3"""
    )
    assert occurrences == [
        datetime(2026, 1, 13, 8, 0, tzinfo=UTC),
        datetime(2026, 2, 10, 8, 0, tzinfo=UTC),
        datetime(2026, 3, 10, 8, 0, tzinfo=UTC),
    ]


def test_until_is_inclusive() -> None:
    occurrences = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;UNTIL=20260903T080000Z"""
    )
    assert occurrences == [
        datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 2, 8, 0, tzinfo=UTC),
        datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
    ]


def test_tzid_wall_clock_times() -> None:
    occurrences = _occurrences(
        """DTSTART;TZID=Asia/Shanghai:20260901T080000
RRULE:FREQ=DAILY;COUNT=2"""
    )
    assert occurrences == [
        datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 2, 0, 0, tzinfo=UTC),
    ]


def test_dst_nonexistent_local_time_is_skipped() -> None:
    # US DST starts 2026-03-08; 02:00 local does not exist that day.
    occurrences = _occurrences(
        """DTSTART;TZID=America/New_York:20260307T020000
RRULE:FREQ=DAILY;COUNT=3"""
    )
    assert occurrences == [
        datetime(2026, 3, 7, 7, 0, tzinfo=UTC),
        datetime(2026, 3, 9, 6, 0, tzinfo=UTC),
        datetime(2026, 3, 10, 6, 0, tzinfo=UTC),
    ]


def test_first_after_returns_strictly_later_occurrence() -> None:
    rule = _rule(
        """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;COUNT=5"""
    )
    assert rule.first_after(datetime(2026, 9, 2, 8, 0, tzinfo=UTC)) == datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    assert rule.first_after(datetime(2026, 9, 5, 8, 0, tzinfo=UTC)) is None
    assert rule.first_after(datetime(2026, 8, 1, tzinfo=UTC)) == datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def test_first_after_accepts_naive_instant() -> None:
    rule = _rule(
        """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;COUNT=2"""
    )
    assert rule.first_after(datetime(2026, 9, 1, 8, 0)) == datetime(2026, 9, 2, 8, 0, tzinfo=UTC)


def test_default_time_zone_applies_without_tzid() -> None:
    rule = _rule(
        """DTSTART:20260901T080000
RRULE:FREQ=DAILY;COUNT=1""",
        default_time_zone="Asia/Shanghai",
    )
    assert list(rule.occurrences()) == [datetime(2026, 9, 1, 0, 0, tzinfo=UTC)]
    assert rule.tz is not None


def test_cross_check_daily_against_dateutil() -> None:
    ours = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;INTERVAL=3;COUNT=6"""
    )
    theirs = list(
        dateutil_rrule.rrule(
            dateutil_rrule.DAILY,
            dtstart=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            interval=3,
            count=6,
        )
    )
    assert ours == [value.astimezone(UTC) for value in theirs]


def test_cross_check_weekly_against_dateutil() -> None:
    ours = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,FR;COUNT=6"""
    )
    theirs = list(
        dateutil_rrule.rrule(
            dateutil_rrule.WEEKLY,
            dtstart=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            interval=2,
            byweekday=(dateutil_rrule.MO, dateutil_rrule.FR),
            count=6,
        )
    )
    assert ours == [value.astimezone(UTC) for value in theirs]


def test_cross_check_until_against_dateutil() -> None:
    ours = _occurrences(
        """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;UNTIL=20260910T080000Z"""
    )
    theirs = list(
        dateutil_rrule.rrule(
            dateutil_rrule.DAILY,
            dtstart=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            until=datetime(2026, 9, 10, 8, 0, tzinfo=UTC),
        )
    )
    assert ours == [value.astimezone(UTC) for value in theirs]


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ("", "exactly two non-empty lines"),
        ("DTSTART:20260901T080000Z", "exactly two non-empty lines"),
        (
            """RRULE:FREQ=DAILY
DTSTART:20260901T080000Z""",
            "first rrule line must start with DTSTART",
        ),
        (
            """DTSTART:20260901T080000Z
RRULE:COUNT=2""",
            "RRULE requires FREQ",
        ),
        (
            """DTSTART:20260901T080000Z
RRULE:FREQ=HOURLY;COUNT=2""",
            "HOURLY frequencies are not supported",
        ),
        (
            """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;BYYEARDAY=1""",
            "not supported",
        ),
        (
            """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;FOO=1""",
            "unknown RRULE part",
        ),
        (
            """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;COUNT=2;UNTIL=20260905T080000Z""",
            "COUNT and UNTIL cannot be combined",
        ),
        (
            """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;BYDAY=2MO""",
            "numeric BYDAY requires FREQ=MONTHLY or FREQ=YEARLY",
        ),
        (
            """DTSTART;TZID=Asia/Shanghai:20260901T080000Z
RRULE:FREQ=DAILY;COUNT=1""",
            "must not use the Z suffix",
        ),
        (
            """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;UNTIL=20260905""",
            "UNTIL must be a UTC timestamp",
        ),
        (
            """DTSTART:20260901T080000Z
RRULE:FREQ=DAILY;COUNT=0""",
            "COUNT must be between",
        ),
        (
            """DTSTART:20261301T080000Z
RRULE:FREQ=DAILY;COUNT=1""",
            "not a valid timestamp",
        ),
    ],
)
def test_invalid_rules(rule: str, message: str) -> None:
    with pytest.raises(ScheduleInputError, match=message):
        parse_rrule(rule)


def test_non_string_rule_is_rejected() -> None:
    with pytest.raises(ScheduleInputError, match="two-line DTSTART/RRULE string"):
        parse_rrule(123)  # type: ignore[arg-type]
