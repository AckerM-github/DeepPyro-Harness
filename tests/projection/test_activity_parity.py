"""Parity tests: projection definitions reproduce ActivityService.refresh."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deeppyro.builtin.activity import ActivityService
from deeppyro.projection import ProjectionRegistry
from tests.projection import activity_defs, support

FIXTURE_IDS = [
    "session-d745f174",
    "session-04a44e0f",
    "session-16b700bc",
    "7bed18f9",
    "7fdb12e1",
    "session-ddb1ba3d",
    "session-66de8981",
]

KEYS = ("activity/todos", "activity/jobs", "activity/agents", "activity/plan_mode")


def _assert_parity(session: object) -> None:
    state = ActivityService().refresh(session)  # type: ignore[arg-type]
    registry = ProjectionRegistry()
    activity_defs.register_all(registry)
    values = registry.snapshot(session)  # type: ignore[arg-type]
    assert set(values) == set(KEYS)
    assert values["activity/todos"] == state.todos
    assert values["activity/jobs"] == state.jobs
    assert values["activity/agents"] == state.agents
    assert values["activity/plan_mode"] == state.plan_mode


@support.requires_fixtures
@pytest.mark.parametrize("prefix", FIXTURE_IDS)
def test_fixture_parity(tmp_path: Path, prefix: str) -> None:
    _assert_parity(support.open_fixture(tmp_path, prefix))


def test_synthetic_team_and_plan_records(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    session.append(
        "todo/write",
        {
            "todos": [
                {"content": "first", "status": "pending"},
                {"content": "second", "status": "in_progress"},
                {"content": "second", "status": "completed"},
                {"content": "", "status": "pending"},
                {"content": "bad", "status": "unknown"},
            ]
        },
    )
    session.append("plan/mode", {"mode": "plan"})
    session.append(
        "team/member",
        {
            "member": {
                "id": "member-1",
                "name": "builder",
                "description": "builds",
                "phase": "running",
                "provider": "p",
                "model": "m",
            }
        },
    )
    session.append(
        "subagent/descriptor",
        {
            "childId": "child-1",
            "label": "helper",
            "mode": "continuable",
            "agentProvider": "prov",
            "agentModel": "mod",
        },
    )
    session.append(
        "subagent/catalog",
        {
            "childId": "child-2",
            "label": "listed",
            "mode": "readonly",
        },
    )
    session.append(
        "job/registered",
        {
            "jobId": "job-1",
            "kind": "exec",
            "label": "Run",
            "owner": "builder",
            "startedAt": 10,
            "toolCall": {"callId": "call-1"},
        },
    )
    session.append("job/settled", {"jobId": "job-1", "status": "done", "finishedAt": 20})
    _assert_parity(session)

    registry = ProjectionRegistry()
    handles = activity_defs.register_all(registry)
    events: list[Any] = []
    registry.on_changed(lambda sid, key, value: events.append(key))
    for record in session.records:
        registry.notify(session, record)
    state = ActivityService().refresh(session)
    values = registry.snapshot(session)
    assert values["activity/todos"] == state.todos
    assert values["activity/jobs"] == state.jobs
    assert values["activity/agents"] == state.agents
    assert values["activity/plan_mode"] == state.plan_mode
    assert set(handles) == set(KEYS)
    assert events  # notifications fired for changed units
    assert values["activity/plan_mode"] == "plan"
    assert [todo.status for todo in values["activity/todos"]] == ["pending", "in_progress"]
