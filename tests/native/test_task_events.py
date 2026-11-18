"""Focused tests for the native task fact kinds (KI-011).

The task plugin records task state, handoffs and handoff results as native
session events. These tests pin the vocabulary contract (closed data shapes),
the fold projection, and the native TaskService round trip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.task import TaskBudget, TaskStatus, new_handoff
from deeppyro.builtin.task_tools import TaskService
from deeppyro.session.errors import EventFieldError, OrderingError, UnknownEventError
from deeppyro.session.fold import HandoffFact, TaskFact, fold
from deeppyro.session.schema import EventKind, NativeSession, validate_event_data

SESSION_ID = "session-00000000-0000-4000-8000-0000000000a1"
TASK_ID = "task_0123456789abcdef"
RUN_ID = "handoff-0123456789ab"
CREATED_AT = "2026-11-18T12:00:00.000Z"


def _task(status: str = "pending", **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": TASK_ID,
        "objective": "write the quarterly report",
        "status": status,
        "owner": None,
        "budget": {"max_steps": 32},
        "created_at": CREATED_AT,
        "parent": None,
    }
    task.update(overrides)
    return task


def _state(action: str = "create", status: str = "pending", **overrides: Any) -> dict[str, Any]:
    return {"action": action, "task": _task(status, **overrides)}


def _handoff(**overrides: Any) -> dict[str, Any]:
    handoff: dict[str, Any] = {
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "from": "lead",
        "to": f"subagent:{RUN_ID}",
        "summary": "carry the context",
        "prompt": "write the report",
        "turn": 1,
        "call_id": "call-00000000000000b1",
        "budget": {"max_steps": 32},
        "open_items": ["append appendix"],
        "created_at": CREATED_AT,
    }
    handoff.update(overrides)
    return handoff


def _result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "ok": True,
        "result": "handoff answer",
    }
    result.update(overrides)
    return result


def _new_session(tmp_path: Path, session_id: str = SESSION_ID) -> tuple[NativeSession, Path]:
    path = tmp_path / "native" / f"{session_id}.jsonl"
    session = NativeSession.create(
        path,
        cwd=tmp_path,
        session_id=session_id,
        created_at=1_700_000_000_000,
    )
    return session, path


def test_task_event_kinds_are_core_native_kinds() -> None:
    assert EventKind.TASK_STATE.value == "task.state"
    assert EventKind.TASK_HANDOFF.value == "task.handoff"
    assert EventKind.TASK_HANDOFF_RESULT.value == "task.handoff.result"
    assert {"task.state", "task.handoff", "task.handoff.result"} <= {kind.value for kind in EventKind}


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        (EventKind.TASK_STATE, _state()),
        (EventKind.TASK_STATE, _state("update", "in_progress")),
        (EventKind.TASK_HANDOFF, _handoff()),
        (EventKind.TASK_HANDOFF_RESULT, _result()),
        (EventKind.TASK_HANDOFF_RESULT, _result(ok=False, result="", error="boom")),
    ],
)
def test_task_kinds_accept_their_documented_shape(kind: EventKind, data: dict[str, Any]) -> None:
    validate_event_data(kind.value, data, seq=1)


@pytest.mark.parametrize(
    "data",
    [
        {"action": "create"},
        {"action": "done", "task": _task()},
        {"action": "create", "task": _task(), "extra": True},
        {"action": "create", "task": _task(id="task_xyz")},
        {"action": "create", "task": _task(status="nope")},
        {"action": "create", "task": _task(objective="")},
        {"action": "create", "task": _task(budget={"max_steps": 0})},
        {"action": "create", "task": _task(budget={"max_steps": 32, "max_seconds": 0})},
        {"action": "create", "task": {**_task(), "extra": True}},
    ],
)
def test_task_state_rejects_malformed_data(data: dict[str, Any]) -> None:
    with pytest.raises(EventFieldError):
        validate_event_data(EventKind.TASK_STATE.value, data, seq=1)


@pytest.mark.parametrize(
    "data",
    [
        {name: value for name, value in _handoff().items() if name != "run_id"},
        {name: value for name, value in _handoff().items() if name != "from"},
        {name: value for name, value in _handoff().items() if name != "to"},
        {name: value for name, value in _handoff().items() if name != "summary"},
        {name: value for name, value in _handoff().items() if name != "created_at"},
        _handoff(task_id="nope"),
        _handoff(turn=-1),
        _handoff(call_id=""),
        _handoff(budget={"max_steps": 0}),
        _handoff(open_items=[""]),
        _handoff(open_items="append appendix"),
        _handoff(unknown=True),
    ],
)
def test_task_handoff_rejects_malformed_data(data: dict[str, Any]) -> None:
    with pytest.raises(EventFieldError):
        validate_event_data(EventKind.TASK_HANDOFF.value, data, seq=1)


@pytest.mark.parametrize(
    "data",
    [
        {"run_id": RUN_ID, "result": ""},
        {"ok": True, "result": ""},
        {"run_id": RUN_ID, "ok": "yes", "result": ""},
        {"run_id": RUN_ID, "ok": 1, "result": ""},
        _result(error=5),
        _result(task_id="nope"),
        _result(unknown=True),
    ],
)
def test_task_handoff_result_rejects_malformed_data(data: dict[str, Any]) -> None:
    with pytest.raises(EventFieldError):
        validate_event_data(EventKind.TASK_HANDOFF_RESULT.value, data, seq=1)


def test_fold_projects_task_facts_and_handoff_result(tmp_path: Path) -> None:
    session, _ = _new_session(tmp_path)
    try:
        session.append(EventKind.TASK_STATE, _state())
        session.append(EventKind.TASK_STATE, _state("update", "in_progress"))
        session.append(EventKind.TASK_HANDOFF, _handoff())
        session.append(EventKind.TASK_HANDOFF_RESULT, _result())
        session.append(EventKind.TASK_STATE, _state("update", "completed"))

        assert all(event.run_id is None for event in session.events)
        assert all(event.turn_id is None for event in session.events)

        view = fold(session.header, session.events)
        assert view.issues == ()
        assert view.tasks == (
            TaskFact(
                task_id=TASK_ID,
                objective="write the quarterly report",
                status="completed",
                owner=None,
                created_at=CREATED_AT,
                seq=5,
            ),
        )
        assert view.handoffs == (
            HandoffFact(
                run_id=RUN_ID,
                from_owner="lead",
                to_owner=f"subagent:{RUN_ID}",
                summary="carry the context",
                task_id=TASK_ID,
                created_at=CREATED_AT,
                seq=3,
                open_items=("append appendix",),
                result_ok=True,
                result="handoff answer",
                error=None,
                result_seq=4,
            ),
        )
    finally:
        session.close()


def test_fold_rejects_orphan_and_duplicate_handoff_events(tmp_path: Path) -> None:
    orphan, _ = _new_session(tmp_path, "session-00000000-0000-4000-8000-0000000000a2")
    try:
        orphan.append(EventKind.TASK_HANDOFF_RESULT, _result())
        with pytest.raises(OrderingError, match=r"has no task\.handoff"):
            fold(orphan.header, orphan.events)
    finally:
        orphan.close()

    duplicate, _ = _new_session(tmp_path, "session-00000000-0000-4000-8000-0000000000a3")
    try:
        duplicate.append(EventKind.TASK_HANDOFF, _handoff())
        duplicate.append(EventKind.TASK_HANDOFF, _handoff())
        with pytest.raises(OrderingError, match="used more than once"):
            fold(duplicate.header, duplicate.events)
    finally:
        duplicate.close()

    twice, _ = _new_session(tmp_path, "session-00000000-0000-4000-8000-0000000000a4")
    try:
        twice.append(EventKind.TASK_HANDOFF, _handoff())
        twice.append(EventKind.TASK_HANDOFF_RESULT, _result())
        twice.append(EventKind.TASK_HANDOFF_RESULT, _result())
        with pytest.raises(OrderingError, match="already has a result"):
            fold(twice.header, twice.events)
    finally:
        twice.close()


@pytest.mark.parametrize("event_type", ["task.future", "subagent.started", "team.task.changed"])
def test_unknown_or_reserved_kinds_behave_as_before(tmp_path: Path, event_type: str) -> None:
    session, _ = _new_session(tmp_path, "session-00000000-0000-4000-8000-0000000000a5")
    try:
        with pytest.raises(UnknownEventError, match="unknown event type"):
            session.append(event_type, {})
        assert session.events == ()
    finally:
        session.close()


def test_native_task_service_round_trips_through_a_reopened_session(tmp_path: Path) -> None:
    session, path = _new_session(tmp_path)
    service = TaskService()
    try:
        task = service.create(session, "write the quarterly report", budget=TaskBudget(max_steps=8))
        service.update(session, task.id, TaskStatus.IN_PROGRESS)
        handoff = new_handoff(
            "lead",
            f"subagent:{RUN_ID}",
            "carry the context",
            state={
                "turn": 1,
                "callId": "call-1",
                "usage": {"input": 10, "output": 5, "cached": 2},
                "budget": task.budget.to_dict(),
                "prompt": "write the report",
            },
            open_items=("append appendix",),
            task_id=task.id,
        )
        service.start_handoff(session, handoff, run_id=RUN_ID)
        service.finish_handoff(
            session,
            run_id=RUN_ID,
            task_id=task.id,
            ok=True,
            result="handoff answer",
        )
        service.complete(session, task.id)

        assert [event.type for event in session.events] == [
            "task.state",
            "task.state",
            "task.handoff",
            "task.handoff.result",
            "task.state",
        ]
        handoff_event = session.events[2]
        assert handoff_event.data == {
            "run_id": RUN_ID,
            "task_id": task.id,
            "from": "lead",
            "to": f"subagent:{RUN_ID}",
            "summary": "carry the context",
            "prompt": "write the report",
            "turn": 1,
            "call_id": "call-1",
            "budget": {"max_steps": 8},
            "open_items": ["append appendix"],
            "created_at": handoff.created_at,
        }
        assert "usage" not in handoff_event.data
        result_event = session.events[3]
        assert result_event.data == {
            "run_id": RUN_ID,
            "task_id": task.id,
            "ok": True,
            "result": "handoff answer",
        }
        folded = fold(session.header, session.events)
        assert [fact.status for fact in folded.tasks] == ["completed"]
        assert folded.handoffs[0].result_ok is True
        assert folded.handoffs[0].result == "handoff answer"
    finally:
        session.close()

    reopened = NativeSession.open(path)
    try:
        resumed = TaskService()
        assert resumed.get(reopened, task.id).status is TaskStatus.COMPLETED
        assert resumed.pending_handoffs(reopened) == ()
        result = resumed.handoff_result(reopened, RUN_ID)
        assert result is not None
        assert result["ok"] is True
        assert result["result"] == "handoff answer"
        assert result["usage"] is None
        run = resumed.handoffs(reopened)[0]
        assert run["runId"] == RUN_ID
        assert run["handoff"]["task_id"] == task.id
        assert "usage" not in run["handoff"]["state"]
    finally:
        reopened.close()
