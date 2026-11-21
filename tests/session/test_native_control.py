"""Tests for the native session control service."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.core.ids import new_session_id
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.native_control import NativeSessionControl, NewSession
from deeppyro.session.native_fork import NATIVE_FORK_NOT_FOUND, NativeForkError
from deeppyro.session.native_query import NATIVE_QUERY_NOT_FOUND, NativeQueryError
from tests.session.native_helpers import CREATED_AT, SESSION_ID, write_native

SESSION_ID_2 = "session-00000000-0000-0000-0000-000000000001"
SESSION_ID_3 = "session-00000000-0000-0000-0000-000000000002"


def _control(tmp_path: Path) -> NativeSessionControl:
    return NativeSessionControl(root=tmp_path)


def test_create_and_open_roundtrip(tmp_path: Path) -> None:
    control = _control(tmp_path)
    session = control.create(NewSession(cwd=tmp_path))
    try:
        assert session.header.id.startswith("session-")
        assert session.path.exists()
        assert control.exists(session.header.id) is True
        with pytest.raises(SessionLockedError):
            control.open(session.header.id)
        log = control.read(session.header.id)
        assert log.header.id == session.header.id
        assert control.look(session.header.id).id == session.header.id
    finally:
        session.close()
    reopened = control.open(session.header.id)
    try:
        assert reopened.events == ()
        assert reopened.closed is False
    finally:
        reopened.close()


def test_create_honours_explicit_fields(tmp_path: Path) -> None:
    control = _control(tmp_path)
    session = control.create(
        NewSession(
            cwd=tmp_path,
            preset="standard",
            origin="subagent",
            delegation_depth=2,
            session_id=SESSION_ID,
            created_at=CREATED_AT,
        )
    )
    try:
        assert session.header.id == SESSION_ID
        assert session.header.preset == "standard"
        assert session.header.origin == "subagent"
        assert session.header.delegation_depth == 2
        assert session.header.created_at == CREATED_AT
        assert Path(session.header.cwd) == tmp_path.resolve()
    finally:
        session.close()

    with pytest.raises(FileExistsError):
        control.create(NewSession(cwd=tmp_path, session_id=SESSION_ID))


def test_exists_read_and_transcript_report_unknown_ids(tmp_path: Path) -> None:
    control = _control(tmp_path)
    missing = new_session_id()
    assert control.exists(missing) is False
    with pytest.raises(NativeQueryError) as read_error:
        control.read(missing)
    assert read_error.value.code == NATIVE_QUERY_NOT_FOUND
    with pytest.raises(NativeQueryError) as transcript_error:
        control.transcript(missing)
    assert transcript_error.value.code == NATIVE_QUERY_NOT_FOUND
    with pytest.raises(NativeQueryError) as look_error:
        control.look(missing)
    assert look_error.value.code == NATIVE_QUERY_NOT_FOUND
    assert control.latest() is None
    assert control.list() == []


def test_list_is_newest_first_and_filters_archived(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=SESSION_ID_2, created_at=CREATED_AT)
    write_native(tmp_path, session_id=SESSION_ID_3, created_at=CREATED_AT + 10)
    write_native(tmp_path, session_id=SESSION_ID, created_at=CREATED_AT + 20, archived=True)
    control = _control(tmp_path)

    visible = control.list()
    assert [info.id for info in visible] == [SESSION_ID_3, SESSION_ID_2]
    everything = control.list(include_archived=True)
    assert [info.id for info in everything] == [SESSION_ID, SESSION_ID_3, SESSION_ID_2]
    assert control.latest().id == SESSION_ID_3
    assert control.latest(include_archived=True).id == SESSION_ID
    assert [info.id for info in control.list(limit=1)] == [SESSION_ID_3]
    assert [info.id for info in control.list(limit=1, offset=1)] == [SESSION_ID_2]
    with pytest.raises(ValueError):
        control.list(limit=-1)
    with pytest.raises(ValueError):
        control.list(offset=-1)


def test_transcript_reads_a_written_session(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=SESSION_ID)
    transcript = _control(tmp_path).transcript(SESSION_ID)
    assert transcript.readable is True
    assert transcript.header.id == SESSION_ID
    assert transcript.entries
    assert transcript.final_text


def test_rename_archive_and_unarchive(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=SESSION_ID, title=None)
    control = _control(tmp_path)

    assert control.rename(SESSION_ID, "  Fix   the parser  ") == "Fix the parser"
    assert control.look(SESSION_ID).title == "Fix the parser"
    assert control.archive(SESSION_ID) is True
    assert control.look(SESSION_ID).archived is True
    assert control.list() == []
    assert [info.id for info in control.list(include_archived=True)] == [SESSION_ID]
    assert control.archive(SESSION_ID, archived=False) is False
    assert control.list()[0].id == SESSION_ID


def test_fork_copies_the_session_and_locks_the_child(tmp_path: Path) -> None:
    write_native(tmp_path, session_id=SESSION_ID)
    control = _control(tmp_path)
    source = control.read(SESSION_ID)
    fork = control.fork(SESSION_ID)
    try:
        assert fork.header.id != SESSION_ID
        assert fork.header.parent_id == SESSION_ID
        assert fork.header.origin == "fork"
        assert fork.header.delegation_depth == source.header.delegation_depth + 1
        assert len(fork.events) == len(source.events)
    finally:
        fork.close()

    explicit = control.fork(SESSION_ID, new_id=SESSION_ID_2)
    try:
        assert explicit.header.id == SESSION_ID_2
        assert explicit.header.parent_id == SESSION_ID
    finally:
        explicit.close()

    with pytest.raises(NativeForkError) as error:
        control.fork(new_session_id())
    assert error.value.code == NATIVE_FORK_NOT_FOUND


def test_delete_removes_the_log_and_its_lock(tmp_path: Path) -> None:
    control = _control(tmp_path)
    session = control.create(NewSession(cwd=tmp_path, session_id=SESSION_ID))
    session.close()

    info = control.delete(SESSION_ID)

    assert info.id == SESSION_ID
    assert not info.path.exists()
    assert control.exists(SESSION_ID) is False
    with pytest.raises(NativeQueryError) as error:
        control.delete(SESSION_ID)
    assert error.value.code == NATIVE_QUERY_NOT_FOUND


def test_delete_refuses_while_another_writer_holds_the_session(tmp_path: Path) -> None:
    control = _control(tmp_path)
    session = control.create(NewSession(cwd=tmp_path, session_id=SESSION_ID))
    try:
        with pytest.raises(SessionLockedError):
            control.delete(SESSION_ID)
        assert session.path.exists()
    finally:
        session.close()

    control.delete(SESSION_ID)
    assert session.path.exists() is False


def test_new_session_validates_its_fields(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        NewSession(cwd=tmp_path, origin="alien")
    with pytest.raises(ValueError):
        NewSession(cwd=tmp_path, session_id="not-a-session")
    with pytest.raises(ValueError):
        NewSession(cwd=tmp_path, delegation_depth=-1)
    with pytest.raises(ValueError):
        NewSession(cwd=tmp_path, created_at=-5)
