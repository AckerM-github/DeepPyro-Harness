"""KI-017: the task service folds the six-state machine out of session facts."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.agent.task import TASK_ID_RE, Task, TaskBudget, TaskStateError, TaskStatus, TaskValidationError
from deeppyro.builtin.task_tools import (
    HANDOFF_EVENT,
    HANDOFF_RESULT_EVENT,
    TASK_EVENT,
    TaskPlugin,
    TaskService,
    task_tools,
)
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import PluginSpec, boot
from deeppyro.session.session import Session
from deeppyro.session.store import SessionStore


def _open(tmp_path: Path) -> tuple[TaskService, Session, SessionStore]:
    store = SessionStore(root=tmp_path / "sessions")
    session = store.create(tmp_path)
    return TaskService(), session, store


def test_create_persists_a_task_fact(tmp_path: Path) -> None:
    service, session, _store = _open(tmp_path)

    task = service.create(session, "ship the release", owner="lead", budget=TaskBudget(max_tokens=100))

    assert TASK_ID_RE.match(task.id)
    assert task.status is TaskStatus.PENDING
    assert task.owner == "lead"
    assert task.budget.max_tokens == 100
    assert service.tasks(session) == (task,)
    assert service.get(session, task.id) == task
    assert service.unfinished(session) == (task,)
    facts = [record for record in session.records if record.type == TASK_EVENT]
    assert len(facts) == 1
    assert facts[0].ignorable is True
    assert facts[0].data["version"] == 1
    assert facts[0].data["action"] == "create"
    assert facts[0].data["task"]["id"] == task.id
    assert Task.from_dict(facts[0].data["task"]) == task
    assert facts[0].data["task"]["budget"] == {"max_steps": 32, "max_tokens": 100}


def test_parent_task_requires_an_existing_parent(tmp_path: Path) -> None:
    service, session, _store = _open(tmp_path)
    parent = service.create(session, "parent")
    child = service.create(session, "child", parent=parent.id)
    assert child.parent == parent.id
    with pytest.raises(TaskValidationError, match="unknown task"):
        service.create(session, "orphan", parent="task_0000000000000000")


def test_six_state_machine_transitions(tmp_path: Path) -> None:
    service, session, _store = _open(tmp_path)
    task = service.create(session, "walk the states")

    started = service.update(session, task.id, TaskStatus.IN_PROGRESS)
    assert started.status is TaskStatus.IN_PROGRESS
    blocked = service.update(session, task.id, "blocked")
    assert blocked.status is TaskStatus.BLOCKED
    restarted = service.update(session, task.id, TaskStatus.IN_PROGRESS)
    assert restarted.status is TaskStatus.IN_PROGRESS
    completed = service.complete(session, task.id)
    assert completed.status is TaskStatus.COMPLETED
    assert completed.is_terminal
    assert service.unfinished(session) == ()

    failed_task = service.create(session, "fails")
    failed = service.update(session, failed_task.id, TaskStatus.IN_PROGRESS)
    failed = service.update(session, failed.id, TaskStatus.FAILED)
    cancelled_task = service.create(session, "cancels")
    cancelled = service.update(session, cancelled_task.id, TaskStatus.CANCELLED)
    assert (failed.status, cancelled.status) == (TaskStatus.FAILED, TaskStatus.CANCELLED)
    assert all(record.ignorable for record in session.records if record.type == TASK_EVENT)


def test_illegal_transitions_are_diagnosable(tmp_path: Path) -> None:
    service, session, _store = _open(tmp_path)
    task = service.create(session, "cannot skip states")

    with pytest.raises(TaskStateError, match="cannot transition from pending to completed"):
        service.update(session, task.id, TaskStatus.COMPLETED)
    with pytest.raises(TaskStateError, match="allowed: in_progress, cancelled"):
        service.update(session, task.id, TaskStatus.COMPLETED)
    with pytest.raises(TaskStateError, match="is already pending"):
        service.update(session, task.id, TaskStatus.PENDING)
    with pytest.raises(TaskStateError, match="unknown task status"):
        service.update(session, task.id, "running")
    with pytest.raises(TaskValidationError, match="task id must match"):
        service.get(session, "not-a-task")
    with pytest.raises(TaskValidationError, match="unknown task"):
        service.get(session, "task_ffffffffffffffff")

    service.update(session, task.id, TaskStatus.IN_PROGRESS)
    service.complete(session, task.id)
    with pytest.raises(TaskStateError, match="cannot transition from completed to in_progress"):
        service.update(session, task.id, TaskStatus.IN_PROGRESS)
    with pytest.raises(TaskStateError, match="allowed: none"):
        service.update(session, task.id, TaskStatus.BLOCKED)


def test_assign_and_owner_updates_persist(tmp_path: Path) -> None:
    service, session, _store = _open(tmp_path)
    task = service.create(session, "owned work")
    assigned = service.assign(session, task.id, "worker-7")
    assert assigned.owner == "worker-7"
    assert service.get(session, task.id).owner == "worker-7"
    updated = service.update(session, task.id, TaskStatus.IN_PROGRESS, owner="worker-8")
    assert (updated.owner, updated.status) == ("worker-8", TaskStatus.IN_PROGRESS)


def test_reopened_session_restores_unfinished_tasks(tmp_path: Path) -> None:
    service, session, store = _open(tmp_path)
    done = service.create(session, "already done")
    service.update(session, done.id, TaskStatus.IN_PROGRESS)
    service.complete(session, done.id)
    live = service.create(session, "still running")
    service.update(session, live.id, TaskStatus.IN_PROGRESS)
    blocked = service.create(session, "waiting")
    service.update(session, blocked.id, TaskStatus.IN_PROGRESS)
    service.update(session, blocked.id, TaskStatus.BLOCKED)

    reopened = store.open(session.session_id)
    resumed = TaskService()

    assert {task.id for task in resumed.tasks(reopened)} == {done.id, live.id, blocked.id}
    assert {task.id for task in resumed.unfinished(reopened)} == {live.id, blocked.id}
    assert resumed.get(reopened, done.id).status is TaskStatus.COMPLETED
    assert resumed.get(reopened, live.id).status is TaskStatus.IN_PROGRESS
    assert resumed.tasks(reopened) == service.tasks(session)


def test_handoff_facts_survive_reopen(tmp_path: Path) -> None:
    from deeppyro.agent.task import new_handoff

    service, session, store = _open(tmp_path)
    task = service.create(session, "delegated work")
    handoff = new_handoff(
        "lead",
        "subagent:handoff-abc",
        "carry the context",
        state={"turn": 1, "budget": {"max_steps": 32}, "usage": {"input": 10, "output": 5, "cached": 2}},
        open_items=("finish the report",),
        task_id=task.id,
    )
    service.start_handoff(session, handoff, run_id="handoff-abc")
    assert [fact["runId"] for fact in service.pending_handoffs(session)] == ["handoff-abc"]

    service.finish_handoff(session, run_id="handoff-abc", task_id=task.id, ok=True, result="report finished")
    service.update(session, task.id, TaskStatus.IN_PROGRESS)
    service.complete(session, task.id)

    reopened = store.open(session.session_id)
    resumed = TaskService()

    assert [fact["runId"] for fact in resumed.handoffs(reopened)] == ["handoff-abc"]
    assert resumed.pending_handoffs(reopened) == ()
    result = resumed.handoff_result(reopened, "handoff-abc")
    assert result is not None
    assert result["ok"] is True
    assert result["result"] == "report finished"
    assert resumed.get(reopened, task.id).status is TaskStatus.COMPLETED
    handoff_records = [record for record in reopened.records if record.type == HANDOFF_EVENT]
    assert handoff_records[0].ignorable is True
    assert handoff_records[0].data["handoff"]["open_items"] == ["finish the report"]
    result_records = [record for record in reopened.records if record.type == HANDOFF_RESULT_EVENT]
    assert result_records[0].ignorable is True


def test_cached_fold_sees_new_records(tmp_path: Path) -> None:
    service, session, _store = _open(tmp_path)
    assert service.tasks(session) == ()
    task = service.create(session, "fresh")
    assert service.tasks(session) == (task,)
    service.update(session, task.id, TaskStatus.IN_PROGRESS)
    service.complete(session, task.id)
    assert service.get(session, task.id).status is TaskStatus.COMPLETED


def test_task_tools_are_the_five_plugin_tools() -> None:
    tools = task_tools(TaskService())
    assert [tool.name for tool in tools] == [
        "task_create",
        "task_update",
        "task_complete",
        "task_list",
        "task_handoff",
    ]
    assert all(tool.parallel_safe is False for tool in tools if tool.name != "task_list")
    assert TaskPlugin.provide == "task"
    assert TaskPlugin.plugin_id == "task"
    assert list(TaskPlugin.inject) == ["tools"]


async def test_task_plugin_boots_and_registers_tools() -> None:
    config = Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )
    spec = PluginSpec(id="task", name="deeppyro.builtin.task_tools:TaskPlugin", after="agents")
    app = await boot(config, extra_plugins=[spec])
    try:
        assert "task" in app.started
        service = app.require(TaskService)
        assert isinstance(service, TaskService)
        tools = app.require(ToolsPlugin)
        names = set(tools.names("C:/work"))
        assert {"task_create", "task_update", "task_complete", "task_list", "task_handoff"} <= names
    finally:
        await app.stop()
