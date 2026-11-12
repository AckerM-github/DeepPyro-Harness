"""Fork semantics: copied prefix, inherited end-seed, and synthetic closers."""

from __future__ import annotations

import json
import pathlib
import shutil

import pytest

from deeppyro.core.errors import SessionError
from deeppyro.session import SessionStore, build_fork_seed  # noqa: F401  (re-export check below)
from deeppyro.session.fork import (
    CLOSER_TEXT,
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    SessionForkError,
    fork_session,
    open_turn_closers,
)
from deeppyro.session.fork import (
    build_fork_seed as _build_fork_seed,
)
from tests.session import support


def _store(tmp_path: pathlib.Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


def _source_with_pending_call(store: SessionStore, session_id: str = "session-source"):
    session = store.create(pathlib.Path("C:/work"), agent_preset="ptc", session_id=session_id)
    session.append("turn/start", {"turn": 1})
    session.append("step/start", {"turn": 1, "step": 1})
    session.append("system/message", support.system_message())
    session.append("user/message", support.user_message())
    session.append(
        "assistant/message",
        support.assistant_message(tool_calls=[{"id": "call_1", "name": "read", "arguments": "{}"}]),
    )
    session.append("tool/call", support.tool_call())
    return session


def test_fork_copies_prefix_and_closes_open_turn(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    source = _source_with_pending_call(store)
    child = fork_session(store, source.session_id)

    assert child.session_id != source.session_id
    assert child.header.parentSession == source.session_id
    assert child.header.isSeeded is True
    assert child.header.delegationDepth == source.header.delegationDepth + 1
    assert child.header.cwd == source.header.cwd
    assert child.header.agentPreset == "ptc"
    assert [record.seq for record in child.records] == list(range(10))
    prefix_rows = [json.loads(line) for line in child.path.read_text(encoding="utf-8").splitlines()[1:]]
    source_rows = [json.loads(line) for line in source.path.read_text(encoding="utf-8").splitlines()[1:]]
    assert prefix_rows[:6] == source_rows
    assert prefix_rows[6] == {
        "type": "session/end-seed",
        "seq": 6,
        "time": source.records[-1].time,
        "data": {"inherited": True},
    }
    assert prefix_rows[7]["type"] == "tool/result"
    assert prefix_rows[7]["data"]["error"] == {
        "name": "ToolOutcomeUnknownError",
        "code": TOOL_OUTCOME_UNKNOWN,
    }
    assert prefix_rows[7]["data"]["message"]["id"] == "forked-tool-result-call_1-7"
    assert prefix_rows[7]["data"]["message"]["isError"] is True
    assert prefix_rows[7]["sourceEventSeqs"] == [5]
    assert prefix_rows[7]["data"]["message"]["content"][0]["text"] == CLOSER_TEXT["forked"]["started"]
    assert prefix_rows[8]["type"] == "step/end"
    assert prefix_rows[8]["data"] == {"turn": 1, "step": 1}
    assert prefix_rows[9]["type"] == "turn/end"
    assert prefix_rows[9]["data"] == {"turn": 1, "reason": {"kind": "forked"}}

    derived = child.derive_messages()
    assert [message.role for message in derived] == ["system", "user", "assistant", "tool"]
    assert derived[-1].isError is True
    assert derived[-1].toolCallId == "call_1"

    assert len(source.records) == 6
    assert source.path.read_bytes() == source.path.read_bytes()
    assert child.append("user/message", support.user_message("msg_next", "continue")) == 10
    assert len(store.open(child.session_id).records) == 11


def test_fork_at_earlier_boundary(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    source = _source_with_pending_call(store)
    child = fork_session(store, source.session_id, at_seq=2)
    assert [record.type for record in child.records] == [
        "turn/start",
        "step/start",
        "system/message",
        "session/end-seed",
        "step/end",
        "turn/end",
    ]
    assert [record.seq for record in child.records] == list(range(6))
    assert child.records[3].data == {"inherited": True}
    assert child.records[4].data == {"turn": 1, "step": 1}
    assert child.records[5].data == {"turn": 1, "reason": {"kind": "forked"}}
    assert [message.role for message in child.derive_messages()] == ["system"]


def test_fork_of_closed_turn_only_marks_seed(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    source = store.create(pathlib.Path("C:/work"), session_id="session-closed")
    source.append("turn/start", {"turn": 1})
    source.append("step/start", {"turn": 1, "step": 1})
    source.append("user/message", support.user_message())
    source.append("step/end", {"turn": 1, "step": 1})
    source.append("turn/end", {"turn": 1})
    child = fork_session(store, source.session_id)
    assert [record.type for record in child.records] == [
        "turn/start",
        "step/start",
        "user/message",
        "step/end",
        "turn/end",
        "session/end-seed",
    ]
    assert child.records[-1].seq == 5
    assert child.records[-1].data == {"inherited": True}


def test_fork_empty_session(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    source = store.create(pathlib.Path("C:/work"), session_id="session-empty")
    child = fork_session(store, source.session_id)
    assert child.records == []
    assert child.header.isSeeded is True
    assert child.header.parentSession == source.session_id
    assert child.header.delegationDepth == 1
    assert child.path.is_file()
    reopened = store.open(child.session_id)
    assert reopened.records == []
    assert reopened.header.isSeeded is True


def test_fork_pending_call_that_never_started(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    source = store.create(pathlib.Path("C:/work"), session_id="session-not-started")
    source.append("turn/start", {"turn": 1})
    source.append(
        "assistant/message",
        support.assistant_message(tool_calls=[{"id": "call_x", "name": "read", "arguments": "{}"}]),
    )
    child = fork_session(store, source.session_id)
    result = next(record for record in child.records if record.type == "tool/result")
    assert result.data["error"] == {"name": "ToolNotStartedError", "code": TOOL_NOT_STARTED}
    assert "has no record of this tool call starting" in result.data["message"]["content"][0]["text"]
    assert result.sourceEventSeqs is None
    assert result.data["message"]["id"] == "forked-tool-result-call_x-3"


def test_fork_errors(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    source = _source_with_pending_call(store)
    with pytest.raises(SessionForkError) as excinfo:
        fork_session(store, "session-missing")
    assert excinfo.value.code == "SESSION_NOT_FOUND"

    for bound in (-1, 99, "x", True):
        with pytest.raises(SessionForkError) as excinfo:
            fork_session(store, source.session_id, at_seq=bound)  # type: ignore[arg-type]
        assert excinfo.value.code == "INVALID_BOUNDARY"

    empty = store.create(pathlib.Path("C:/work"), session_id="session-empty-bound")
    with pytest.raises(SessionForkError) as excinfo:
        fork_session(store, empty.session_id, at_seq=0)
    assert excinfo.value.code == "INVALID_BOUNDARY"


@support.requires_fixtures
def test_fork_of_trimmed_session_is_rejected(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    fixture = support.SESSIONS_DIR / "session-66de8981-4091-4bdc-9ff1-b23470ad6fa9.jsonl"
    store.root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, store.root / fixture.name)
    source = store.open(fixture.stem)
    assert source.trimmed is True
    with pytest.raises(SessionForkError) as excinfo:
        fork_session(store, fixture.stem, at_seq=0)
    assert excinfo.value.code == "INVALID_BOUNDARY"
    assert "trimmed" in str(excinfo.value)


@support.requires_fixtures
def test_fork_preserves_subagent_origin_from_real_fixture(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    fixture = support.SESSIONS_DIR / "7fdb12e1-c6c4-4059-8a53-e7d9d566de7c.jsonl"
    store.root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, store.root / fixture.name)
    source = store.open(fixture.stem)
    assert source.header.origin == "subagent"
    child = fork_session(store, fixture.stem)
    assert child.header.origin == "subagent"
    assert child.header.delegationDepth == source.header.delegationDepth + 1
    assert child.header.parentSession == fixture.stem
    assert [record.seq for record in child.records] == list(range(len(child.records)))
    assert child.records[len(source.records)].type == "session/end-seed"
    assert child.derive_messages() == source.derive_messages()


def test_build_fork_seed_shape() -> None:
    events = [
        support.record(support.event("turn/start", 0, {"turn": 1})),
        support.record(support.event("step/start", 1, {"turn": 1, "step": 1})),
        support.record(support.surface("user/message", 2, support.user_message())),
    ]
    seed = _build_fork_seed(events, 2)
    assert [event["seq"] for event in seed] == [0, 1, 2, 3, 4, 5]
    assert seed[3] == {
        "type": "session/end-seed",
        "seq": 3,
        "time": events[2].time,
        "data": {"inherited": True},
    }
    assert seed[4]["type"] == "step/end"
    assert seed[5]["data"] == {"turn": 1, "reason": {"kind": "forked"}}


def test_open_turn_closers_interrupted_kind() -> None:
    events = [
        support.record(support.event("turn/start", 0, {"turn": 2})),
        support.record(support.event("step/start", 1, {"turn": 2, "step": 3})),
        support.record(
            support.surface(
                "assistant/message",
                2,
                support.assistant_message(
                    tool_calls=[{"id": "call_9", "name": "read", "arguments": "{}"}], turn=2, step=3
                ),
            )
        ),
        support.record(support.event("tool/call", 3, support.tool_call("call_9", turn=2, step=3))),
    ]
    closers = open_turn_closers(events, {"kind": "interrupted"})
    assert [closer["type"] for closer in closers] == ["tool/result", "step/end", "turn/end"]
    assert [closer["seq"] for closer in closers] == [4, 5, 6]
    result = closers[0]
    assert result["data"]["message"]["content"][0]["text"] == CLOSER_TEXT["interrupted"]["started"]
    assert result["data"]["error"]["code"] == TOOL_OUTCOME_UNKNOWN
    assert result["sourceEventSeqs"] == [3]
    assert closers[2]["data"] == {"turn": 2, "reason": {"kind": "interrupted"}}


def test_open_turn_closers_none_when_turn_closed() -> None:
    events = [
        support.record(support.event("turn/start", 0, {"turn": 1})),
        support.record(support.event("turn/end", 1, {"turn": 1})),
    ]
    assert open_turn_closers(events, {"kind": "forked"}) == []
    assert open_turn_closers([], {"kind": "forked"}) == []


def test_synthetic_results_satisfy_the_writer_contract(tmp_path: pathlib.Path) -> None:
    store = _store(tmp_path)
    source = _source_with_pending_call(store)
    child = fork_session(store, source.session_id)
    reopened = store.open(child.session_id)
    result = next(record for record in reopened.records if record.type == "tool/result" and record.seq == 7)
    assert result.data["message"]["source"] == {"kind": "tool", "callId": "call_1"}
    assert result.data["message"]["toolCallId"] == "call_1"
    assert result.data["message"]["isError"] is True
    assert result.surfaceOp == "append"


def test_session_fork_error_is_session_error() -> None:
    assert issubclass(SessionForkError, SessionError)
