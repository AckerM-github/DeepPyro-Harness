"""The goal state machine: verbs, revision CAS and phase transitions."""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.goal import GoalError, GoalRef, GoalService


class _Record:
    def __init__(self, type_: str, data: dict[str, Any], time: int = 0) -> None:
        self.type = type_
        self.data = data
        self.time = time


class _Session:
    def __init__(self, session_id: str = "session-1") -> None:
        self.session_id = session_id
        self.records: list[_Record] = []

    def append(self, type_: str, data: dict[str, Any] | None = None) -> int:
        self.records.append(_Record(type_, dict(data or {}), time=len(self.records) + 1))
        return len(self.records)


class _Clock:
    def __init__(self, start: int = 1_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 10
        return self.value


def _service() -> GoalService:
    service = GoalService()
    service.configure(clock=_Clock())
    return service


async def test_edit_keeps_the_phase_and_bumps_the_revision() -> None:
    service = _service()
    session = _Session()
    created = await service.create(session, "first", max_goal_rounds=5)

    edited = await service.edit(session, created.ref, objective="second")

    assert edited.goal.objective == "second"
    assert edited.goal.phase == "active"
    assert edited.goal.revision == 2
    assert edited.goal.max_goal_rounds == 5
    assert edited.activation == "armed"
    assert session.records[-1].data["operation"] == "edit"


async def test_a_stale_reference_is_rejected() -> None:
    service = _service()
    session = _Session()
    created = await service.create(session, "first")
    await service.edit(session, created.ref, objective="second")

    with pytest.raises(GoalError) as stale:
        await service.edit(session, created.ref, objective="third")

    assert stale.value.code == "GOAL_STALE_REVISION"


async def test_a_second_create_while_active_is_refused() -> None:
    service = _service()
    session = _Session()
    created = await service.create(session, "first")

    with pytest.raises(GoalError) as exists:
        await service.create(session, "second")

    assert exists.value.code == "GOAL_ALREADY_EXISTS"
    assert created.goal.id in str(exists.value)


async def test_pause_disarms_and_resume_arms_again() -> None:
    service = _service()
    session = _Session()
    created = await service.create(session, "first")
    assert service.activation(session) == "armed"

    paused = await service.pause(session, created.ref)
    assert paused.goal.phase == "paused"
    assert service.activation(session) == "disarmed"

    resumed = await service.resume(session, paused.ref)
    assert resumed.goal.phase == "active"
    assert service.activation(session) == "armed"

    with pytest.raises(GoalError) as already:
        await service.resume(session, resumed.ref)
    assert already.value.code == "GOAL_INVALID_TRANSITION"
    assert "already active and armed" in str(already.value)


async def test_resume_refuses_an_exhausted_goal() -> None:
    service = _service()
    session = _Session()
    created = await service.create(session, "first", max_goal_rounds=1)
    session.records.append(
        _Record(
            "user/message",
            {
                "id": "message-1",
                "role": "user",
                "content": [{"type": "text", "text": "go"}],
                "source": {
                    "kind": "goal",
                    "goalId": created.goal.id,
                    "revision": created.goal.revision,
                    "round": 1,
                },
            },
            time=99,
        )
    )
    service.invalidate(session)
    paused = await service.pause(session, created.ref)

    with pytest.raises(GoalError) as exhausted:
        await service.resume(session, paused.ref)

    assert exhausted.value.code == "GOAL_INVALID_TRANSITION"
    assert "exhausted 1 goal rounds" in str(exhausted.value)


async def test_block_requires_a_reason_and_records_the_code() -> None:
    service = _service()
    session = _Session()
    created = await service.create(session, "first")

    blocked = await service.block(session, created.ref, "the API key is missing")

    assert blocked.goal.phase == "blocked"
    assert blocked.goal.blocked_reason is not None
    assert blocked.goal.blocked_reason.code == "model-reported"
    assert blocked.goal.blocked_reason.message == "the API key is missing"
    assert session.records[-1].data["goal"]["blockedReason"] == {
        "code": "model-reported",
        "message": "the API key is missing",
    }
    with pytest.raises(GoalError) as missing:
        await service.block(session, blocked.ref, "   ")
    assert missing.value.code == "GOAL_INVALID_BLOCK_REASON"


async def test_complete_allows_a_fresh_create() -> None:
    service = _service()
    session = _Session()
    created = await service.create(session, "first")

    done = await service.complete(session, created.ref)
    assert done.goal.phase == "complete"
    assert service.activation(session) == "disarmed"

    fresh = await service.create(session, "second")
    assert fresh.goal.id != created.goal.id
    assert fresh.goal.revision == 1
    assert fresh.goal.phase == "active"


async def test_clear_needs_a_goal_and_a_matching_reference() -> None:
    service = _service()
    session = _Session()
    with pytest.raises(GoalError) as nothing:
        await service.clear(session)
    assert nothing.value.code == "GOAL_NOT_FOUND"

    created = await service.create(session, "first")
    with pytest.raises(GoalError) as stale:
        await service.clear(session, GoalRef(created.goal.id, 9))
    assert stale.value.code == "GOAL_STALE_REVISION"


async def test_edit_cannot_be_called_without_a_replacement() -> None:
    service = _service()
    session = _Session()
    created = await service.create(session, "first")

    with pytest.raises(GoalError) as invalid:
        await service.edit(session, created.ref)

    assert invalid.value.code == "GOAL_INVALID_EDIT"
