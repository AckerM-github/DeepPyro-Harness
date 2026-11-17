"""M2 run event contract tests (schema section 5.5)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from deeppyro.core.ids import new_session_id
from deeppyro.core.messages import Message, TextPart, UserSource
from deeppyro.session.errors import EventFieldError, OrderingError
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeCodec, NativeSession, validate_event_data

RUN_ID = "run_0123456789abcdef"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TURN_ID = "turn_0123456789abcdea"
TURN_ID_2 = "turn_0123456789abcdeb"
STEP_ID = "step_0123456789abcdef"
CALL_ID = "call_0123456789abcdef"
IDEM_KEY = "idem:" + "a" * 64


def budgets(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "input_tokens": 12,
        "output_tokens": 4,
        "cost_usd": 0.002,
        "tool_calls": 1,
        "wall_ms": 350,
    }
    data.update(overrides)
    return data


def checkpoint_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "last_seq": 9,
        "status": "running",
        "budgets": budgets(),
        "tool_calls": [
            {"call_id": CALL_ID, "name": "read_file", "state": "completed", "idempotency_key": IDEM_KEY}
        ],
        "approvals": [{"approval_id": "approval-1", "tool_call_id": CALL_ID, "state": "pending"}],
    }
    data.update(overrides)
    return data


def create_session(tmp_path: Path) -> NativeSession:
    session_id = new_session_id()
    return NativeSession.create(tmp_path / f"{session_id}.jsonl", cwd=tmp_path, session_id=session_id)


def append_turn(session: NativeSession, turn_id: str, *, step_id: str | None = None) -> None:
    session.append(EventKind.TURN_STARTED, run_id=RUN_ID, turn_id=turn_id, trace_id=TRACE_ID)
    if step_id is not None:
        session.append(
            EventKind.STEP_STARTED,
            run_id=RUN_ID,
            turn_id=turn_id,
            step_id=step_id,
            trace_id=TRACE_ID,
        )
        session.append(
            EventKind.STEP_ENDED,
            run_id=RUN_ID,
            turn_id=turn_id,
            step_id=step_id,
            trace_id=TRACE_ID,
        )
    session.append(
        EventKind.TURN_ENDED,
        {"reason": {"kind": "completed"}},
        run_id=RUN_ID,
        turn_id=turn_id,
        trace_id=TRACE_ID,
    )


def test_run_events_round_trip_without_turn_or_step(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    path = session.path
    try:
        started = session.append(EventKind.RUN_STARTED, run_id=RUN_ID, trace_id=TRACE_ID)
        append_turn(session, TURN_ID)
        checkpoint = session.append(
            EventKind.RUN_CHECKPOINT,
            checkpoint_data(last_message_id="msg_0000000000000011"),
            run_id=RUN_ID,
            trace_id=TRACE_ID,
        )
        finished = session.append(
            EventKind.RUN_FINISHED,
            {"status": "completed", "reason": {"kind": "completed"}},
            run_id=RUN_ID,
            trace_id=TRACE_ID,
        )
    finally:
        session.close()
    log = NativeCodec.read(path)
    assert [event.type for event in log.events] == [
        "run.started",
        "turn.started",
        "turn.ended",
        "run.checkpoint",
        "run.finished",
    ]
    assert started.turn_id is None and started.step_id is None
    assert checkpoint.data["budgets"] == budgets()
    assert checkpoint.data["tool_calls"][0]["idempotency_key"] == IDEM_KEY
    assert finished.data["status"] == "completed"
    assert log.events[-1].ts == finished.ts


def test_run_events_require_run_and_trace_ids(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    try:
        with pytest.raises(EventFieldError, match="requires field 'run_id'"):
            session.append(EventKind.RUN_STARTED)
        with pytest.raises(EventFieldError, match="requires field 'trace_id'"):
            session.append(EventKind.RUN_STARTED, run_id=RUN_ID)
        with pytest.raises(EventFieldError, match="requires field 'run_id'"):
            session.append(EventKind.RUN_FINISHED, {"status": "failed"})
    finally:
        session.close()


def test_run_events_forbid_turn_and_step_ids(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    try:
        with pytest.raises(EventFieldError, match="does not allow field 'turn_id'"):
            session.append(EventKind.RUN_STARTED, run_id=RUN_ID, turn_id=TURN_ID, trace_id=TRACE_ID)
        with pytest.raises(EventFieldError, match="does not allow field 'step_id'"):
            session.append(EventKind.RUN_STARTED, run_id=RUN_ID, step_id=STEP_ID, trace_id=TRACE_ID)
    finally:
        session.close()


def test_run_checkpoint_is_allowed_between_and_inside_turns(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    path = session.path
    try:
        session.append(EventKind.RUN_STARTED, run_id=RUN_ID, trace_id=TRACE_ID)
        session.append(EventKind.RUN_CHECKPOINT, checkpoint_data(), run_id=RUN_ID, trace_id=TRACE_ID)
        session.append(EventKind.TURN_STARTED, run_id=RUN_ID, turn_id=TURN_ID, trace_id=TRACE_ID)
        session.append(
            EventKind.STEP_STARTED,
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
        )
        session.append(EventKind.RUN_CHECKPOINT, checkpoint_data(), run_id=RUN_ID, trace_id=TRACE_ID)
        session.append(
            EventKind.STEP_ENDED,
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
        )
        session.append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "completed"}},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
        )
        session.append(EventKind.RUN_CHECKPOINT, checkpoint_data(), run_id=RUN_ID, trace_id=TRACE_ID)
    finally:
        session.close()
    assert [event.type for event in NativeCodec.read(path).events].count("run.checkpoint") == 3


def test_run_checkpoint_rejects_malformed_budgets_and_calls(tmp_path: Path) -> None:
    def data() -> dict[str, object]:
        return checkpoint_data()

    cases: list[tuple[Callable[[dict[str, object]], None], str]] = [
        (lambda value: value.pop("budgets"), "requires field 'budgets'"),
        (lambda value: value.update(extra=True), "unknown field 'extra'"),
        (lambda value: value.update(status="paused"), "invalid value 'paused' for field 'status'"),
        (lambda value: value.update(last_seq=-1), "invalid value -1 for field 'last_seq'"),
        (lambda value: value.update(tool_calls={}), "must be an array"),
        (lambda value: value.update(approvals=[]), None),
    ]
    for mutate, message in cases:
        value = data()
        mutate(value)
        if message is None:
            validate_event_data("run.checkpoint", value, seq=1)
            continue
        with pytest.raises(EventFieldError, match=message):
            validate_event_data("run.checkpoint", value, seq=1)

    bad_budget = data()
    bad_budget["budgets"] = budgets(wall_ms=-1)
    with pytest.raises(EventFieldError, match="invalid value -1 for field 'wall_ms'"):
        validate_event_data("run.checkpoint", bad_budget, seq=1)
    bool_budget = data()
    bool_budget["budgets"] = budgets(input_tokens=True)
    with pytest.raises(EventFieldError, match="invalid value True for field 'input_tokens'"):
        validate_event_data("run.checkpoint", bool_budget, seq=1)

    bad_state = data()
    bad_state["tool_calls"] = [
        {"call_id": CALL_ID, "name": "read_file", "state": "done", "idempotency_key": IDEM_KEY}
    ]
    with pytest.raises(EventFieldError, match="invalid value 'done' for field 'state'"):
        validate_event_data("run.checkpoint", bad_state, seq=1)
    bad_key = data()
    bad_key["tool_calls"] = [
        {"call_id": CALL_ID, "name": "read_file", "state": "completed", "idempotency_key": "nope"}
    ]
    with pytest.raises(EventFieldError, match="invalid value 'nope' for field 'idempotency_key'"):
        validate_event_data("run.checkpoint", bad_key, seq=1)
    bad_approval = data()
    bad_approval["approvals"] = [{"approval_id": "approval-1", "state": "pending"}]
    with pytest.raises(EventFieldError, match="requires field 'tool_call_id'"):
        validate_event_data("run.checkpoint", bad_approval, seq=1)
    bad_message_id = data()
    bad_message_id["last_message_id"] = "message-1"
    with pytest.raises(EventFieldError, match="invalid value 'message-1' for field 'last_message_id'"):
        validate_event_data("run.checkpoint", bad_message_id, seq=1)
    bad_turn = data()
    bad_turn["turn_id"] = "turn-1"
    with pytest.raises(EventFieldError, match="invalid value 'turn-1' for field 'turn_id'"):
        validate_event_data("run.checkpoint", bad_turn, seq=1)


def test_run_finished_and_started_shapes_are_strict() -> None:
    validate_event_data("run.started", {}, seq=1)
    validate_event_data("run.finished", {"status": "cancelled"}, seq=1)
    validate_event_data(
        "run.finished",
        {"status": "failed", "reason": {"kind": "error", "message": "boom", "code": "E1"}},
        seq=1,
    )
    with pytest.raises(EventFieldError, match="unknown field 'turn_id'"):
        validate_event_data("run.started", {"turn_id": TURN_ID}, seq=1)
    with pytest.raises(EventFieldError, match="invalid value 'running' for field 'status'"):
        validate_event_data("run.finished", {"status": "running"}, seq=1)
    with pytest.raises(EventFieldError, match="field 'reason' has invalid value"):
        validate_event_data("run.finished", {"status": "failed", "reason": {"kind": "bogus"}}, seq=1)


def test_run_events_are_tracked_but_ignored_by_fold(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    path = session.path
    try:
        session.append(EventKind.RUN_STARTED, run_id=RUN_ID, trace_id=TRACE_ID)
        session.append(EventKind.TURN_STARTED, run_id=RUN_ID, turn_id=TURN_ID, trace_id=TRACE_ID)
        message = Message(
            "msg_0000000000000011",
            "user",
            (TextPart("hello"),),
            1_700_000_000_000,
            UserSource(),
        )
        session.append(
            EventKind.USER_MESSAGE,
            {"message": message.to_dict()},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
        )
        session.append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "completed"}},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
        )
        session.append(EventKind.RUN_FINISHED, {"status": "completed"}, run_id=RUN_ID, trace_id=TRACE_ID)
    finally:
        session.close()
    log = NativeCodec.read(path)
    view = fold(log.header, log.events)
    assert [item.id for item in view.messages] == [message.id]
    assert view.issues == ()
    assert view.last_seq == log.events[-1].seq
    assert view.last_message_id == message.id


def test_run_event_data_is_validated_before_write(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    path = session.path
    try:
        before = path.read_bytes()
        with pytest.raises(EventFieldError):
            session.append(EventKind.RUN_CHECKPOINT, {"last_seq": 1}, run_id=RUN_ID, trace_id=TRACE_ID)
        assert path.read_bytes() == before
        assert session.last_seq == 0
    finally:
        session.close()


def test_step_inside_turn_is_still_required_after_run_events(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    try:
        session.append(EventKind.RUN_STARTED, run_id=RUN_ID, trace_id=TRACE_ID)
        session.append(EventKind.TURN_STARTED, run_id=RUN_ID, turn_id=TURN_ID, trace_id=TRACE_ID)
        with pytest.raises(OrderingError, match="outside turn"):
            session.append(
                EventKind.STEP_STARTED,
                run_id=RUN_ID,
                turn_id=TURN_ID_2,
                step_id=STEP_ID,
                trace_id=TRACE_ID,
            )
    finally:
        session.close()


def test_run_checkpoint_carries_unitless_float_cost(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    path = session.path
    try:
        session.append(
            EventKind.RUN_CHECKPOINT,
            checkpoint_data(budgets=budgets(cost_usd=0)),
            run_id=RUN_ID,
            trace_id=TRACE_ID,
        )
    finally:
        session.close()
    event = NativeCodec.read(path).events[0]
    assert event.data["budgets"]["cost_usd"] == 0

    with pytest.raises(EventFieldError):
        validate_event_data("run.checkpoint", checkpoint_data(budgets=budgets(cost_usd=-0.1)), seq=1)
    infinite: dict[str, object] = checkpoint_data()
    infinite["budgets"] = budgets(cost_usd=float("inf"))
    with pytest.raises(EventFieldError):
        validate_event_data("run.checkpoint", infinite, seq=1)


def test_turn_reason_optional_fields_are_accepted() -> None:
    """Docs schema 5.3 lists optional fields; the validator must accept them."""
    validate_event_data("turn.ended", {"reason": {"kind": "max_tokens"}}, seq=1)
    validate_event_data("turn.ended", {"reason": {"kind": "max_tokens", "limit": 10}}, seq=1)
    validate_event_data("turn.ended", {"reason": {"kind": "error", "message": "boom", "code": "E1"}}, seq=1)
    validate_event_data("turn.ended", {"reason": {"kind": "aborted", "by": "user", "message": "bye"}}, seq=1)
