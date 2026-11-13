"""Tests for the in-process projection registry."""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.core.wire import LogRecord
from deeppyro.projection import (
    ProjectionDefinition,
    ProjectionError,
    ProjectionRegistry,
    inherited_event_count,
)
from tests.projection import support


def make_definition(
    *,
    key: str = "counter",
    state_version: int = 1,
    summary: bool = True,
    wire: Any = None,
    calls: list[Any] | None = None,
) -> ProjectionDefinition:
    def init(header: Any, inherited: int) -> dict[str, Any]:
        if calls is not None:
            calls.append(("init", header.id, inherited))
        return {"events": [], "inherited": inherited}

    def apply(state: dict[str, Any], record: LogRecord) -> dict[str, Any]:
        if calls is not None:
            calls.append(("apply", record.seq))
        if record.type == "todo/write":
            return {"events": state["events"] + [record.seq], "inherited": state["inherited"]}
        return state

    if wire is None:
        wire = lambda state: tuple(state["events"])
    elif wire is False:
        wire = None
    return ProjectionDefinition(
        key=key,
        state_version=state_version,
        init=init,
        apply=apply,
        wire=wire,
        summary=summary,
    )


def test_register_validates_definitions() -> None:
    registry = ProjectionRegistry()
    with pytest.raises(ProjectionError):
        registry.register("nope")  # type: ignore[arg-type]
    with pytest.raises(ProjectionError):
        registry.register(make_definition(key=""))
    with pytest.raises(ProjectionError):
        registry.register(make_definition(key="   "))
    with pytest.raises(ProjectionError):
        registry.register(make_definition(state_version=-1))
    with pytest.raises(ProjectionError):
        registry.register(make_definition(state_version=True))  # type: ignore[arg-type]
    with pytest.raises(ProjectionError):
        registry.register(make_definition(state_version=1.5))  # type: ignore[arg-type]
    bad = ProjectionDefinition(
        key="bad",
        state_version=1,
        init=lambda header, inherited: None,  # type: ignore[arg-type,return-value]
        apply=lambda state, record: state,
        wire="not-callable",  # type: ignore[arg-type]
    )
    with pytest.raises(ProjectionError):
        registry.register(bad)
    assert registry.keys() == ()


def test_same_key_and_version_shares_one_unit() -> None:
    registry = ProjectionRegistry()
    first = registry.register(make_definition())
    second = registry.register(make_definition())
    assert first.key == second.key == "counter"
    assert first.state_version == 1
    assert registry.keys() == ("counter",)
    second.dispose()
    assert registry.keys() == ("counter",)
    first.dispose()
    assert registry.keys() == ()
    first.dispose()  # idempotent
    assert first.disposed is True


def test_conflicting_state_version_and_summary_raise() -> None:
    registry = ProjectionRegistry()
    registry.register(make_definition(key="todos", state_version=2))
    with pytest.raises(ProjectionError) as excinfo:
        registry.register(make_definition(key="todos", state_version=3))
    assert "state_version 2" in str(excinfo.value)
    assert "state_version 3" in str(excinfo.value)
    with pytest.raises(ProjectionError):
        registry.register(make_definition(key="todos", state_version=2, summary=False))


def _session_with_events(tmp_path: Any) -> Any:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    session.append("todo/write", {"todos": [{"content": "a", "status": "pending"}]})
    session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    return session


def test_state_of_folds_lazily_and_notify_advances(tmp_path: Any) -> None:
    calls: list[Any] = []
    registry = ProjectionRegistry()
    handle = registry.register(make_definition(calls=calls))
    session = _session_with_events(tmp_path)
    assert calls == []
    assert handle.state_of(session)["events"] == [2]
    assert calls[0][0] == "init"
    assert calls[0][2] == 0
    assert handle.state_of(session)["events"] == [2]  # memoized, no replay
    assert len([call for call in calls if call[0] == "apply"]) == 4


def test_notify_fires_once_per_changed_unit(tmp_path: Any) -> None:
    registry = ProjectionRegistry()
    handle = registry.register(make_definition())
    session = support.make_session(tmp_path)
    changes: list[tuple[str, str, Any]] = []
    unsubscribe = registry.on_changed(lambda sid, key, value: changes.append((sid, key, value)))
    support.append_turn(session, 1)
    registry.notify(session, session.records[-1])
    registry.notify(session, session.records[-1])  # duplicate notify is a no-op
    assert changes == []
    session.append("todo/write", {"todos": []})
    record = session.records[-1]
    registry.notify(session, record)
    registry.notify(session, record)
    assert changes == [(session.session_id, "counter", (2,))]
    assert handle.reader_for(session)()["events"] == [2]
    unsubscribe()
    session.append("todo/write", {"todos": []})
    registry.notify(session, session.records[-1])
    assert len(changes) == 1


def test_missed_notifications_self_heal(tmp_path: Any) -> None:
    registry = ProjectionRegistry()
    handle = registry.register(make_definition())
    session = support.make_session(tmp_path)
    changes: list[Any] = []
    registry.on_changed(lambda sid, key, value: changes.append(value))
    support.append_turn(session, 1)
    session.append("todo/write", {"todos": []})
    session.append("todo/write", {"todos": []})
    registry.notify(session, session.records[-1])
    assert handle.state_of(session)["events"] == [2, 3]
    assert changes == [(2, 3)]


def test_identity_gate_returns_same_state_object(tmp_path: Any) -> None:
    registry = ProjectionRegistry()
    handle = registry.register(make_definition())
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    registry.notify(session, session.records[-1])
    before = handle.state_of(session)
    session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    registry.notify(session, session.records[-1])
    after = handle.state_of(session)
    assert after is before
    session.append("todo/write", {"todos": []})
    registry.notify(session, session.records[-1])
    changed = handle.state_of(session)
    assert changed is not before
    assert changed["events"] == [session.records[-1].seq]


def test_register_after_events_folds_the_whole_log(tmp_path: Any) -> None:
    session = _session_with_events(tmp_path)
    registry = ProjectionRegistry()
    late = registry.register(make_definition(key="late"))
    assert late.state_of(session)["events"] == [2]
    assert registry.as_of_seq(session) == session.last_seq
    assert registry.snapshot(session) == {"late": (2,)}


def test_snapshot_only_includes_live_wire_units(tmp_path: Any) -> None:
    session = _session_with_events(tmp_path)
    registry = ProjectionRegistry()
    with_wire = registry.register(make_definition(key="with-wire"))
    without_wire = registry.register(make_definition(key="no-wire", wire=False))
    assert set(registry.snapshot(session)) == {"with-wire"}
    assert set(registry.snapshot(session, keys=["with-wire"])) == {"with-wire"}
    assert registry.snapshot(session, keys=["no-wire"]) == {}
    without_wire.dispose()
    with_wire.dispose()
    assert registry.snapshot(session) == {}
    assert registry.keys() == ()


def test_listener_errors_are_swallowed(tmp_path: Any) -> None:
    registry = ProjectionRegistry()
    registry.register(make_definition())
    seen: list[Any] = []

    def broken(session_id: str, key: str, value: Any) -> None:
        raise RuntimeError("listener boom")

    registry.on_changed(broken)
    registry.on_changed(lambda sid, key, value: seen.append(value))
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    registry.notify(session, session.records[-1])
    session.append("todo/write", {"todos": []})
    registry.notify(session, session.records[-1])
    assert seen == [(2,)]


def test_checkpoint_rows_are_deep_copied(tmp_path: Any) -> None:
    registry = ProjectionRegistry()
    handle = registry.register(make_definition())
    session = _session_with_events(tmp_path)
    rows = registry.checkpoint(session)
    assert rows == {"counter": {"ver": 1, "seq": session.last_seq, "val": {"events": [2], "inherited": 0}}}
    rows["counter"]["val"]["events"].append(999)
    assert handle.state_of(session)["events"] == [2]
    handle.dispose()
    released_rows = registry.checkpoint(session)
    assert released_rows["counter"]["seq"] == session.last_seq


def test_inherited_event_count_marker(tmp_path: Any) -> None:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    assert inherited_event_count(session) == 0
    # Simulate a fork cut: the marker is the first inherited end-seed record.
    session.append("session/end-seed", {"inherited": True})
    session.append("turn/end", {"turn": 1, "reason": {"kind": "forked"}})
    assert inherited_event_count(session) == 2


def test_state_of_unknown_key_raises() -> None:
    registry = ProjectionRegistry()
    with pytest.raises(ProjectionError):
        registry.state_of(None, "missing")  # type: ignore[arg-type]
