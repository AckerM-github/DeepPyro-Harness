from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deeppyro.core.messages import Message, ModelSource, TextPart, ToolCallPart, ToolSource
from deeppyro.observability.trace import (
    ModelCallRecord,
    RunRecord,
    ToolCallRecord,
    TraceFormatError,
    TraceLog,
    TraceStore,
    args_hash,
    canonical_json,
    hash_payload,
    join_trace,
    prompt_hash,
    read_trace,
    record_from_dict,
)
from deeppyro.session.fold import fold
from deeppyro.session.schema import Event, EventKind, NativeCodec, NativeSession

RUN = "run_" + "a" * 16
OTHER_RUN = "run_" + "b" * 16
TRACE = "c" * 32
SESSION = "session-11111111-2222-3333-4444-555555555555"
TURN = "turn_" + "1" * 16
STEP = "step_" + "2" * 16
SPAN = "span_" + "3" * 16
CALL = "call_" + "4" * 16


def run_record() -> RunRecord:
    return RunRecord(
        run_id=RUN,
        trace_id=TRACE,
        session_id=SESSION,
        started_at=1000,
        provider="deepseek",
        model="deepseek-flash",
    )


def model_record(**overrides: object) -> ModelCallRecord:
    values: dict[str, object] = {
        "trace_id": TRACE,
        "span_id": SPAN,
        "run_id": RUN,
        "turn_id": TURN,
        "step_id": STEP,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "prompt_hash": hash_payload({"messages": [{"role": "user", "content": "hi"}]}),
        "input_tokens": 12,
        "output_tokens": 3,
        "latency_ms": 40,
        "finish_reason": "tool_calls",
        "retry_count": 0,
        "ts": 1001,
    }
    values.update(overrides)
    return ModelCallRecord(**values)  # type: ignore[arg-type]


def tool_record(**overrides: object) -> ToolCallRecord:
    values: dict[str, object] = {
        "trace_id": TRACE,
        "span_id": "span_" + "5" * 16,
        "run_id": RUN,
        "turn_id": TURN,
        "step_id": STEP,
        "call_id": CALL,
        "tool": "read_file",
        "args_hash": args_hash('{"path":"a.txt"}'),
        "result_size": 42,
        "is_error": False,
        "retry_count": 0,
        "latency_ms": 7,
        "ts": 1002,
    }
    values.update(overrides)
    return ToolCallRecord(**values)  # type: ignore[arg-type]


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8", newline="")


def trace_lines() -> list[str]:
    return [
        json.dumps(run_record().to_dict(), ensure_ascii=False, separators=(",", ":")),
        json.dumps(model_record().to_dict(), ensure_ascii=False, separators=(",", ":")),
        json.dumps(tool_record().to_dict(), ensure_ascii=False, separators=(",", ":")),
    ]


def event(kind: str, seq: int, data: dict[str, object] | None = None) -> Event:
    return Event(
        type=kind,
        seq=seq,
        ts=1000 + seq,
        data={} if data is None else data,
        run_id=RUN,
        turn_id=TURN,
        step_id=STEP,
        trace_id=TRACE,
    )


def test_canonical_json_sorts_and_normalizes() -> None:
    assert canonical_json({"b": 1, "a": (2, 3)}) == '{"a":[2,3],"b":1}'
    assert canonical_json({"text": "你好"}) == '{"text":"你好"}'
    assert canonical_json({"nested": {"z": None, "a": True}}) == '{"nested":{"a":true,"z":null}}'


def test_canonical_json_rejects_non_json_values() -> None:
    with pytest.raises(TraceFormatError, match="non-finite"):
        canonical_json({"n": float("nan")})
    with pytest.raises(TraceFormatError, match="type"):
        canonical_json(b"raw")
    with pytest.raises(TraceFormatError, match="string object keys"):
        canonical_json({1: "x"})

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(TraceFormatError, match="cyclic"):
        canonical_json(cyclic)


def test_hashes_are_stable_and_order_independent() -> None:
    assert hash_payload({"a": 1, "b": 2}) == hash_payload({"b": 2, "a": 1})
    assert hash_payload({"a": 1}).startswith("sha256:")
    assert args_hash('{"a":1,"b":2}') == args_hash({"b": 2, "a": 1})
    assert prompt_hash({"messages": []}) == prompt_hash({"messages": []})
    assert hashlib.sha256(b'{"a":1}').hexdigest() in hash_payload({"a": 1})


def test_args_hash_rejects_invalid_json() -> None:
    with pytest.raises(TraceFormatError, match="not valid JSON"):
        args_hash("{nope")


def test_trace_store_round_trip(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    run = run_record()
    path = store.create_run(run)
    assert path.name == RUN + ".jsonl"
    store.append(model_record())
    store.append(tool_record())

    log = store.read(RUN)
    assert log.run == run
    assert log.records == (model_record(), tool_record())
    assert path.read_bytes().endswith(bytes([10]))
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_trace_store_refuses_duplicate_run(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    store.create_run(run_record())
    with pytest.raises(TraceFormatError, match="already exists"):
        store.create_run(run_record())


def test_trace_store_append_requires_run(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    with pytest.raises(TraceFormatError, match="does not exist"):
        store.append(model_record())


def test_read_trace_accepts_explicit_nulls(tmp_path: Path) -> None:
    data = model_record().to_dict()
    data["cached_input_tokens"] = None
    data["reasoning_tokens"] = None
    data["cost_usd"] = None
    data["context_message_ids"] = None
    data["error_code"] = None
    path = tmp_path / "trace.jsonl"
    write_lines(path, [json.dumps(run_record().to_dict()), json.dumps(data)])
    log = read_trace(path)
    assert len(log.records) == 1
    assert log.records[0].cached_input_tokens is None


def test_read_trace_rejects_first_line_without_run(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    write_lines(path, [json.dumps(model_record().to_dict())])
    with pytest.raises(TraceFormatError, match="first line must be a run record"):
        read_trace(path)


def test_read_trace_rejects_second_run(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    write_lines(path, [*trace_lines(), json.dumps(run_record().to_dict())])
    with pytest.raises(TraceFormatError, match="more than one run record"):
        read_trace(path)


def test_read_trace_rejects_foreign_run(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    foreign = model_record(run_id=OTHER_RUN).to_dict()
    write_lines(path, [json.dumps(run_record().to_dict()), json.dumps(foreign)])
    with pytest.raises(TraceFormatError, match="does not belong"):
        read_trace(path)


def test_read_trace_rejects_blank_line(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    write_lines(path, [json.dumps(run_record().to_dict()), ""])
    with pytest.raises(TraceFormatError, match="blank"):
        read_trace(path)


def test_read_trace_rejects_bom(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_bytes(bytes([0xEF, 0xBB, 0xBF]) + json.dumps(run_record().to_dict()).encode())
    with pytest.raises(TraceFormatError, match="BOM"):
        read_trace(path)


def test_read_trace_rejects_unknown_field(tmp_path: Path) -> None:
    data = model_record().to_dict()
    data["extra"] = 1
    path = tmp_path / "trace.jsonl"
    write_lines(path, [json.dumps(run_record().to_dict()), json.dumps(data)])
    with pytest.raises(TraceFormatError, match="unknown field"):
        read_trace(path)


def test_read_trace_rejects_missing_required_field(tmp_path: Path) -> None:
    data = model_record().to_dict()
    del data["output_tokens"]
    path = tmp_path / "trace.jsonl"
    write_lines(path, [json.dumps(run_record().to_dict()), json.dumps(data)])
    with pytest.raises(TraceFormatError, match="lacks required field"):
        read_trace(path)


def test_read_trace_rejects_bad_span_and_hash(tmp_path: Path) -> None:
    bad_span = model_record(span_id="span_bad").to_dict()
    path = tmp_path / "trace.jsonl"
    write_lines(path, [json.dumps(run_record().to_dict()), json.dumps(bad_span)])
    with pytest.raises(TraceFormatError, match="span_id"):
        read_trace(path)

    bad_hash = model_record(prompt_hash="sha256:bad").to_dict()
    write_lines(path, [json.dumps(run_record().to_dict()), json.dumps(bad_hash)])
    with pytest.raises(TraceFormatError, match="prompt_hash"):
        read_trace(path)


def test_read_trace_rejects_unknown_type(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    write_lines(path, [json.dumps({"type": "span"})])
    with pytest.raises(TraceFormatError, match="unknown trace record type"):
        read_trace(path)


def test_record_from_dict_targets() -> None:
    assert record_from_dict(run_record().to_dict()) == run_record()
    assert record_from_dict(model_record().to_dict()) == model_record()
    assert record_from_dict(tool_record().to_dict()) == tool_record()


def test_join_trace_links_model_and_tool_spans() -> None:
    events = [
        Event("turn.started", 1, 1001, {}, run_id=RUN, turn_id=TURN, trace_id=TRACE),
        Event("step.started", 2, 1002, {}, run_id=RUN, turn_id=TURN, step_id=STEP, trace_id=TRACE),
        Event(
            "assistant.message",
            3,
            1003,
            {"message": {}},
            run_id=RUN,
            turn_id=TURN,
            step_id=STEP,
            trace_id=TRACE,
        ),
        Event(
            "tool.call",
            4,
            1004,
            {"call_id": CALL},
            run_id=RUN,
            turn_id=TURN,
            step_id=STEP,
            trace_id=TRACE,
        ),
    ]
    log = TraceLog(Path("trace.jsonl"), run_record(), (model_record(), tool_record()))
    joined = join_trace(log, events)
    assert joined.run == run_record()
    assert joined.model_calls == (model_record(),)
    assert joined.tool_calls == (tool_record(),)


def test_join_trace_requires_matching_run_id() -> None:
    events = [
        Event("turn.started", 1, 1001, {}, run_id=OTHER_RUN, turn_id=TURN, trace_id=TRACE),
        Event("step.started", 2, 1002, {}, run_id=OTHER_RUN, turn_id=TURN, step_id=STEP, trace_id=TRACE),
        Event(
            "tool.call",
            3,
            1003,
            {"call_id": CALL},
            run_id=OTHER_RUN,
            turn_id=TURN,
            step_id=STEP,
            trace_id=TRACE,
        ),
    ]
    log = TraceLog(Path("trace.jsonl"), run_record(), (model_record(), tool_record()))
    with pytest.raises(TraceFormatError, match="no matching session step"):
        join_trace(log, events)


def test_join_trace_rejects_orphan_span() -> None:
    log = TraceLog(Path("trace.jsonl"), run_record(), (model_record(step_id="step_" + "9" * 16),))
    with pytest.raises(TraceFormatError, match="no matching session step"):
        join_trace(log, [])


def test_join_trace_rejects_tool_without_session_call() -> None:
    events = [Event("step.started", 1, 1001, {}, run_id=RUN, turn_id=TURN, step_id=STEP, trace_id=TRACE)]
    log = TraceLog(Path("trace.jsonl"), run_record(), (tool_record(),))
    with pytest.raises(TraceFormatError, match=r"no matching session tool\.call"):
        join_trace(log, events)


def test_model_record_rejects_bad_finish_reason() -> None:
    data = model_record().to_dict()
    data["finish_reason"] = "weird"
    with pytest.raises(TraceFormatError, match="not supported"):
        record_from_dict(data)


def test_tool_record_rejects_non_boolean_is_error() -> None:
    data = tool_record().to_dict()
    data["is_error"] = "false"
    with pytest.raises(TraceFormatError, match="is_error must be a boolean"):
        record_from_dict(data)


def test_trace_join_smoke_with_native_session(tmp_path: Path) -> None:
    session_path = tmp_path / (SESSION + ".jsonl")
    assistant = Message(
        id="msg_" + "6" * 16,
        role="assistant",
        content=(TextPart("reading"), ToolCallPart(CALL, "read_file", '{"path":"a.txt"}')),
        created_at=1003,
        source=ModelSource("deepseek", "deepseek-flash"),
    )
    tool_message = Message(
        id="msg_" + "7" * 16,
        role="tool",
        content=(TextPart("done"),),
        created_at=1005,
        source=ToolSource(CALL),
        tool_call_id=CALL,
    )
    with NativeSession.create(session_path, cwd=tmp_path, session_id=SESSION, created_at=1000) as session:
        session.append(EventKind.TURN_STARTED, {}, run_id=RUN, turn_id=TURN, trace_id=TRACE, ts=1001)
        session.append(
            EventKind.STEP_STARTED,
            {},
            run_id=RUN,
            turn_id=TURN,
            step_id=STEP,
            trace_id=TRACE,
            ts=1002,
        )
        session.append(
            EventKind.ASSISTANT_MESSAGE,
            {"message": assistant.to_dict(), "finish_reason": "tool_calls"},
            run_id=RUN,
            turn_id=TURN,
            step_id=STEP,
            trace_id=TRACE,
            ts=1003,
        )
        session.append(
            EventKind.TOOL_CALL,
            {"call_id": CALL, "name": "read_file", "arguments": '{"path":"a.txt"}'},
            run_id=RUN,
            turn_id=TURN,
            step_id=STEP,
            trace_id=TRACE,
            ts=1004,
        )
        session.append(
            EventKind.TOOL_RESULT,
            {"call_id": CALL, "message": tool_message.to_dict()},
            run_id=RUN,
            turn_id=TURN,
            step_id=STEP,
            trace_id=TRACE,
            ts=1005,
        )
        session.append(
            EventKind.STEP_ENDED,
            {},
            run_id=RUN,
            turn_id=TURN,
            step_id=STEP,
            trace_id=TRACE,
            ts=1006,
        )
        session.append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "completed"}},
            run_id=RUN,
            turn_id=TURN,
            trace_id=TRACE,
            ts=1007,
        )

    log = NativeCodec.read(session_path)
    view = fold(log.header, log.events)
    assert view.issues == ()
    assert [message.role for message in view.messages] == ["assistant", "tool"]

    store = TraceStore(tmp_path / "traces")
    store.create_run(run_record())
    store.append(model_record(ts=1003))
    store.append(tool_record(ts=1005))
    joined = join_trace(store.read(RUN), log.events)
    assert len(joined.model_calls) == 1
    assert len(joined.tool_calls) == 1
    assert joined.model_calls[0].finish_reason == "tool_calls"
    assert joined.tool_calls[0].call_id == CALL
