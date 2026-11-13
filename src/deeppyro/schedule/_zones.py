"""Time zone helpers shared by the schedule timing engines.

Kept dependency-free: zone lookup uses the standard library zoneinfo database,
wall-clock conversion detects daylight-saving gaps (a nonexistent local time is
skipped instead of being silently shifted) and resolves ambiguous local times to
the earlier instant (fold=0).
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .types import ScheduleInputError

#: Names the standard library resolves without the optional tzdata package.
_UTC_NAMES: frozenset[str] = frozenset({"UTC", "ETC/UTC", "ETC/UCT", "UCT", "Z"})


@lru_cache(maxsize=64)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def resolve_zone(name: str = "", *, default: str = "") -> tzinfo:
    """Resolve an explicit IANA zone, falling back to a default or system local."""
    selected = (name or "").strip() or (default or "").strip()
    if selected:
        if selected.upper() in _UTC_NAMES:
            return UTC
        try:
            return _zone(selected)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ScheduleInputError(
                "invalid_time_zone",
                f"unknown time zone {selected!r} (install the tzdata package to enable named time zones)",
            ) from exc
    local = datetime.now().astimezone().tzinfo
    return local or UTC


def wall_to_utc(zone: tzinfo, naive: datetime) -> datetime | None:
    """Convert a local wall time to UTC, or None when DST skipped it."""
    candidate = naive.replace(tzinfo=zone)
    utc = candidate.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != naive:
        return None
    return utc


__all__ = ["resolve_zone", "wall_to_utc"]
