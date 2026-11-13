"""Tests for projection sidecar checkpoints and restore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deeppyro.projection import (
    ProjectionCheckpoints,
    ProjectionDefinition,
    ProjectionError,
    ProjectionRegistry,
    decode_checkpoint,
    encode_checkpoint,
    load_checkpoint,
    read_checkpoint,
    save_checkpoint,
    sidecar_path,
    write_checkpoint,
)
from deeppyro.session.store import SessionStore
from tests.projection import activity_defs, support

FIXTURE_IDS = [
    "session-d745f174",
    "session-04a44e0f",
    "session-16b700bc",
    "session-ddb1ba3d",
    "session-66de8981",
]


def _registry() -> ProjectionRegistry:
    registry = ProjectionRegistry()
    activity_defs.register_all(registry)
    return registry


def _reopen(session: Any) -> Any:
    """Reopen a session from disk, as a fresh process would."""
    return SessionStore(Path(session.path).parent).open(session.session_id)


def _sidecar_matches(session: Any) -> Path:
    return Path(str(session.path) + ".projections.json")


def test_sidecar_path_is_adjacent_to_the_session_file(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    assert sidecar_path(session) == _sidecar_matches(session)
    assert sidecar_path(session).name.endswith(".jsonl.projections.json")
    assert sidecar_path(session).parent == Path(session.path).parent


def test_write_and_read_checkpoint_roundtrip(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_user_message(session, "msg_user", "hello")
    rows = {"todos": {"ver": 1, "seq": session.last_seq, "val": {"todos": []}}}
    path = write_checkpoint(session, rows)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "cells": rows}
    assert read_checkpoint(session) == rows
    leftovers = [item.name for item in path.parent.iterdir() if ".tmp-" in item.name]
    assert leftovers == []


@pytest.mark.parametrize("prefix", FIXTURE_IDS)
@support.requires_fixtures
def test_checkpoint_restore_equals_full_replay(tmp_path: Path, prefix: str) -> None:
    session = support.open_fixture(tmp_path, prefix)
    registry = _registry()
    expected = registry.snapshot(session)
    checkpoint_rows = registry.checkpoint(session)
    assert set(checkpoint_rows) == {
        "activity/todos",
        "activity/jobs",
        "activity/agents",
        "activity/plan_mode",
    }
    save_checkpoint(registry, session)

    reopened = _reopen(session)
    restored = _registry()
    rows = load_checkpoint(restored, reopened)
    assert rows is not None
    assert restored.snapshot(reopened) == expected

    full_replay = _registry().snapshot(reopened)
    assert full_replay == expected
    assert restored.snapshot(reopened) == full_replay


def test_restore_handles_replace_heavy_session(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_system_prompt(session, "sys")
    support.append_user_message(session, "msg_user", "hello")
    seq = session.records[-1].seq
    for index in range(9):
        seq = session.append(
            "user/message",
            support.user_message_data("msg_user", f"replace-{index}"),
            surfaceOp={"op": "replace", "startSeq": seq, "endSeq": seq},
            sourceEventSeqs=[seq],
        )
    assert [record.type for record in session.records].count("user/message") == 10
    assert [(message.role, message.text()) for message in session.derive_messages()] == [
        ("system", "sys"),
        ("user", "replace-8"),
    ]
    registry = _registry()
    expected = registry.snapshot(session)
    save_checkpoint(registry, session)
    reopened = _reopen(session)
    restored = _registry()
    assert load_checkpoint(restored, reopened) is not None
    assert restored.snapshot(reopened) == expected == _registry().snapshot(reopened)


def _counting_definition(calls: list[int], *, key: str = "counter") -> ProjectionDefinition:
    def init(header: Any, inherited: int) -> dict[str, Any]:
        return {"events": []}

    def apply(state: dict[str, Any], record: Any) -> dict[str, Any]:
        calls.append(record.seq)
        if record.type == "todo/write":
            return {"events": state["events"] + [record.seq]}
        return state

    return ProjectionDefinition(key=key, state_version=1, init=init, apply=apply)


def test_restore_only_replays_records_after_the_checkpoint(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    for _ in range(20):
        session.append("todo/write", {"todos": []})
    seed_calls: list[int] = []
    seeded = ProjectionRegistry()
    seeded.register(_counting_definition(seed_calls))
    rows = seeded.checkpoint(session)
    assert rows["counter"]["seq"] == session.last_seq
    assert len(seed_calls) == len(session.records)

    for _ in range(5):
        session.append("todo/write", {"todos": []})
    tail_calls: list[int] = []
    restored = ProjectionRegistry()
    restored.register(_counting_definition(tail_calls))
    restored.restore(session, rows)
    assert tail_calls == [record.seq for record in session.records[-5:]]
    assert len(tail_calls) == 5


def test_stale_state_version_refolds_the_whole_log(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    for _ in range(3):
        session.append("todo/write", {"todos": []})
    stale = {"counter": {"ver": 2, "seq": session.last_seq, "val": {"events": []}}}
    calls: list[int] = []
    registry = ProjectionRegistry()
    registry.register(_counting_definition(calls))
    registry.restore(session, stale)
    assert calls == [record.seq for record in session.records]
    assert len(calls) == len(session.records)


def test_rows_beyond_the_log_end_are_discarded(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    session.append("todo/write", {"todos": []})
    calls: list[int] = []
    registry = ProjectionRegistry()
    registry.register(_counting_definition(calls))
    registry.restore(session, {"counter": {"ver": 1, "seq": session.last_seq + 100, "val": {}}})
    assert calls == [record.seq for record in session.records]


def test_restore_does_not_fire_listener_events(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    session.append("todo/write", {"todos": [{"content": "a", "status": "pending"}]})
    registry = _registry()
    rows = registry.checkpoint(session)
    changes: list[Any] = []
    fresh = _registry()
    fresh.on_changed(lambda sid, key, value: changes.append((sid, key, value)))
    fresh.restore(session, rows)
    assert changes == []
    assert fresh.snapshot(session) == registry.snapshot(session)


def test_missing_sidecar_is_not_an_error(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    assert not sidecar_path(session).exists()
    assert read_checkpoint(session) is None
    registry = _registry()
    assert load_checkpoint(registry, session) is None
    assert ProjectionCheckpoints(registry).load(session) is False


def test_corrupt_sidecar_degrades_to_no_checkpoint(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    session.append("todo/write", {"todos": []})
    sidecar_path(session).write_text("{not json", encoding="utf-8")
    assert read_checkpoint(session) is None
    registry = _registry()
    assert load_checkpoint(registry, session) is None
    checkpoints = ProjectionCheckpoints(registry)
    assert checkpoints.load(session) is False
    assert checkpoints.save(session) is not None
    assert read_checkpoint(session) is not None


def test_projection_checkpoints_toggle(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    support.append_turn(session, 1)
    session.append("todo/write", {"todos": []})
    registry = _registry()
    disabled = ProjectionCheckpoints(registry, enabled=False)
    assert disabled.path_for(session) == sidecar_path(session)
    assert disabled.save(session) is None
    assert disabled.load(session) is False
    assert disabled.restore_if_available(session) is False
    enabled = ProjectionCheckpoints(registry)
    path = enabled.save(session)
    assert path is not None and path.exists()
    assert enabled.load(session) is True


def test_write_checkpoint_rejects_non_json_state(tmp_path: Path) -> None:
    session = support.make_session(tmp_path)
    with pytest.raises(ProjectionError):
        write_checkpoint(session, {"bad": {"ver": 1, "seq": -1, "val": object()}})


def test_encode_checkpoint_validates_rows() -> None:
    with pytest.raises(ProjectionError):
        encode_checkpoint([])  # type: ignore[arg-type]
    with pytest.raises(ProjectionError):
        encode_checkpoint({"": {"ver": 1, "seq": -1, "val": None}})
    with pytest.raises(ProjectionError):
        encode_checkpoint({"k": {"ver": True, "seq": -1, "val": None}})
    with pytest.raises(ProjectionError):
        encode_checkpoint({"k": {"ver": 1, "seq": -2, "val": None}})
    with pytest.raises(ProjectionError):
        encode_checkpoint({"k": {"ver": 1, "seq": -1}})
    assert encode_checkpoint({"k": {"ver": 1, "seq": -1, "val": None}}) == {
        "version": 1,
        "cells": {"k": {"ver": 1, "seq": -1, "val": None}},
    }


def test_decode_checkpoint_is_tolerant() -> None:
    assert decode_checkpoint([]) is None
    assert decode_checkpoint({"version": 99, "cells": {}}) is None
    assert decode_checkpoint({"version": 1, "cells": []}) is None
    rows = decode_checkpoint(
        {
            "version": 1,
            "cells": {
                "good": {"ver": 1, "seq": 4, "val": {"ok": True}},
                "bad-version": {"ver": "1", "seq": 4, "val": None},
                "bad-seq": {"ver": 1, "seq": -2, "val": None},
                "no-val": {"ver": 1, "seq": 4},
            },
        }
    )
    assert rows == {"good": {"ver": 1, "seq": 4, "val": {"ok": True}}}
