"""Tests for the shared team task board."""

from __future__ import annotations

import pytest

from deeppyro.team import TaskInfo, TeamError
from tests.team.helpers import make_team, records


def test_create_task_fields_and_record(tmp_path) -> None:
    env = make_team(tmp_path)
    task = env.service.create_task(
        "Write team tests",
        "cover the shared board",
        write_scopes=["tests/team"],
    )
    assert isinstance(task, TaskInfo)
    assert task.id.startswith("task-")
    assert task.revision == 1
    assert task.subject == "Write team tests"
    assert task.description == "cover the shared board"
    assert task.status == "pending"
    assert task.blocked_by == ()
    assert task.write_scopes == ("tests/team",)
    assert task.owner == ""
    assert task.created_at > 0

    record = records(env.parent, "team/task")[-1]
    assert record == {
        "version": 1,
        "teamId": env.parent.session_id,
        "task": {
            "id": task.id,
            "revision": 1,
            "subject": "Write team tests",
            "description": "cover the shared board",
            "status": "pending",
            "blockedBy": [],
            "writeScopes": ["tests/team"],
        },
    }
    assert env.service.tasks() == [task]
    assert env.service.get_task(task.id) == task
    assert env.service.get_task("task-unknown") is None


def test_create_task_rejects_unknown_blocker(tmp_path) -> None:
    env = make_team(tmp_path)
    with pytest.raises(TeamError, match="unknown blocker"):
        env.service.create_task("Dependent", blocked_by=["missing"])
    assert env.service.tasks() == []


def test_claim_assigns_owner_and_blocks_on_dependency(tmp_path) -> None:
    env = make_team(tmp_path)
    blocker = env.service.create_task("Design the API")
    dependent = env.service.create_task(
        "Implement the API",
        blocked_by=[blocker.id],
        write_scopes=["src/deeppyro/team"],
    )
    with pytest.raises(TeamError, match="is blocked by"):
        env.service.update_task(dependent.id, "start")
    with pytest.raises(TeamError, match="is blocked by"):
        env.service.update_task(dependent.id, "claim")

    claimed = env.service.update_task(blocker.id, "claim")
    assert claimed.status == "in_progress"
    assert claimed.owner == "lead"
    assert claimed.revision == 2

    completed = env.service.update_task(blocker.id, "complete")
    assert completed.status == "completed"
    assert completed.revision == 3
    started = env.service.update_task(dependent.id, "claim")
    assert started.status == "in_progress"
    assert started.owner == "lead"
    assert started.write_scopes == ("src/deeppyro/team",)


def test_start_completed_task_is_rejected(tmp_path) -> None:
    env = make_team(tmp_path)
    task = env.service.create_task("Done work")
    env.service.update_task(task.id, "complete")
    with pytest.raises(TeamError, match="already completed"):
        env.service.update_task(task.id, "start")
    reopened = env.service.update_task(task.id, "reopen")
    assert reopened.status == "pending"
    started = env.service.update_task(task.id, "start")
    assert started.status == "in_progress"


def test_edit_updates_fields_and_revision(tmp_path) -> None:
    env = make_team(tmp_path)
    task = env.service.create_task("Draft", "first draft")
    edited = env.service.update_task(
        task.id,
        "edit",
        subject="Final draft",
        description="revised",
        blocked_by=(),
        write_scopes=["docs/spec/team.md"],
    )
    assert edited.subject == "Final draft"
    assert edited.description == "revised"
    assert edited.blocked_by == ()
    assert edited.write_scopes == ("docs/spec/team.md",)
    assert edited.revision == 2
    assert edited.status == "pending"

    with pytest.raises(TeamError, match="at least one changed field"):
        env.service.update_task(task.id, "edit")
    with pytest.raises(TeamError, match="unknown task action"):
        env.service.update_task(task.id, "archive")
    with pytest.raises(TeamError, match="unknown team task"):
        env.service.update_task("task-unknown", "claim")


def test_edit_cannot_self_block(tmp_path) -> None:
    env = make_team(tmp_path)
    task = env.service.create_task("Self")
    with pytest.raises(TeamError, match="cannot block itself"):
        env.service.update_task(task.id, "edit", blocked_by=[task.id])


def test_blockers_can_be_created_by_subject(tmp_path) -> None:
    env = make_team(tmp_path)
    blocker = env.service.create_task("Design")
    dependent = env.service.create_task("Implement", blocked_by="Design")
    assert dependent.blocked_by == ("Design",)
    with pytest.raises(TeamError, match="is blocked by Design"):
        env.service.update_task(dependent.id, "start")
    env.service.update_task(blocker.id, "complete")
    started = env.service.update_task(dependent.id, "start")
    assert started.status == "in_progress"


def test_every_update_appends_revision_record(tmp_path) -> None:
    env = make_team(tmp_path)
    task = env.service.create_task("Track revisions")
    env.service.update_task(task.id, "claim")
    env.service.update_task(task.id, "complete")
    task_records = records(env.parent, "team/task")
    assert [item["task"]["revision"] for item in task_records] == [1, 2, 3]
    assert [item["task"]["status"] for item in task_records] == [
        "pending",
        "in_progress",
        "completed",
    ]
    assert all(item["version"] == 1 for item in task_records)
    assert all(item["teamId"] == env.parent.session_id for item in task_records)


def test_tasks_preserve_insertion_order(tmp_path) -> None:
    env = make_team(tmp_path)
    first = env.service.create_task("First")
    second = env.service.create_task("Second")
    assert [task.id for task in env.service.tasks()] == [first.id, second.id]
