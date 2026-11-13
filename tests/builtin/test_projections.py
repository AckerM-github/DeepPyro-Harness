"""Tests for the bundled projections service and Session append observers."""

from __future__ import annotations

import json
from pathlib import Path

from deeppyro.builtin.projections import (
    PROJECTION_KEYS,
    ProjectionsService,
    definitions,
)
from deeppyro.session.session import Session
from deeppyro.session.store import SessionStore


def _make_session(tmp_path: Path) -> Session:
    return SessionStore(Path(tmp_path) / "sessions").create(Path(tmp_path))


def _write_todos(session: Session, *items: tuple[str, str]) -> int:
    return session.append(
        "todo/write",
        {"todos": [{"content": content, "status": status} for content, status in items]},
    )


def test_production_definitions_cover_the_activity_keys() -> None:
    assert tuple(definition.key for definition in definitions()) == PROJECTION_KEYS


def test_attach_observes_direct_session_appends(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    service = ProjectionsService()
    changes: list[str] = []
    service.registry.on_changed(lambda _sid, key, _value: changes.append(key))

    assert service.attach(session) is False

    seq = _write_todos(session, ("read", "in_progress"), ("write", "pending"))
    assert changes[-1] == "activity/todos"
    todo_row = service.checkpoint(session)["activity/todos"]
    assert todo_row == {
        "ver": 1,
        "seq": seq,
        "val": [
            {"content": "read", "status": "in_progress"},
            {"content": "write", "status": "pending"},
        ],
    }

    job_seq = session.append(
        "job/registered",
        {
            "jobId": "job-1",
            "kind": "exec",
            "label": "Run tests",
            "owner": "builder",
            "startedAt": 10,
            "toolCall": {"callId": "call-1"},
        },
    )
    jobs = service.snapshot(session)["activity/jobs"]
    assert len(jobs) == 1
    assert jobs[0].id == "job-1"
    assert jobs[0].status == "running"
    assert service.checkpoint(session)["activity/jobs"]["seq"] == job_seq


def test_detach_stops_observing_but_leaves_sidecar_alone(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    service = ProjectionsService()
    service.attach(session)
    changes: list[str] = []
    service.registry.on_changed(lambda _sid, key, _value: changes.append(key))

    service.detach(session)
    _write_todos(session, ("after-detach", "pending"))
    assert changes == []
    # Lazy snapshot still folds the log; detach only removes the incremental hook.
    assert [item.content for item in service.snapshot(session)["activity/todos"]] == ["after-detach"]


def test_save_and_restore_round_trip_through_the_sidecar(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    service = ProjectionsService()
    service.attach(session)
    _write_todos(session, ("read", "completed"), ("write", "in_progress"))
    session.append("plan/mode", {"mode": "plan"})
    expected = service.snapshot(session)

    sidecar = service.save(session)
    assert sidecar is not None and sidecar.exists()
    payload = json.loads(Path(sidecar).read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["cells"]) == set(PROJECTION_KEYS)
    assert payload["cells"]["activity/plan_mode"]["val"] == "plan"

    restored = ProjectionsService()
    assert restored.attach(session) is True
    assert restored.snapshot(session) == expected


def test_broken_append_observer_never_breaks_the_session(tmp_path: Path) -> None:
    session = _make_session(tmp_path)

    def explode(_session: Session, _record: object) -> None:
        raise RuntimeError("observer boom")

    session.add_append_observer(explode)
    seq = _write_todos(session, ("still-written", "pending"))
    assert seq == 0
    assert session.records[-1].type == "todo/write"
    assert session.last_seq == 0
