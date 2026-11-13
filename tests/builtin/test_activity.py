"""Tests for the session activity projection service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deeppyro.builtin.activity import (
    ActivityPlugin,
    ActivityService,
    ActivityState,
    current_todo,
    parse_todos,
    plan_counts,
)


@dataclass
class Record:
    type: str
    data: dict[str, Any]


class FakeSession:
    def __init__(self, records: list[Record]) -> None:
        self.records = records


def test_parse_todos_keeps_valid_items_in_order() -> None:
    todos = parse_todos(
        [
            {"content": "read the repo", "status": "completed"},
            {"content": "write code", "status": "in_progress"},
            {"content": "  ", "status": "pending"},
            {"content": "bad status", "status": "blocked"},
            "not a table",
            {"content": "read the repo", "status": "pending"},
        ]
    )
    assert [(item.content, item.status) for item in todos] == [
        ("read the repo", "completed"),
        ("write code", "in_progress"),
    ]


def test_plan_counts_and_current_todo() -> None:
    todos = parse_todos(
        [
            {"content": "a", "status": "pending"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "completed"},
        ]
    )
    assert plan_counts(todos) == {
        "pending": 1,
        "in_progress": 1,
        "completed": 1,
        "total": 3,
    }
    assert current_todo(todos) is todos[1]
    assert current_todo(todos[:1]) is todos[0]
    assert current_todo(()) is None


def test_refresh_projects_todos_jobs_agents_and_plan_mode() -> None:
    service = ActivityService()
    session = FakeSession(
        [
            Record("todo/write", {"todos": [{"content": "ship it", "status": "in_progress"}]}),
            Record(
                "job/registered",
                {
                    "jobId": "job-1",
                    "kind": "shell",
                    "label": "pytest",
                    "owner": "agent",
                    "startedAt": 10,
                    "toolCall": {"callId": "call-1"},
                },
            ),
            Record("job/settled", {"jobId": "job-1", "status": "done", "finishedAt": 20}),
            Record(
                "team/member",
                {
                    "member": {
                        "id": "member-1",
                        "name": "builder",
                        "description": "builds",
                        "phase": "running",
                        "provider": "local",
                        "model": "m",
                    }
                },
            ),
            Record(
                "subagent/descriptor",
                {"childId": "sub-1", "label": "researcher", "mode": "async", "agentModel": "m2"},
            ),
            Record("plan/mode", {"mode": "plan"}),
        ]
    )
    state = service.refresh(session)
    assert state.todos[0].content == "ship it"
    assert state.plan_mode == "plan"
    job = state.jobs[0]
    assert (job.id, job.kind, job.label, job.status) == ("job-1", "shell", "pytest", "done")
    assert job.finished_at == 20
    assert job.call_id == "call-1"
    assert [agent.id for agent in state.agents] == ["member-1", "sub-1"]
    assert state.agents[0].phase == "running"
    assert state.agents[1].model == "m2"
    assert state.revision == 1
    assert state.has_items


def test_refresh_is_stable_and_bumps_revision_only_on_change() -> None:
    service = ActivityService()
    session = FakeSession([Record("todo/write", {"todos": [{"content": "a", "status": "pending"}]})])
    first = service.refresh(session)
    second = service.refresh(session)
    assert second is first
    assert second.revision == 1
    session.records.append(Record("todo/write", {"todos": [{"content": "a", "status": "completed"}]}))
    third = service.refresh(session)
    assert third.revision == 2
    assert third.todos[0].status == "completed"


def test_refresh_tolerates_missing_or_malformed_records() -> None:
    service = ActivityService()
    assert service.refresh(object()) is service.state
    assert service.refresh(FakeSession([])) is service.state
    state = service.refresh(
        FakeSession(
            [
                Record("todo/write", {"todos": "nope"}),
                Record("job/registered", {"jobId": ""}),
                Record("team/member", {"member": None}),
                Record("unknown/type", {}),
            ]
        )
    )
    assert state == ActivityState(revision=0)


def test_subscribe_notifies_and_unsubscribes() -> None:
    service = ActivityService()
    seen: list[int] = []
    unsubscribe = service.subscribe(lambda state: seen.append(state.revision))
    session = FakeSession([Record("todo/write", {"todos": [{"content": "a", "status": "pending"}]})])
    service.refresh(session)
    assert seen == [1]
    unsubscribe()
    session.records.append(Record("todo/write", {"todos": []}))
    service.refresh(session)
    assert seen == [1]


def test_bad_listener_does_not_break_refresh() -> None:
    service = ActivityService()

    def explode(state: ActivityState) -> None:
        raise RuntimeError("boom")

    service.subscribe(explode)
    state = service.refresh(
        FakeSession([Record("todo/write", {"todos": [{"content": "a", "status": "pending"}]})])
    )
    assert state.has_items


def test_plugin_registers_the_service() -> None:
    provided: dict[str, Any] = {}

    class Ctx:
        def provide(self, name: str, value: Any) -> None:
            provided[name] = value

    plugin = ActivityPlugin(Ctx(), {})
    plugin.setup(Ctx(), {})
    assert plugin.plugin_id == "activity"
    assert isinstance(plugin.service, ActivityService)
