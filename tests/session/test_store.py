"""Store, append, layout, and zstd persistence tests."""

from __future__ import annotations

import json
import pathlib

import pytest

from deeppyro.core.errors import SessionError
from deeppyro.session import SessionStore, layout, reader, writer
from deeppyro.session.store import SessionInfo
from tests.session import support


def _new_store(tmp_path: pathlib.Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


def _append_turn(session, *, turn: int = 1) -> None:
    session.append("turn/start", {"turn": turn})
    session.append("step/start", {"turn": turn, "step": 1})


def _conversation(session) -> None:
    _append_turn(session)
    session.append("system/message", support.system_message())
    session.append("user/message", support.user_message())
    session.append("assistant/message", support.assistant_message())
    session.append("step/end", {"turn": 1, "step": 1})
    session.append("turn/end", {"turn": 1})


def test_create_writes_header_only_log(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session = store.create(pathlib.Path("C:/work"), agent_preset="ptc", session_id="session-one")
    assert session.session_id == "session-one"
    assert session.path == layout.flat_session_path(store.root, "session-one")
    assert session.path.is_file()
    assert session.records == []
    assert session.last_seq == -1
    lines = session.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "type": "session",
        "version": 4,
        "id": "session-one",
        "createdAt": session.header.createdAt,
        "cwd": "C:\\work",
        "isSeeded": False,
        "delegationDepth": 0,
        "agentPreset": "ptc",
    }


def test_create_rejects_bad_arguments(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    with pytest.raises(SessionError, match="absolute path"):
        store.create(pathlib.Path("relative/dir"))
    with pytest.raises(SessionError, match="non-empty string"):
        store.create(pathlib.Path("C:/work"), session_id="")
    store.create(pathlib.Path("C:/work"), session_id="session-dup")
    with pytest.raises(SessionError, match="already exists"):
        store.create(pathlib.Path("C:/work"), session_id="session-dup")


def test_append_roundtrip_and_file_shape(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session = store.create(pathlib.Path("C:/work"), session_id="session-append")
    _conversation(session)
    assert [record.seq for record in session.records] == list(range(7))
    reopened = store.open("session-append")
    assert [record.type for record in reopened.records] == [
        "turn/start",
        "step/start",
        "system/message",
        "user/message",
        "assistant/message",
        "step/end",
        "turn/end",
    ]
    raw_lines = session.path.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 8
    rows = [json.loads(line) for line in raw_lines[1:]]
    assert [row["seq"] for row in rows] == list(range(7))
    assert all(
        set(row) <= {"type", "seq", "time", "data", "surfaceOp", "sourceEventSeqs", "ignorable"}
        for row in rows
    )
    assert rows[2]["surfaceOp"] == "append"
    derived = reopened.derive_messages()
    assert [message.role for message in derived] == ["system", "user", "assistant"]
    assert derived[1].text() == "hello"


def test_append_is_compact_utf8_single_line(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session = store.create(pathlib.Path("C:/work"), session_id="session-utf8")
    session.append("user/message", support.user_message(text="你好，世界"))
    raw = session.path.read_bytes()
    assert raw.endswith(b"\n")
    line = raw.splitlines()[-1]
    assert "你好，世界".encode() in line
    assert b": " not in line  # compact separators
    assert json.loads(line.decode("utf-8"))["data"]["content"][0]["text"] == "你好，世界"


def test_tool_result_append_keeps_meta_and_source_seqs(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session = store.create(pathlib.Path("C:/work"), session_id="session-tools")
    _append_turn(session)
    assistant = session.append(
        "assistant/message",
        support.assistant_message(tool_calls=[{"id": "call_1", "name": "read", "arguments": "{}"}]),
    )
    call_seq = session.append("tool/call", support.tool_call())
    result_seq = session.append(
        "tool/result",
        support.tool_result("call_1", "file body", meta={"cwd": "C:\\work"}),
        sourceEventSeqs=[call_seq],
    )
    rows = [json.loads(line) for line in session.path.read_text(encoding="utf-8").splitlines()[1:]]
    row = next(item for item in rows if item["seq"] == result_seq)
    assert row["data"]["meta"] == {"cwd": "C:\\work"}
    assert row["sourceEventSeqs"] == [call_seq]
    assert row["surfaceOp"] == "append"
    assert row["data"]["message"]["toolCallId"] == "call_1"
    assert assistant == call_seq - 1
    reopened = store.open("session-tools")
    assert reopened.records[-1].data["meta"]["cwd"] == "C:\\work"


def test_append_validation_errors_do_not_touch_file(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session = store.create(pathlib.Path("C:/work"), session_id="session-invalid")
    before = session.path.read_bytes()
    with pytest.raises(SessionError):
        session.append(
            "system/message",
            {
                "turn": 1,
                "step": 1,
                "message": {
                    "id": "m",
                    "role": "assistant",
                    "content": [],
                    "source": {"kind": "model", "provider": "p", "model": "m"},
                },
            },
        )
    assert session.path.read_bytes() == before
    assert session.records == []
    session.close()
    with pytest.raises(SessionError, match="closed"):
        session.append("turn/start", {"turn": 1})


def test_explicit_time_is_honoured(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session = store.create(pathlib.Path("C:/work"), session_id="session-time")
    session.append("turn/start", {"turn": 1}, time=1234)
    assert session.records[0].time == 1234
    assert store.open("session-time").records[0].time == 1234


def test_store_list_latest_and_session_info(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    older = store.create(pathlib.Path("C:/work"), agent_preset="ptc", session_id="session-older")
    _conversation(older)
    older.append("turn/start", {"turn": 2})
    older.append(
        "session/title", {"title": "older title", "messageSeqs": [3], "source": {"kind": "fallback"}}
    )
    newer = store.create(pathlib.Path("D:/other"), session_id="session-newer")
    newer.append("session/title", {"title": "", "messageSeqs": [], "source": {"kind": "fallback"}})
    newer.append("session/title", {"title": "newer title", "messageSeqs": [], "source": {"kind": "provider"}})

    infos = store.list()
    assert [info.id for info in infos] == ["session-newer", "session-older"]
    assert all(isinstance(info, SessionInfo) for info in infos)
    newest, old = infos
    assert newest.title == "newer title"
    assert newest.turns == 0
    assert newest.recordCount == 2
    assert newest.cwd == "D:\\other"
    assert newest.agentPreset is None
    assert old.title == "older title"
    assert old.turns == 2
    assert old.agentPreset == "ptc"
    assert old.createdAt <= newest.createdAt
    assert store.latest() is not None and store.latest().id == "session-newer"


def test_list_skips_corrupt_logs(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    store.create(pathlib.Path("C:/work"), session_id="session-good")
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "broken.jsonl").write_text("{not json}\n", encoding="utf-8")
    (store.root / "empty.jsonl").write_text("", encoding="utf-8")
    assert [info.id for info in store.list()] == ["session-good"]


def test_empty_store_latest_is_none(tmp_path: pathlib.Path) -> None:
    assert _new_store(tmp_path).latest() is None


def test_open_unknown_session_raises(tmp_path: pathlib.Path) -> None:
    with pytest.raises(SessionError, match="not found"):
        _new_store(tmp_path).open("session-missing")


def test_open_supports_non_session_id_prefix(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    store.create(pathlib.Path("C:/work"), session_id="7bed18f9-a703-4ce9-b76a-507f7c861258")
    assert (
        store.open("7bed18f9-a703-4ce9-b76a-507f7c861258").session_id
        == "7bed18f9-a703-4ce9-b76a-507f7c861258"
    )


def test_open_supports_dsh_nested_layout(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session_id = "session-nested"
    path = layout.session_file_path(store.root, session_id, "C:/work")
    writer.write_new_session(path, support.header(session_id), [support.event("turn/start", 0, {"turn": 1})])
    assert path.parts[-3].startswith("--C-work--")
    session = store.open(session_id)
    assert session.path == path
    assert [record.seq for record in session.records] == [0]
    assert [info.id for info in store.list()] == [session_id]
    session.append("turn/end", {"turn": 1})
    assert [record.type for record in store.open(session_id).records] == ["turn/start", "turn/end"]


def test_open_and_append_zstd_flat_log(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session_id = "session-zstd-flat"
    path = store.root / f"{session_id}.jsonl.zstd"
    store.root.mkdir(parents=True, exist_ok=True)
    writer.write_new_session(
        path, support.header(session_id), [support.event("turn/start", 0, {"turn": 1})], zstd=True
    )
    session = store.open(session_id)
    assert session.zstd is True
    session.append("turn/end", {"turn": 1})
    assert [record.type for record in reader.read_session_file(path).records] == ["turn/start", "turn/end"]


def test_store_with_default_root_uses_config(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deeppyro.session.store.session_dir", lambda: tmp_path / "default")
    assert SessionStore().root == tmp_path / "default"


def test_session_stats(tmp_path: pathlib.Path) -> None:
    store = _new_store(tmp_path)
    session = store.create(pathlib.Path("C:/work"), session_id="session-stats")
    _conversation(session)
    session.append("user/message", support.user_message("msg_u2", "again"))
    assert session.usage() == {"input": 10, "output": 5, "cached": 2}
    assert session.turn_count() == 1
    assert session.last_step() == (1, 1)
    empty = store.create(pathlib.Path("C:/work"), session_id="session-empty")
    assert empty.usage() == {"input": 0, "output": 0, "cached": 0}
    assert empty.turn_count() == 0
    assert empty.last_step() == (0, 0)


def test_layout_encoding() -> None:
    assert layout.encode_segment("session-abc") == "session-abc"
    assert layout.encode_segment(".") == "~002E"
    assert layout.encode_segment("..") == "~002E~002E"
    assert layout.encode_segment("a/b") == "a~002Fb"
    assert layout.encode_segment("a~b") == "a~007Eb"
    assert layout.project_key("C:\\Users\\example\\.dsh\\im") == "--C-Users-example-.dsh-im--"
    assert layout.project_key("/home/user/project") == "--home-user-project--"
    assert layout.log_filename() == "session.v4.jsonl"
    assert layout.log_filename(zstd=True) == "session.v4.jsonl.zstd"
    assert layout.is_log_file(pathlib.Path("a.jsonl.zstd")) is True
    assert layout.is_log_file(pathlib.Path("a.txt")) is False
