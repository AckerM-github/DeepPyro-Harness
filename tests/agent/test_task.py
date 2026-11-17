"""Task and Handoff records: state machine, budgets and strict JSON round-trips."""

from __future__ import annotations

import json

import pytest

from deeppyro.agent.task import (
    DEFAULT_MAX_STEPS,
    MAX_STEPS_CEILING,
    TERMINAL_STATUSES,
    Handoff,
    Task,
    TaskBudget,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
    new_handoff,
    new_task,
    new_task_id,
    parse_status,
)

FIXED = "2026-01-02T03:04:05.000Z"


def test_new_task_defaults() -> None:
    task = new_task("add sqlite memory", created_at=FIXED)
    assert task.id.startswith("task_") and len(task.id) == len("task_") + 16
    assert task.objective == "add sqlite memory"
    assert task.status is TaskStatus.PENDING
    assert task.owner is None
    assert task.parent is None
    assert task.budget == TaskBudget()
    assert task.budget.max_steps == DEFAULT_MAX_STEPS
    assert task.created_at == FIXED
    assert task.is_terminal is False


def test_new_task_with_owner_parent_budget() -> None:
    parent = new_task("parent", created_at=FIXED)
    task = new_task(
        "child",
        owner="worker-1",
        budget=TaskBudget(max_steps=4, max_tokens=1000, max_seconds=2.5),
        parent=parent.id,
        created_at=FIXED,
        task_id="task_0123456789abcdef",
    )
    assert task.owner == "worker-1"
    assert task.parent == parent.id
    assert task.budget == TaskBudget(max_steps=4, max_tokens=1000, max_seconds=2.5)


def test_new_task_ids_are_unique() -> None:
    ids = {new_task_id() for _ in range(1)}
    assert len(ids) == 1


def test_state_machine_happy_path() -> None:
    task = new_task("ship it", created_at=FIXED)
    task = task.transition(TaskStatus.IN_PROGRESS)
    assert task.status is TaskStatus.IN_PROGRESS
    task = task.transition(TaskStatus.BLOCKED)
    task = task.transition(TaskStatus.IN_PROGRESS)
    task = task.transition(TaskStatus.COMPLETED)
    assert task.is_terminal is True
    assert task.allowed_transitions() == ()


def test_state_machine_rejects_illegal_transitions() -> None:
    task = new_task("ship it", created_at=FIXED)
    with pytest.raises(TaskStateError, match="cannot transition"):
        task.transition(TaskStatus.COMPLETED)
    with pytest.raises(TaskStateError):
        task.transition(TaskStatus.BLOCKED)
    done = task.transition(TaskStatus.CANCELLED)
    with pytest.raises(TaskStateError):
        done.transition(TaskStatus.IN_PROGRESS)
    with pytest.raises(TaskValidationError):
        task.transition("completed")  # type: ignore[arg-type]


def test_failure_and_cancel_are_terminal() -> None:
    task = new_task("ship it", created_at=FIXED)
    failed = task.transition(TaskStatus.IN_PROGRESS).transition(TaskStatus.FAILED)
    assert failed.status in TERMINAL_STATUSES
    assert failed.is_terminal is True
    blocked = task.transition(TaskStatus.IN_PROGRESS).transition(TaskStatus.BLOCKED)
    assert blocked.transition(TaskStatus.CANCELLED).is_terminal is True


def test_assign_and_transition_are_pure() -> None:
    task = new_task("review", created_at=FIXED)
    owned = task.assign("worker-2")
    assert task.owner is None
    assert owned.owner == "worker-2"
    assert owned.assign(None).owner is None
    with pytest.raises(TaskValidationError):
        task.assign("   ")


def test_task_json_round_trip() -> None:
    task = new_task(
        "handoff fixture",
        owner="worker-1",
        budget=TaskBudget(max_steps=5, max_tokens=10),
        created_at=FIXED,
    )
    payload = task.to_dict()
    assert payload["status"] == "pending"
    assert json.loads(task.to_json()) == payload
    assert Task.from_dict(json.loads(json.dumps(payload))) == task
    assert Task.from_json(task.to_json()) == task
    assert task.to_json() == Task.from_json(task.to_json()).to_json()


def test_task_json_rejects_bad_payloads() -> None:
    task = new_task("bad payload", created_at=FIXED)
    payload = task.to_dict()
    with pytest.raises(TaskValidationError, match="unknown field"):
        Task.from_dict({**payload, "unexpected": True})
    missing = dict(payload)
    del missing["objective"]
    with pytest.raises(TaskValidationError, match="lacks required field"):
        Task.from_dict(missing)
    with pytest.raises(TaskValidationError, match="unknown task status"):
        Task.from_dict({**payload, "status": "bogus"})
    with pytest.raises(TaskValidationError, match="task id must match"):
        Task.from_dict({**payload, "id": "task_nope"})
    with pytest.raises(TaskValidationError, match="not ISO-8601"):
        Task.from_dict({**payload, "created_at": "yesterday"})
    with pytest.raises(TaskValidationError, match="task objective"):
        Task.from_dict({**payload, "objective": "  "})


def test_budget_validation_and_round_trip() -> None:
    budget = TaskBudget(max_steps=3, max_tokens=1000, max_seconds=1.5)
    assert budget.to_dict() == {"max_steps": 3, "max_tokens": 1000, "max_seconds": 1.5}
    assert TaskBudget.from_dict(budget.to_dict()) == budget
    assert TaskBudget.from_dict({"max_steps": 3}) == TaskBudget(max_steps=3)
    with pytest.raises(TaskValidationError):
        TaskBudget(max_steps=0)
    with pytest.raises(TaskValidationError):
        TaskBudget(max_steps=MAX_STEPS_CEILING + 1)
    with pytest.raises(TaskValidationError):
        TaskBudget(max_tokens=-1)
    with pytest.raises(TaskValidationError):
        TaskBudget(max_seconds=0)
    with pytest.raises(TaskValidationError, match="unknown field"):
        TaskBudget.from_dict({"max_steps": 1, "extra": 2})


def test_parse_status() -> None:
    assert parse_status("in_progress") is TaskStatus.IN_PROGRESS
    assert parse_status(TaskStatus.BLOCKED) is TaskStatus.BLOCKED
    with pytest.raises(TaskStateError):
        parse_status("running")


def test_handoff_round_trip_and_public_keys() -> None:
    handoff = new_handoff(
        "worker-1",
        "worker-2",
        "first half done, tests pending",
        state={"task": "task_0123456789abcdef", "files": ["a.py", "b.py"]},
        open_items=("run pytest", "commit"),
        task_id="task_0123456789abcdef",
        created_at=FIXED,
    )
    payload = handoff.to_dict()
    assert payload["from"] == "worker-1"
    assert payload["to"] == "worker-2"
    assert "from_owner" not in payload
    assert Handoff.from_dict(json.loads(json.dumps(payload))) == handoff
    assert Handoff.from_json(handoff.to_json()) == handoff
    assert handoff.to_json() == json.dumps(payload, ensure_ascii=False, sort_keys=True)


def test_handoff_rejects_bad_state_and_fields() -> None:
    handoff = new_handoff("a", "b", "summary", created_at=FIXED)
    with pytest.raises(TaskValidationError):
        new_handoff("a", "b", "summary", state={"bad": {1, 2}}, created_at=FIXED)
    with pytest.raises(TaskValidationError):
        new_handoff("  ", "b", "summary", created_at=FIXED)
    with pytest.raises(TaskValidationError):
        new_handoff("a", "b", "  ", created_at=FIXED)
    with pytest.raises(TaskValidationError):
        new_handoff("a", "b", "summary", open_items=("",), created_at=FIXED)
    with pytest.raises(TaskValidationError):
        new_handoff("a", "b", "summary", open_items=(), task_id="nope", created_at=FIXED)
    payload = handoff.to_dict()
    with pytest.raises(TaskValidationError, match="unknown field"):
        Handoff.from_dict({**payload, "extra": 1})
    with pytest.raises(TaskValidationError, match="state"):
        Handoff.from_dict({**payload, "state": ["not", "a", "dict"]})
    with pytest.raises(TaskValidationError, match="open_items"):
        Handoff.from_dict({**payload, "open_items": "run pytest"})
    missing = dict(payload)
    del missing["summary"]
    with pytest.raises(TaskValidationError, match="lacks required field"):
        Handoff.from_dict(missing)


def test_task_requires_valid_types() -> None:
    with pytest.raises(TaskValidationError):
        Task(
            id="task_0123456789abcdef", objective="x", status="pending", budget=TaskBudget(), created_at=FIXED
        )  # type: ignore[arg-type]
    with pytest.raises(TaskValidationError):
        Task(
            id="task_0123456789abcdef",
            objective="x",
            status=TaskStatus.PENDING,
            budget={"max_steps": 1},  # type: ignore[arg-type]
            created_at=FIXED,
        )
    with pytest.raises(TaskValidationError):
        Task(
            id="task_0123456789abcdef",
            objective="x",
            status=TaskStatus.PENDING,
            budget=TaskBudget(),
            created_at=FIXED,
            parent="parent_nope",
        )
