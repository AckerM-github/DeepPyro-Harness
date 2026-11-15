"""The goal service: one goal per session, recorded and announced."""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.core.bus import EventBus
from deeppyro.core.errors import ToolError
from deeppyro.goal import GOAL_EVENT, GoalService


class _Record:
    def __init__(self, type_: str, data: dict[str, Any]) -> None:
        self.type = type_
        self.data = data


class _Session:
    def __init__(self, session_id: str = "session-1") -> None:
        self.session_id = session_id
        self.records: list[_Record] = []

    def append(self, type_: str, data: dict[str, Any] | None = None) -> int:
        self.records.append(_Record(type_, dict(data or {})))
        return len(self.records)


async def test_set_records_and_publishes() -> None:
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.on("goal/changed", seen.append)
    service = GoalService()
    service.configure(emit=bus.emit)
    session = _Session()
    assert await service.set(session, "  ship   the  plugin  ") == "ship the plugin"
    assert session.records[-1].type == GOAL_EVENT
    assert session.records[-1].data == {"goal": "ship the plugin"}
    assert service.get(session) == "ship the plugin"
    import asyncio

    await asyncio.sleep(0)
    assert seen == [{"session": "session-1", "goal": "ship the plugin"}]


async def test_clear_records_none_and_publishes() -> None:
    bus = EventBus()
    seen: list[dict[str, Any]] = []
    bus.on("goal/changed", seen.append)
    service = GoalService()
    service.configure(emit=bus.emit)
    session = _Session()
    await service.set(session, "one")
    await service.clear(session)
    assert service.get(session) is None
    assert session.records[-1].data == {"goal": None}
    import asyncio

    await asyncio.sleep(0)
    assert seen == [
        {"session": "session-1", "goal": "one"},
        {"session": "session-1", "goal": None},
    ]


async def test_a_reopened_session_rehydrates_from_the_log() -> None:
    session = _Session()
    first = GoalService()
    await first.set(session, "finish the bus")
    reopened = GoalService()
    reopened.configure(load=lambda session_id: session.records if session_id == "session-1" else [])
    assert reopened.get("session-1") == "finish the bus"


async def test_empty_and_oversized_goals_are_rejected() -> None:
    service = GoalService({"maxChars": 10})
    session = _Session()
    with pytest.raises(ToolError):
        await service.set(session, "   ")
    with pytest.raises(ToolError):
        await service.set(session, "x" * 11)


def test_context_text_is_empty_without_a_goal() -> None:
    service = GoalService()
    session = _Session()
    assert service.context_text(session) == ""
