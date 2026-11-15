"""The goal service: DSH snapshots, revision CAS and process-local activation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from deeppyro.core.bus import EventBus
from deeppyro.goal import GOAL_EVENT, GoalError, GoalService

SNAPSHOT_KEYS = {"kind", "version", "operation", "goal", "roundsStarted", "createdAt", "updatedAt"}


class _Record:
    def __init__(self, type_: str, data: dict[str, Any]) -> None:
        self.type = type_
        self.data = data
        self.time = 0


class _Session:
    def __init__(self, session_id: str = "session-1") -> None:
        self.session_id = session_id
        self.records: list[_Record] = []

    def append(self, type_: str, data: dict[str, Any] | None = None) -> int:
        self.records.append(_Record(type_, dict(data or {})))
        return len(self.records)


async def test_create_records_a_snapshot_and_publishes() -> None:
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.on("goal/changed", seen.append)
    service = GoalService()
    service.configure(emit=bus.emit)
    session = _Session()

    view = await service.create(session, "  ship   the  plugin  ")

    assert view.goal.objective == "ship the plugin"
    assert view.goal.phase == "active"
    assert view.goal.revision == 1
    assert view.rounds_started == 0
    assert view.activation == "armed"
    record = session.records[-1]
    assert record.type == GOAL_EVENT
    assert set(record.data) == SNAPSHOT_KEYS
    assert record.data["operation"] == "create"
    assert record.data["goal"]["phase"] == "active"
    await asyncio.sleep(0)
    assert seen == [{"session": "session-1", "goal": "ship the plugin"}]
    assert service.get(session) == "ship the plugin"


async def test_clear_writes_a_tombstone_and_disarms() -> None:
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.on("goal/changed", seen.append)
    service = GoalService()
    service.configure(emit=bus.emit)
    session = _Session()
    created = await service.create(session, "one")

    await service.clear(session, created.ref)

    assert service.get(session) is None
    assert service.activation(session) == "disarmed"
    tombstone = session.records[-1].data
    assert set(tombstone) == {"kind", "version", "operation", "cleared", "clearedAt"}
    assert tombstone["cleared"] == {"id": created.goal.id, "revision": 2}
    await asyncio.sleep(0)
    assert [item["goal"] for item in seen] == ["one", None]


async def test_a_reopened_session_rehydrates_from_the_log() -> None:
    session = _Session()
    first = GoalService()
    await first.create(session, "finish the bus")
    reopened = GoalService()
    reopened.configure(load=lambda session_id: session.records if session_id == "session-1" else [])

    assert reopened.get("session-1") == "finish the bus"
    # A fresh process never trusts an activation it did not arm itself.
    assert reopened.activation("session-1") == "disarmed"


async def test_empty_and_oversized_goals_are_rejected() -> None:
    service = GoalService({"maxChars": 10})
    session = _Session()
    with pytest.raises(GoalError) as empty:
        await service.create(session, "   ")
    assert empty.value.code == "GOAL_INVALID_OBJECTIVE"
    with pytest.raises(GoalError) as oversized:
        await service.create(session, "x" * 11)
    assert oversized.value.code == "GOAL_INVALID_OBJECTIVE"


def test_context_text_is_empty_without_a_goal() -> None:
    service = GoalService()
    session = _Session()
    assert service.context_text(session) == ""


async def test_the_legacy_set_and_clear_helpers_still_work() -> None:
    service = GoalService()
    session = _Session()

    assert await service.set(session, "one") == "one"
    assert await service.set(session, "two") == "two"
    assert service.get(session) == "two"
    await service.clear(session)
    assert service.get(session) is None
