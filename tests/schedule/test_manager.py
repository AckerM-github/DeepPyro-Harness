"""Tests for the schedule manager: catalog operations and the delivery timer."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from deeppyro.schedule import (
    DeliveryReceipt,
    NewOnceTarget,
    NewTarget,
    ScheduleConfig,
    ScheduleInputError,
    ScheduleManager,
    ScheduleStore,
    SessionTarget,
)

NOW = datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
RRULE_DAILY = """DTSTART:20261114T020100Z
RRULE:FREQ=DAILY;COUNT=2"""


class FakeClock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> datetime:
        self.moment = self.moment + timedelta(seconds=seconds)
        return self.moment


class FakeHost:
    def __init__(self, sessions: list[str] | None = None) -> None:
        self.sessions = set(sessions if sessions is not None else ["session-1"])
        self.deliveries: list[tuple[object, str]] = []
        self.depths: dict[str, int] = {}
        self.failures = 0
        self.receipt: DeliveryReceipt | None = None
        self.raise_depth = False

    async def session_exists(self, session_id: str) -> bool:
        return session_id in self.sessions

    async def delegation_depth(self, session_id: str) -> int:
        if self.raise_depth:
            raise RuntimeError("depth boom")
        return self.depths.get(session_id, 0)

    async def deliver(self, task: object, message: str) -> DeliveryReceipt | None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("delivery boom")
        self.deliveries.append((task, message))
        return self.receipt


def _manager(
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
    host: FakeHost | None = None,
    config: ScheduleConfig | None = None,
) -> ScheduleManager:
    return ScheduleManager(
        ScheduleStore(tmp_path / "schedules.json"),
        host=host if host is not None else FakeHost(),
        config=config,
        now_fn=clock if clock is not None else FakeClock(),
    )


async def _create(
    manager: ScheduleManager,
    request: dict[str, object],
    *,
    actor: str | None = "session-1",
) -> object:
    return await manager.create(request, actor_session_id=actor)


async def test_create_normalizes_fields_and_persists(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = await _create(manager, {"title": "  standup ", "prompt": "say hi", "rrule": RRULE_DAILY})
    assert record.title == "standup"
    assert record.prompt == "say hi"
    assert record.kind == "rrule"
    assert record.status == "active"
    assert record.scheduled_at == "2026-11-14T02:01:00.000Z"
    assert record.created_at == "2026-11-14T02:00:00.000Z"
    assert isinstance(record.target, SessionTarget)
    assert record.target.session_id == "session-1"

    page = await manager.list()
    assert page["total"] == 1
    assert page["limit"] == 100
    assert page["offset"] == 0
    model = page["tasks"][0]
    assert model["id"] == record.id
    assert model["status"] == "active"
    assert model["kind"] == "rrule"
    assert model["scheduledAt"] == record.scheduled_at
    assert "scheduledAtLocal" in model

    stored = ScheduleStore(tmp_path / "schedules.json").load()
    assert list(stored) == [record.id]


async def test_create_rejects_missing_and_invalid_fields(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(ScheduleInputError, match="title must not be empty"):
        await _create(manager, {"title": "   ", "prompt": "x", "after_seconds": 60})
    with pytest.raises(ScheduleInputError, match="title must be at most 120 characters"):
        await _create(manager, {"title": "x" * 121, "prompt": "x", "after_seconds": 60})
    with pytest.raises(ScheduleInputError, match="prompt must not be empty"):
        await _create(manager, {"title": "x", "prompt": "", "after_seconds": 60})
    with pytest.raises(ScheduleInputError, match="schedule requires a timing"):
        await _create(manager, {"title": "x", "prompt": "y"})
    with pytest.raises(ScheduleInputError, match="every_seconds must be between"):
        await _create(manager, {"title": "x", "prompt": "y", "every_seconds": 30})
    with pytest.raises(ScheduleInputError, match="cron timings cannot be executed"):
        await _create(manager, {"title": "x", "prompt": "y", "cron": "* * * * *"})
    with pytest.raises(ScheduleInputError, match="cron expression must have five fields"):
        await _create(manager, {"title": "x", "prompt": "y", "cron": "* *"})
    with pytest.raises(ScheduleInputError, match="not in the future") as error:
        await _create(manager, {"title": "x", "prompt": "y", "at": "2020-01-01T00:00:00Z"})
    assert error.value.code == "not_future"
    assert await manager.list() == {"tasks": [], "total": 0, "limit": 100, "offset": 0}


async def test_create_target_kinds(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60, "target": "new_once"})
    assert isinstance(record.target, NewOnceTarget)
    record = await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60, "target": "new"})
    assert isinstance(record.target, NewTarget)
    record = await _create(
        manager,
        {
            "title": "t",
            "prompt": "p",
            "after_seconds": 60,
            "target": {"kind": "session", "session_id": "session-9"},
        },
    )
    assert isinstance(record.target, SessionTarget)
    assert record.target.session_id == "session-9"
    with pytest.raises(ScheduleInputError, match="target is required when there is no current session"):
        await manager.create({"title": "t", "prompt": "p", "after_seconds": 60})
    with pytest.raises(ScheduleInputError, match="unknown schedule target"):
        await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60, "target": "bogus"})


async def test_list_filters_and_paginates(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first = await _create(manager, {"title": "a", "prompt": "p", "after_seconds": 60})
    second = await _create(manager, {"title": "b", "prompt": "p", "after_seconds": 120})
    third = await _create(manager, {"title": "c", "prompt": "p", "after_seconds": 180})
    await manager.update(second.id, {"active": False})

    active = await manager.list(active=True)
    assert active["total"] == 2
    assert {task["id"] for task in active["tasks"]} == {first.id, third.id}
    paused = await manager.list(active=False)
    assert paused["total"] == 1
    assert paused["tasks"][0]["id"] == second.id

    page = await manager.list(limit=1, offset=1)
    assert page["total"] == 3
    assert page["limit"] == 1
    assert page["offset"] == 1

    with pytest.raises(ScheduleInputError, match="limit must be between 1 and 500"):
        await manager.list(limit=0)
    with pytest.raises(ScheduleInputError, match="offset must not be negative"):
        await manager.list(offset=-1)


async def test_update_applies_cas_and_validates(tmp_path: Path) -> None:
    clock = FakeClock()
    manager = _manager(tmp_path, clock=clock)
    record = await _create(manager, {"title": "old", "prompt": "p", "after_seconds": 60})

    clock.advance(5)
    updated = await manager.update(record.id, {"title": "new", "prompt": "q"})
    assert updated.title == "new"
    assert updated.prompt == "q"
    assert updated.updated_at > record.updated_at

    with pytest.raises(ScheduleInputError, match="changed since updatedAt"):
        await manager.update(record.id, {"title": "again"}, expected_updated_at=record.updated_at)
    with pytest.raises(ScheduleInputError, match="requires at least one changed field"):
        await manager.update(record.id, {})
    with pytest.raises(ScheduleInputError, match="does not exist"):
        await manager.update("schedule-missing", {"title": "x"})
    with pytest.raises(ScheduleInputError, match="active must be a boolean"):
        await manager.update(record.id, {"active": "yes"})


async def test_pause_and_resume_controls_delivery(tmp_path: Path) -> None:
    clock = FakeClock()
    host = FakeHost()
    manager = _manager(tmp_path, clock=clock, host=host)
    record = await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60})

    paused = await manager.update(record.id, {"active": False})
    assert paused.status == "paused"
    clock.advance(120)
    assert await manager.tick() == []
    assert host.deliveries == []

    resumed = await manager.update(record.id, {"active": True})
    assert resumed.status == "active"
    receipts = await manager.tick()
    assert len(receipts) == 1
    assert host.deliveries[0][1].startswith("Scheduled task: t")
    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.status == "ended"


async def test_tick_delivers_one_shot_and_ends_it(tmp_path: Path) -> None:
    clock = FakeClock()
    host = FakeHost()
    manager = _manager(tmp_path, clock=clock, host=host)
    record = await _create(manager, {"title": "t", "prompt": "do it", "after_seconds": 60})

    clock.advance(120)
    receipts = await manager.tick()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.scheduled_at == "2026-11-14T02:01:00.000Z"
    assert receipt.delivered_at == "2026-11-14T02:02:00.000Z"
    assert receipt.target_session_id == "session-1"
    assert len(host.deliveries) == 1
    message = host.deliveries[0][1]
    assert message.startswith("Scheduled task: t (occurrence ")
    assert message.endswith(")\n\ndo it")

    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.status == "ended"
    assert stored.last_delivery is not None
    assert len(stored.delivery_history) == 1
    assert ScheduleStore(tmp_path / "schedules.json").load()[record.id].status == "ended"
    assert manager.next_wakeup() is None
    assert await manager.tick() == []


async def test_tick_uses_host_receipt_metadata(tmp_path: Path) -> None:
    clock = FakeClock()
    host = FakeHost()
    host.receipt = DeliveryReceipt(
        scheduled_at="",
        delivered_at="2026-11-14T02:02:00.000Z",
        message_id="msg-1",
        target_session_id="session-1",
    )
    manager = _manager(tmp_path, clock=clock, host=host)
    record = await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60})
    clock.advance(120)
    receipts = await manager.tick()
    assert receipts[0].message_id == "msg-1"
    assert receipts[0].scheduled_at == "2026-11-14T02:01:00.000Z"
    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.last_delivery is not None
    assert stored.last_delivery.message_id == "msg-1"


async def test_tick_collapses_missed_recurring_occurrences(tmp_path: Path) -> None:
    clock = FakeClock()
    host = FakeHost()
    manager = _manager(tmp_path, clock=clock, host=host)
    record = await _create(manager, {"title": "t", "prompt": "p", "every_seconds": 60})

    clock.advance(300)
    receipts = await manager.tick()
    assert len(receipts) == 1
    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.status == "active"
    assert stored.scheduled_at == "2026-11-14T02:06:00.000Z"
    assert len(stored.delivery_history) == 1
    assert manager.next_wakeup() == "2026-11-14T02:06:00.000Z"


async def test_failed_delivery_is_retried_without_advancing(tmp_path: Path) -> None:
    clock = FakeClock()
    host = FakeHost()
    manager = _manager(tmp_path, clock=clock, host=host)
    record = await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60})
    host.failures = 1

    clock.advance(120)
    assert await manager.tick() == []
    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.status == "active"
    assert stored.scheduled_at == "2026-11-14T02:01:00.000Z"
    assert stored.delivery_history == ()

    receipts = await manager.tick()
    assert len(receipts) == 1
    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.status == "ended"


async def test_delivery_ends_when_session_is_gone(tmp_path: Path) -> None:
    clock = FakeClock()
    host = FakeHost(sessions=[])
    manager = _manager(tmp_path, clock=clock, host=host)
    record = await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60})
    clock.advance(120)
    assert await manager.tick() == []
    assert host.deliveries == []
    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.status == "ended"


async def test_tick_without_host_does_nothing(tmp_path: Path) -> None:
    clock = FakeClock()
    manager = ScheduleManager(
        ScheduleStore(tmp_path / "schedules.json"),
        host=None,
        now_fn=clock,
    )
    record = await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60})
    clock.advance(120)
    assert await manager.tick() == []
    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.status == "active"
    assert manager.host is None


async def test_delete_and_view(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    record = await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 60})
    assert await manager.view(record.id) is not None
    assert await manager.delete(record.id) is True
    assert await manager.delete(record.id) is False
    assert await manager.view(record.id) is None
    assert await manager.list() == {"tasks": [], "total": 0, "limit": 100, "offset": 0}
    assert ScheduleStore(tmp_path / "schedules.json").load() == {}


async def test_reloads_state_from_store(tmp_path: Path) -> None:
    clock = FakeClock()
    manager = _manager(tmp_path, clock=clock)
    record = await _create(manager, {"title": "t", "prompt": "p", "rrule": RRULE_DAILY})

    reloaded = _manager(tmp_path, clock=clock)
    page = await reloaded.list()
    assert page["total"] == 1
    assert page["tasks"][0]["id"] == record.id
    assert page["tasks"][0]["scheduledAt"] == record.scheduled_at


async def test_corrupt_store_is_quarantined_on_load(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text("{not json", encoding="utf-8")
    manager = _manager(tmp_path)
    assert await manager.list() == {"tasks": [], "total": 0, "limit": 100, "offset": 0}
    assert not path.exists()
    assert list(tmp_path.glob("schedules.json.corrupt-*"))


async def test_delegation_depth_and_next_wakeup(tmp_path: Path) -> None:
    clock = FakeClock()
    host = FakeHost()
    host.depths["session-1"] = 2
    manager = _manager(tmp_path, clock=clock, host=host)
    assert await manager.delegation_depth("session-1") == 2
    assert await manager.delegation_depth("session-2") == 0
    host.raise_depth = True
    assert await manager.delegation_depth("session-1") == 0

    assert manager.next_wakeup() is None
    await _create(manager, {"title": "t", "prompt": "p", "after_seconds": 120})
    assert manager.next_wakeup() == "2026-11-14T02:02:00.000Z"


async def test_timer_start_and_stop_delivers(tmp_path: Path) -> None:
    host = FakeHost()
    manager = ScheduleManager(
        ScheduleStore(tmp_path / "schedules.json"),
        host=host,
        config=ScheduleConfig(scan_interval_seconds=1),
    )
    record = await _create(manager, {"title": "timer", "prompt": "ping", "after_seconds": 1})
    await manager.start()
    await manager.start()  # idempotent
    try:
        for _ in range(40):
            if host.deliveries:
                break
            await asyncio.sleep(0.05)
    finally:
        await manager.stop()
        await manager.stop()  # idempotent

    assert len(host.deliveries) == 1
    stored = await manager.view(record.id)
    assert stored is not None
    assert stored.status == "ended"


async def test_stop_before_start_is_safe(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    await manager.stop()
    await manager.stop()
