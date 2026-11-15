"""Folding goal records: the contract must survive a hostile log."""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.goal import GoalReplayError, decode_change, fold
from deeppyro.goal.replay import ClearedChange, SnapshotChange


class _Record:
    def __init__(self, type_: str, data: dict[str, Any], time: int = 0) -> None:
        self.type = type_
        self.data = data
        self.time = time


def _goal(**overrides: Any) -> dict[str, Any]:
    goal = {
        "id": "goal-1",
        "revision": 1,
        "objective": "ship it",
        "phase": "active",
        "maxGoalRounds": 4,
    }
    goal.update(overrides)
    return goal


def _change(operation: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "kind": "goal/change",
        "version": 1,
        "operation": operation,
        "goal": _goal(),
        "roundsStarted": 0,
        "createdAt": 100,
        "updatedAt": 100,
    }
    body.update(overrides)
    return body


def _record(body: dict[str, Any], time: int = 1) -> _Record:
    return _Record("goal/change", body, time)


def test_a_snapshot_decodes() -> None:
    change = decode_change(_change("create"))
    assert isinstance(change, SnapshotChange)
    assert change.goal.objective == "ship it"
    assert change.created_at == 100


def test_missing_or_extra_keys_fail() -> None:
    body = _change("create")
    del body["createdAt"]
    with pytest.raises(GoalReplayError):
        decode_change(body)
    body = _change("create")
    body["extra"] = True
    with pytest.raises(GoalReplayError):
        decode_change(body)


def test_kind_and_version_are_enforced() -> None:
    with pytest.raises(GoalReplayError):
        decode_change(_change("create", kind="goal/change2"))
    with pytest.raises(GoalReplayError):
        decode_change(_change("create", version=2))


def test_an_unknown_operation_fails() -> None:
    with pytest.raises(GoalReplayError):
        decode_change(_change("archive"))


def test_revision_must_advance_by_one() -> None:
    records = [_record(_change("create")), _record(_change("edit", goal=_goal(revision=3)))]
    with pytest.raises(GoalReplayError):
        fold(records)


def test_created_at_and_rounds_are_immutable() -> None:
    with pytest.raises(GoalReplayError):
        fold([_record(_change("create")), _record(_change("edit", createdAt=200))])
    with pytest.raises(GoalReplayError):
        fold([_record(_change("create")), _record(_change("edit", roundsStarted=2))])


def test_updated_at_cannot_go_backwards() -> None:
    records = [
        _record(_change("create", createdAt=100, updatedAt=300)),
        _record(_change("edit", updatedAt=200)),
    ]
    with pytest.raises(GoalReplayError):
        fold(records)


def test_a_round_message_advances_rounds_started() -> None:
    records = [_record(_change("create"))]
    records.append(
        _Record(
            "user/message",
            {
                "id": "message-1",
                "role": "user",
                "content": [{"type": "text", "text": "go"}],
                "source": {"kind": "goal", "goalId": "goal-1", "revision": 1, "round": 1},
            },
        )
    )
    assert fold(records).rounds_started == 1


def test_a_stale_or_foreign_round_message_is_ignored() -> None:
    records = [_record(_change("create"))]
    for source in (
        {"kind": "goal", "goalId": "goal-1", "revision": 1, "round": 2},
        {"kind": "goal", "goalId": "goal-1", "revision": 9, "round": 1},
        {"kind": "user"},
    ):
        records.append(
            _Record(
                "user/message",
                {
                    "id": "message-x",
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                    "source": source,
                },
            )
        )
    assert fold(records).rounds_started == 0


def test_a_tombstone_needs_the_next_revision() -> None:
    records = [_record(_change("create"))]
    tombstone = {
        "kind": "goal/change",
        "version": 1,
        "operation": "clear",
        "cleared": {"id": "goal-1", "revision": 2},
        "clearedAt": 200,
    }
    state = fold([*records, _record(tombstone, time=2)])
    assert state.goal is None
    assert state.last_revision == 2
    assert state.seen_ids == frozenset({"goal-1"})
    with pytest.raises(GoalReplayError):
        fold([*records, _record({**tombstone, "cleared": {"id": "goal-1", "revision": 3}}, time=2)])


def test_a_cleared_id_cannot_come_back() -> None:
    records = [
        _record(_change("create")),
        _record(
            {
                "kind": "goal/change",
                "version": 1,
                "operation": "clear",
                "cleared": {"id": "goal-1", "revision": 2},
                "clearedAt": 200,
            },
            time=2,
        ),
        _record(_change("create", createdAt=200, updatedAt=300), time=3),
    ]
    with pytest.raises(GoalReplayError):
        fold(records)


def test_legacy_records_fold_without_being_rewritten() -> None:
    legacy = _Record("goal/change", {"goal": "old goal"}, time=7)
    state = fold([legacy])
    assert state.goal is not None
    assert state.goal.objective == "old goal"
    assert state.goal.id == "legacy"
    assert state.rounds_started == 0
    assert state.created_at == 7
    assert legacy.data == {"goal": "old goal"}

    dropped = fold([_Record("goal/change", {"goal": None}, time=8)])
    assert dropped.goal is None

    broken = fold([_Record("goal/change", {"goal": "   "}, time=9)])
    assert broken.goal is None


def test_a_tombstone_decodes_as_a_cleared_change() -> None:
    change = decode_change(
        {
            "kind": "goal/change",
            "version": 1,
            "operation": "clear",
            "cleared": {"id": "goal-1", "revision": 2},
            "clearedAt": 200,
        }
    )
    assert isinstance(change, ClearedChange)
    assert change.cleared.revision == 2


def test_blocked_requires_a_reason_and_others_forbid_it() -> None:
    reason = {"code": "model-reported", "message": "gone"}
    blocked = _change(
        "block",
        goal=_goal(revision=2, phase="blocked", blockedReason=reason),
        updatedAt=200,
    )
    assert fold([_record(_change("create")), _record(blocked, time=2)]).goal is not None
    with pytest.raises(GoalReplayError):
        fold([_record(_change("create", goal=_goal(phase="blocked")))])
    with pytest.raises(GoalReplayError):
        fold([_record(_change("create", goal=_goal(blockedReason=reason)))])
