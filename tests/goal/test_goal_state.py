"""The goal state machine: verbs, revision CAS and phase transitions.

The service is native-only: every write is one goal.changed fact appended to a
real NativeSession, and the state is refolded from that log. These tests drive
the public verbs against an on-disk session and assert both the returned view
and the durable event payload.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from deeppyro.goal import GoalError, GoalRef, GoalService
from deeppyro.session.schema import NativeSession


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


SESSION_ID = "session-11111111-1111-1111-1111-111111111111"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[NativeSession]:
    native = NativeSession.create(
        tmp_path / f"{SESSION_ID}.jsonl",
        cwd=tmp_path,
        session_id=SESSION_ID,
    )
    try:
        yield native
    finally:
        native.close()


async def test_edit_keeps_the_phase_and_bumps_the_revision(session: NativeSession) -> None:
    service = _service()
    created = await service.create(session, "first", max_goal_rounds=5)

    edited = await service.edit(session, created.ref, objective="second")

    assert edited.goal.objective == "second"
    assert edited.goal.phase == "active"
    assert edited.goal.revision == 2
    assert edited.goal.max_goal_rounds == 5
    assert edited.activation == "armed"
    event = session.events[-1]
    assert event.type == "goal.changed"
    assert event.data["goal"]["objective"] == "second"
    assert event.data["goal"]["revision"] == 2
    assert event.data["rounds_started"] == 0


async def test_a_stale_reference_is_rejected(session: NativeSession) -> None:
    service = _service()
    created = await service.create(session, "first")
    await service.edit(session, created.ref, objective="second")

    with pytest.raises(GoalError) as stale:
        await service.edit(session, created.ref, objective="third")

    assert stale.value.code == "GOAL_STALE_REVISION"


async def test_a_second_create_while_active_is_refused(session: NativeSession) -> None:
    service = _service()
    created = await service.create(session, "first")

    with pytest.raises(GoalError) as exists:
        await service.create(session, "second")

    assert exists.value.code == "GOAL_ALREADY_EXISTS"
    assert created.goal.id in str(exists.value)


async def test_pause_disarms_and_resume_arms_again(session: NativeSession) -> None:
    service = _service()
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


async def test_resume_refuses_an_exhausted_goal(session: NativeSession) -> None:
    service = _service()
    created = await service.create(session, "first", max_goal_rounds=1)
    service.admit_round(session, created.ref, 1)
    paused = await service.pause(session, created.ref)

    with pytest.raises(GoalError) as exhausted:
        await service.resume(session, paused.ref)

    assert exhausted.value.code == "GOAL_INVALID_TRANSITION"
    assert "exhausted 1 goal rounds" in str(exhausted.value)


async def test_block_requires_a_reason_and_records_the_code(session: NativeSession) -> None:
    service = _service()
    created = await service.create(session, "first")

    blocked = await service.block(session, created.ref, "the API key is missing")

    assert blocked.goal.phase == "blocked"
    assert blocked.goal.blocked_reason is not None
    assert blocked.goal.blocked_reason.code == "model-reported"
    assert blocked.goal.blocked_reason.message == "the API key is missing"
    assert session.events[-1].data["goal"]["blocked_reason"] == {
        "code": "model-reported",
        "message": "the API key is missing",
    }
    with pytest.raises(GoalError) as missing:
        await service.block(session, blocked.ref, "   ")
    assert missing.value.code == "GOAL_INVALID_BLOCK_REASON"


async def test_complete_allows_a_fresh_create(session: NativeSession) -> None:
    service = _service()
    created = await service.create(session, "first")

    done = await service.complete(session, created.ref)
    assert done.goal.phase == "complete"
    assert service.activation(session) == "disarmed"

    fresh = await service.create(session, "second")
    assert fresh.goal.id != created.goal.id
    assert fresh.goal.revision == 1
    assert fresh.goal.phase == "active"


async def test_clear_needs_a_goal_and_a_matching_reference(session: NativeSession) -> None:
    service = _service()
    with pytest.raises(GoalError) as nothing:
        await service.clear(session)
    assert nothing.value.code == "GOAL_NOT_FOUND"

    created = await service.create(session, "first")
    with pytest.raises(GoalError) as stale:
        await service.clear(session, GoalRef(created.goal.id, 9))
    assert stale.value.code == "GOAL_STALE_REVISION"


async def test_edit_cannot_be_called_without_a_replacement(session: NativeSession) -> None:
    service = _service()
    created = await service.create(session, "first")

    with pytest.raises(GoalError) as invalid:
        await service.edit(session, created.ref)

    assert invalid.value.code == "GOAL_INVALID_EDIT"
