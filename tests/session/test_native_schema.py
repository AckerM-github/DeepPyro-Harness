"""Schema 1 codec, reader, writer, and strict rejection tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.core.ids import new_session_id
from deeppyro.core.messages import (
    CompactionSource,
    Message,
    ModelSource,
    SystemSource,
    TextPart,
    ToolSource,
    UserSource,
)
from deeppyro.session.errors import (
    CorruptLineError,
    EventFieldError,
    HeaderError,
    LineTooLargeError,
    MessageError,
    OrderingError,
    PluginNamespaceError,
    SequenceError,
    SessionLockedError,
    SessionV4Error,
    TimestampError,
    UnknownEventError,
    UnsupportedSchemaError,
)
from deeppyro.session.schema import (
    Event,
    EventKind,
    NativeCodec,
    NativeSession,
    SessionHeader,
    validate_event_data,
)

CALL_ID = "call_0123456789abcdef"
RUN_ID = "run_0123456789abcdef"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TURN_ID = "turn_0123456789abcdea"
STEP_ID = "step_0123456789abcdef"
MSG_USER = "msg_0000000000000011"
MSG_ASSISTANT = "msg_0000000000000012"
MSG_TOOL = "msg_0000000000000013"


def header(session_id: str, cwd: Path | str) -> SessionHeader:
    return SessionHeader(
        id=session_id,
        created_at=1_700_000_000_000,
        cwd=str(cwd),
        preset="standard",
        parent_id=None,
        origin="user",
        delegation_depth=0,
    )


def message(message_id: str, role: str, text: str = "hello", **kwargs: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "id": message_id,
        "role": role,
        "content": [{"type": "text", "text": text}],
        "created_at": 1_700_000_000_001,
        "source": {"kind": "user"} if role == "user" else {"kind": "system"},
    }
    fields.update(kwargs)
    return fields


def valid_data() -> dict[str, dict[str, object]]:
    assistant = Message(
        id=MSG_ASSISTANT,
        role="assistant",
        content=(TextPart("calling"),),
        created_at=1_700_000_000_001,
        source=ModelSource("deepseek", "deepseek-flash"),
    )
    return {
        "turn.started": {},
        "turn.ended": {"reason": {"kind": "completed"}},
        "step.started": {},
        "step.ended": {},
        "user.message": {"message": Message(MSG_USER, "user", (TextPart("hi"),), 1, UserSource()).to_dict()},
        "system.message": {
            "message": Message(
                "msg_0000000000000021", "system", (TextPart("prompt"),), 1, SystemSource()
            ).to_dict()
        },
        "developer.message": {"message": message("msg_0000000000000022", "developer")},
        "assistant.message": {"message": assistant.to_dict(), "finish_reason": "stop"},
        "tool.call": {"call_id": CALL_ID, "name": "read", "arguments": "{}"},
        "tool.result": {
            "call_id": CALL_ID,
            "message": Message(
                MSG_TOOL,
                "tool",
                (TextPart("ok"),),
                1,
                ToolSource(CALL_ID),
                tool_call_id=CALL_ID,
            ).to_dict(),
        },
        "approval.requested": {"approval_id": "a1", "tool_call_id": CALL_ID, "tool_name": "write"},
        "approval.decided": {"approval_id": "a1", "outcome": "allowed"},
        "message.replaced": {
            "message_id": MSG_ASSISTANT,
            "message": Message(
                MSG_ASSISTANT, "assistant", (TextPart("again"),), 1, ModelSource("x", "y")
            ).to_dict(),
        },
        "compaction.done": {
            "compaction_id": "cmp_0123456789abcdef",
            "covers": [MSG_USER, MSG_ASSISTANT],
            "message": Message(
                "msg_0000000000000031",
                "user",
                (TextPart("summary"),),
                1,
                CompactionSource("cmp_0123456789abcdef"),
            ).to_dict(),
        },
        "model.selected": {"provider": "deepseek", "model": "deepseek-flash"},
        "preset.selected": {"preset": "standard", "mode": "default"},
        "permission.policy": {"policy": "ask"},
        "sandbox.mode": {"mode": "workspace-write"},
        "workspace.cwd_changed": {"cwd": str(Path.cwd())},
        "session.title": {"title": "Demo", "source": "user"},
        "goal.changed": {
            "goal": {
                "id": "goal-1",
                "revision": 1,
                "objective": "ship",
                "phase": "active",
                "max_goal_rounds": 256,
            },
            "rounds_started": 0,
        },
        "plan.mode": {"mode": "plan"},
        "todo.written": {"todos": [{"content": "a", "status": "pending"}]},
        "deliverable.presented": {"files": [{"path": "out.txt", "description": "result"}]},
        "run.started": {},
        "run.checkpoint": {
            "last_seq": 7,
            "last_message_id": MSG_ASSISTANT,
            "turn_id": TURN_ID,
            "step_id": STEP_ID,
            "status": "running",
            "budgets": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_usd": 0.001,
                "tool_calls": 1,
                "wall_ms": 120,
            },
            "tool_calls": [
                {
                    "call_id": CALL_ID,
                    "name": "read",
                    "state": "completed",
                    "idempotency_key": "idem:" + "a" * 64,
                }
            ],
            "approvals": [],
        },
        "run.finished": {"status": "completed", "reason": {"kind": "completed"}},
    }


@pytest.mark.parametrize("kind", list(EventKind))
def test_every_event_kind_accepts_its_documented_shape(kind: EventKind) -> None:
    validate_event_data(kind.value, valid_data()[kind.value], seq=1)


def test_event_kind_catalog_is_exactly_27() -> None:
    assert len(EventKind) == 27
    assert len({kind.value for kind in EventKind}) == 27


def test_header_round_trip_and_filename_check(tmp_path: Path) -> None:
    session_id = new_session_id()
    raw = header(session_id, tmp_path).to_dict()
    assert SessionHeader.from_dict(raw, expected_id=session_id) == header(session_id, tmp_path)
    with pytest.raises(HeaderError, match="does not match file name"):
        SessionHeader.from_dict(raw, expected_id="session-00000000-0000-0000-0000-000000000000")


def test_header_requires_absolute_cwd_and_known_origin(tmp_path: Path) -> None:
    raw = header(new_session_id(), tmp_path).to_dict()
    raw["cwd"] = "relative/path"
    with pytest.raises(HeaderError, match="cwd"):
        SessionHeader.from_dict(raw)
    raw = header(new_session_id(), tmp_path).to_dict()
    raw["origin"] = "robot"
    with pytest.raises(HeaderError, match="origin"):
        SessionHeader.from_dict(raw)


def test_header_rejects_unknown_and_missing_fields(tmp_path: Path) -> None:
    raw = header(new_session_id(), tmp_path).to_dict()
    raw["extra"] = 1
    with pytest.raises(HeaderError, match="unknown field 'extra'"):
        SessionHeader.from_dict(raw)
    raw = header(new_session_id(), tmp_path).to_dict()
    del raw["preset"]
    with pytest.raises(HeaderError, match="lacks required field 'preset'"):
        SessionHeader.from_dict(raw)


def test_writer_round_trip_and_compact_bytes(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    with NativeSession.create(path, cwd=tmp_path) as session:
        session.append(EventKind.TURN_STARTED, run_id=RUN_ID, turn_id=TURN_ID, trace_id=TRACE_ID)
        session.append(
            EventKind.STEP_STARTED, run_id=RUN_ID, turn_id=TURN_ID, step_id=STEP_ID, trace_id=TRACE_ID
        )
        session.append(
            EventKind.USER_MESSAGE,
            {"message": Message(MSG_USER, "user", (TextPart("hi"),), 1, UserSource()).to_dict()},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
        )
        session.append(
            EventKind.STEP_ENDED, run_id=RUN_ID, turn_id=TURN_ID, step_id=STEP_ID, trace_id=TRACE_ID
        )
        session.append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "completed"}},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
        )
    log = NativeCodec.read(path)
    assert log.header.id == session_id
    assert [event.seq for event in log.events] == [1, 2, 3, 4, 5]
    assert log.events[0].ts >= log.header.created_at
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert '"seq":1,"ts":' in text
    assert " " not in text.splitlines()[0].replace(" ", "", 0)
    assert not path.with_name(path.name + ".lock").exists()


def test_writer_allocates_monotonic_timestamps(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    with NativeSession.create(path, cwd=tmp_path, created_at=10) as session:
        first = session.append(EventKind.PLAN_MODE, {"mode": "plan"}, ts=10)
        second = session.append(EventKind.PLAN_MODE, {"mode": "act"})
        with pytest.raises(TimestampError):
            session.append(EventKind.PLAN_MODE, {"mode": "late"}, ts=9)
    assert first.ts == 10
    assert second.ts >= 10


def test_second_writer_is_rejected(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    with (
        NativeSession.create(path, cwd=tmp_path),
        pytest.raises(SessionLockedError, match="locked by pid"),
    ):
        NativeSession.open(path)


def test_append_validation_does_not_write_partial_line(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    with NativeSession.create(path, cwd=tmp_path) as session:
        before = path.read_bytes()
        with pytest.raises(EventFieldError):
            session.append(EventKind.ASSISTANT_MESSAGE, {"message": {}})
        assert path.read_bytes() == before
        assert session.last_seq == 0


def test_read_rejects_v4_json_and_zstd(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session", "version": 4, "id": session_id}) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(SessionV4Error, match="v4 is not supported"):
        NativeCodec.read(path)
    path.write_bytes(b"\x28\xb5\x2f\xfdrest")
    with pytest.raises(SessionV4Error):
        NativeCodec.read(path)


def test_read_rejects_unsupported_schema(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    raw = header(session_id, tmp_path).to_dict()
    raw["schema"] = 2
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(UnsupportedSchemaError, match="schema 2"):
        NativeCodec.read(path)


def test_read_rejects_blank_bom_and_unterminated_last_line(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text("\n", encoding="utf-8", newline="\n")
    with pytest.raises(CorruptLineError, match="blank"):
        NativeCodec.read(path)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(header(session_id, tmp_path).to_dict()).encode() + b"\n")
    with pytest.raises(CorruptLineError, match="BOM"):
        NativeCodec.read(path)
    path.write_text(json.dumps(header(session_id, tmp_path).to_dict()), encoding="utf-8", newline="\n")
    with pytest.raises(CorruptLineError, match="incomplete last line"):
        NativeCodec.read(path)


def test_read_rejects_duplicate_header(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    line = json.dumps(header(session_id, tmp_path).to_dict()) + "\n"
    path.write_text(line + line, encoding="utf-8", newline="\n")
    with pytest.raises(HeaderError, match="more than one header"):
        NativeCodec.read(path)


def test_read_rejects_sequence_gap_and_timestamp_regression(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    raw = header(session_id, tmp_path)
    event = {"type": "plan.mode", "seq": 2, "ts": raw.created_at, "data": {"mode": "plan"}}
    path.write_text(
        NativeCodec.encode_header(raw) + "\n" + json.dumps(event) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(SequenceError, match="expected 1"):
        NativeCodec.read(path)
    event["seq"] = 1
    event["ts"] = raw.created_at - 1
    path.write_text(
        NativeCodec.encode_header(raw) + "\n" + json.dumps(event) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(TimestampError, match="before the previous ts"):
        NativeCodec.read(path)


def test_read_rejects_unknown_event_and_bad_plugin_namespace(tmp_path: Path) -> None:
    session_id = new_session_id()
    raw = header(session_id, tmp_path)
    for event_type, error in (
        ("mystery.event", UnknownEventError),
        ("plugin.Bad.event", PluginNamespaceError),
        ("plugin.solo", PluginNamespaceError),
    ):
        path = tmp_path / f"{session_id}.jsonl"
        event = {"type": event_type, "seq": 1, "ts": raw.created_at, "data": {}}
        path.write_text(
            NativeCodec.encode_header(raw) + "\n" + json.dumps(event) + "\n", encoding="utf-8", newline="\n"
        )
        with pytest.raises(error):
            NativeCodec.read(path)


def test_valid_plugin_event_is_persisted_with_opaque_data(tmp_path: Path) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    with NativeSession.create(path, cwd=tmp_path) as session:
        event = session.append("plugin.demo.note", {"anything": [1, 2]})
    assert event.type == "plugin.demo.note"
    assert NativeCodec.read(path).events[0].data == {"anything": [1, 2]}


def test_read_rejects_null_association_and_missing_step(tmp_path: Path) -> None:
    session_id = new_session_id()
    raw = header(session_id, tmp_path)
    path = tmp_path / f"{session_id}.jsonl"
    event = {"type": "plan.mode", "seq": 1, "ts": raw.created_at, "data": {"mode": "plan"}, "run_id": None}
    path.write_text(
        NativeCodec.encode_header(raw) + "\n" + json.dumps(event) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(EventFieldError, match="null is omitted"):
        NativeCodec.read(path)
    event = {
        "type": "assistant.message",
        "seq": 1,
        "ts": raw.created_at,
        "data": valid_data()["assistant.message"],
    }
    path.write_text(
        NativeCodec.encode_header(raw) + "\n" + json.dumps(event) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(EventFieldError, match="requires field 'run_id'"):
        NativeCodec.read(path)


def test_read_rejects_message_role_mismatch(tmp_path: Path) -> None:
    session_id = new_session_id()
    raw = header(session_id, tmp_path)
    path = tmp_path / f"{session_id}.jsonl"
    data = {"message": message(MSG_USER, "assistant")}
    event = {"type": "user.message", "seq": 1, "ts": raw.created_at, "data": data}
    path.write_text(
        NativeCodec.encode_header(raw) + "\n" + json.dumps(event) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(MessageError, match="role 'assistant'"):
        NativeCodec.read(path)


def test_turn_and_step_order_is_enforced(tmp_path: Path) -> None:
    session_id = new_session_id()
    raw = header(session_id, tmp_path)
    path = tmp_path / f"{session_id}.jsonl"
    event = {
        "type": "step.started",
        "seq": 1,
        "ts": raw.created_at,
        "run_id": RUN_ID,
        "turn_id": TURN_ID,
        "step_id": STEP_ID,
        "trace_id": TRACE_ID,
        "data": {},
    }
    path.write_text(
        NativeCodec.encode_header(raw) + "\n" + json.dumps(event) + "\n", encoding="utf-8", newline="\n"
    )
    with pytest.raises(OrderingError, match="outside turn"):
        NativeCodec.read(path)


def test_line_guard_can_be_monkeypatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = new_session_id()
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        NativeCodec.encode_header(header(session_id, tmp_path)) + "\n" + "x" * 100 + "\n",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr("deeppyro.session.schema.MAX_LINE_BYTES", 50)
    with pytest.raises(LineTooLargeError, match="larger than 50 bytes"):
        NativeCodec.read(path)


def test_event_encoding_is_compact_and_ordered(tmp_path: Path) -> None:
    event = Event(type="plan.mode", seq=1, ts=2, data={"mode": "plan"})
    assert NativeCodec.encode_event(event) == '{"type":"plan.mode","seq":1,"ts":2,"data":{"mode":"plan"}}'
    event = Event(
        type="step.started",
        seq=2,
        ts=3,
        data={},
        run_id=RUN_ID,
        turn_id=TURN_ID,
        step_id=STEP_ID,
        trace_id=TRACE_ID,
    )
    assert NativeCodec.encode_event(event).startswith('{"type":"step.started","seq":2,"ts":3,"run_id"')
