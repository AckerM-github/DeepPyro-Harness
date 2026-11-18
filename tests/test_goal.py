"""The goal service: schema-1 goal.changed facts, revision CAS and activation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.bus import EventBus
from deeppyro.goal import GoalError, GoalService
from deeppyro.session.schema import Event, SessionHeader

FACT_EVENT = "goal.changed"
FACT_KEYS = {"goal", "rounds_started"}
SNAPSHOT_KEYS = {"id", "revision", "objective", "phase", "max_goal_rounds"}


class _Session:
    """A schema-1 native session log: header plus append-only facts."""

    def __init__(self, session_id: str = "session-1") -> None:
        self.header = SessionHeader(
            id=session_id,
            created_at=0,
            cwd=str(Path.cwd()),
            preset="standard",
            parent_id=None,
            origin="user",
            delegation_depth=0,
        )
        self.events: list[Event] = []
        self._ts = 0

    @property
    def session_id(self) -> str:
        return self.header.id

    def append(self, type_: str, data: dict[str, Any] | None = None) -> Event:
        self._ts += 1
        event = Event(type=type_, seq=len(self.events) + 1, ts=self._ts, data=dict(data or {}))
        self.events.append(event)
        return event


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
    fact = session.events[-1]
    assert fact.type == FACT_EVENT
    assert set(fact.data) == FACT_KEYS
    assert set(fact.data["goal"]) == SNAPSHOT_KEYS
    assert fact.data["goal"]["phase"] == "active"
    assert fact.data["goal"]["objective"] == "ship the plugin"
    assert fact.data["rounds_started"] == 0
    await asyncio.sleep(0)
    assert [event["session"] for event in seen] == ["session-1"]
    assert [event["goal"] for event in seen] == ["ship the plugin"]
    assert [event["change"]["operation"] for event in seen] == ["create"]
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
    fact = session.events[-1]
    assert fact.type == FACT_EVENT
    assert fact.data == {"goal": None, "rounds_started": 0}
    assert created.goal.revision == 1
    assert created.ref.revision == 1
    assert len(created.goal.id) > 0
    await asyncio.sleep(0)
    assert [item["goal"] for item in seen] == ["one", None]


async def test_a_reopened_session_rehydrates_from_the_log() -> None:
    session = _Session()
    first = GoalService()
    await first.create(session, "finish the bus")
    reopened = GoalService()
    reopened.configure(load=lambda session_id: session if session_id == session.session_id else None)

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
