"""Crash-safe native forks: full log copies with a reparented header."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.session.native_fork import (
    NATIVE_FORK_CORRUPT,
    NATIVE_FORK_EXISTS,
    NATIVE_FORK_INVALID,
    NATIVE_FORK_NOT_FOUND,
    NativeForkError,
    fork_native_session,
    forked_event_count,
)
from deeppyro.session.native_index import list_native_sessions, read_native_log
from deeppyro.session.schema import EventKind, NativeSession
from tests.session.native_helpers import (
    CREATED_AT,
    CWD,
    SESSION_ID,
    TITLE,
    write_native,
)

OLDER_ID = "session-00000000-0000-0000-0000-000000000001"
NEWER_ID = "session-00000000-0000-0000-0000-000000000002"


def _fork(source_id: str = OLDER_ID, **kwargs: object) -> NativeSession:
    return fork_native_session(source_id, **kwargs)  # type: ignore[arg-type]


def test_fork_copies_header_and_reparents(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=OLDER_ID)
    source = read_native_log(OLDER_ID, root=tmp_path)
    stamp = CREATED_AT + 60_000

    child = _fork(OLDER_ID, root=tmp_path, new_id=NEWER_ID, created_at=stamp)

    try:
        assert child.header.id == NEWER_ID
        assert child.header.parent_id == OLDER_ID
        assert child.header.origin == "fork"
        assert child.header.delegation_depth == source.header.delegation_depth + 1
        assert child.header.cwd == source.header.cwd
        assert child.header.preset == source.header.preset
        assert child.header.created_at == stamp
        assert len(child.events) == len(source.events)
        delta = stamp - source.header.created_at
        for copied, original in zip(child.events, source.events, strict=True):
            assert copied.type == original.type
            assert copied.seq == original.seq
            assert copied.data == original.data
            assert copied.ts == original.ts + delta
        stamps = [event.ts for event in child.events]
        assert stamps == sorted(stamps)
        assert stamps[0] >= stamp
    finally:
        child.close()

    infos = {info.id: info for info in list_native_sessions(tmp_path)}
    assert set(infos) == {OLDER_ID, NEWER_ID}
    assert infos[NEWER_ID].parent_id == OLDER_ID
    assert infos[NEWER_ID].origin == "fork"
    assert infos[NEWER_ID].title == TITLE


def test_fork_is_appendable_and_listed(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=OLDER_ID, title=None)
    child = fork_native_session(OLDER_ID, root=tmp_path, new_id=NEWER_ID, created_at=CREATED_AT + 1_000)
    try:
        expected_seq = child.last_seq + 1
        event = child.append(EventKind.SESSION_TITLE, {"title": "Forked work", "source": "user"})
        assert event.seq == expected_seq
    finally:
        child.close()

    log = read_native_log(NEWER_ID, root=tmp_path)
    assert len(log.events) == 9
    assert log.events[-1].type == EventKind.SESSION_TITLE.value
    assert list_native_sessions(tmp_path)[0].title == "Forked work"


def test_fork_reports_unknown_and_corrupt_sources(tmp_path: Path) -> None:
    with pytest.raises(NativeForkError) as missing:
        _fork(root=tmp_path)
    assert missing.value.code == NATIVE_FORK_NOT_FOUND

    Path(tmp_path / f"{OLDER_ID}.jsonl").write_text("not json\n", encoding="utf-8")
    with pytest.raises(NativeForkError) as corrupt:
        _fork(OLDER_ID, root=tmp_path)
    assert corrupt.value.code == NATIVE_FORK_CORRUPT


def test_fork_rejects_existing_target_and_invalid_ids(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=OLDER_ID)
    write_native(tmp_path, session_id=NEWER_ID, created_at=CREATED_AT + 10)

    with pytest.raises(NativeForkError) as exists:
        _fork(OLDER_ID, root=tmp_path, new_id=NEWER_ID, created_at=CREATED_AT + 1_000)
    assert exists.value.code == NATIVE_FORK_EXISTS

    with pytest.raises(NativeForkError) as invalid:
        _fork(OLDER_ID, root=tmp_path, new_id="not-a-session", created_at=CREATED_AT + 1_000)
    assert invalid.value.code == NATIVE_FORK_INVALID

    with pytest.raises(NativeForkError) as bad_stamp:
        _fork(OLDER_ID, root=tmp_path, new_id=NEWER_ID, created_at=-1)
    assert bad_stamp.value.code == NATIVE_FORK_INVALID

    assert {info.id for info in list_native_sessions(tmp_path)} == {OLDER_ID, NEWER_ID}


def test_fork_of_archived_session_keeps_the_marker(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=OLDER_ID, archived=True)

    child = fork_native_session(OLDER_ID, root=tmp_path, new_id=NEWER_ID, created_at=CREATED_AT + 1_000)
    try:
        assert len(child.events) == 10
    finally:
        child.close()

    assert [info.id for info in list_native_sessions(tmp_path)] == []
    archived = {info.id: info for info in list_native_sessions(tmp_path, include_archived=True)}
    assert archived[NEWER_ID].archived is True


def test_fork_of_header_only_session_yields_empty_log(tmp_path: Path) -> None:
    path = tmp_path / f"{SESSION_ID}.jsonl"
    with NativeSession.create(path, cwd=CWD):
        pass

    child = fork_native_session(SESSION_ID, root=tmp_path, new_id=NEWER_ID, created_at=CREATED_AT + 1_000)
    try:
        assert child.events == ()
        assert child.header.id == NEWER_ID
    finally:
        child.close()

    assert read_native_log(NEWER_ID, root=tmp_path).events == ()
    assert len(list_native_sessions(tmp_path, include_archived=True)) == 2


def test_prefix_fork_copies_a_consistent_prefix(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=OLDER_ID)
    source = read_native_log(OLDER_ID, root=tmp_path)
    stamp = CREATED_AT + 60_000
    bound = 7  # mid-turn: ends at step.ended, no synthetic turn closer is added

    child = _fork(OLDER_ID, root=tmp_path, new_id=NEWER_ID, created_at=stamp, through_seq=bound)
    try:
        assert child.header.origin == "fork"
        assert child.header.parent_id == OLDER_ID
        assert child.header.delegation_depth == source.header.delegation_depth + 1
        assert [event.seq for event in child.events] == list(range(1, bound + 1))
        assert child.last_seq == bound
        delta = stamp - source.header.created_at
        for copied, original in zip(child.events, source.events[:bound], strict=True):
            assert copied.type == original.type
            assert copied.seq == original.seq
            assert copied.data == original.data
            assert copied.ts == original.ts + delta
        assert child.events[-1].type == EventKind.STEP_ENDED.value
        assert EventKind.TURN_ENDED.value not in {event.type for event in child.events}
        assert forked_event_count(child) == bound
        appended = child.append(EventKind.SESSION_TITLE, {"title": "Prefix work", "source": "user"})
        assert appended.seq == bound + 1
    finally:
        child.close()

    log = read_native_log(NEWER_ID, root=tmp_path)
    assert len(log.events) == bound + 1
    assert log.events[-1].type == EventKind.SESSION_TITLE.value
    listed = {info.id: info for info in list_native_sessions(tmp_path)}
    assert listed[NEWER_ID].parent_id == OLDER_ID


def test_prefix_fork_bounds_are_zero_full_and_beyond_the_tail(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=OLDER_ID)
    source = read_native_log(OLDER_ID, root=tmp_path)
    last_seq = source.events[-1].seq

    empty = _fork(OLDER_ID, root=tmp_path, new_id=NEWER_ID, created_at=CREATED_AT + 1_000, through_seq=0)
    try:
        assert empty.events == ()
        assert forked_event_count(empty) == 0
    finally:
        empty.close()

    beyond_id = "session-00000000-0000-0000-0000-000000000003"
    beyond = _fork(
        OLDER_ID,
        root=tmp_path,
        new_id=beyond_id,
        created_at=CREATED_AT + 2_000,
        through_seq=last_seq + 100,
    )
    try:
        assert len(beyond.events) == len(source.events)
        assert forked_event_count(beyond) == len(source.events)
    finally:
        beyond.close()

    full_id = "session-00000000-0000-0000-0000-000000000004"
    full = _fork(OLDER_ID, root=tmp_path, new_id=full_id, created_at=CREATED_AT + 3_000)
    try:
        assert len(full.events) == len(source.events)
        assert forked_event_count(full) == len(source.events)
    finally:
        full.close()


def test_prefix_fork_rejects_invalid_bounds(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=OLDER_ID)

    for invalid_bound in (-1, True, 1.5, "3"):
        with pytest.raises(NativeForkError) as invalid:
            _fork(OLDER_ID, root=tmp_path, new_id=NEWER_ID, through_seq=invalid_bound)  # type: ignore[arg-type]
        assert invalid.value.code == NATIVE_FORK_INVALID
        assert not Path(tmp_path / f"{NEWER_ID}.jsonl").exists()
        assert not Path(tmp_path / f".{NEWER_ID}.jsonl.tmp").exists()

    assert {info.id for info in list_native_sessions(tmp_path)} == {OLDER_ID}
