"""Tests for the durable agent inbox projection and commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.inbox import (
    CLAIMED_EVENT,
    DISCARDED_EVENT,
    INSERTED_EVENT,
    SPLICED_EVENT,
    TARGET_NEXT_STEP,
    TARGET_NEXT_TURN,
    TARGETS,
    Inbox,
)
from deeppyro.core.errors import SessionError
from deeppyro.core.wire import Message, TextPart
from deeppyro.session import SessionStore, read_session_file

from .fakes import FakeSessionLog


def make_message(mid: str, text: str = "hello") -> Message:
    return Message(role="user", content=[TextPart(text=text)], id=mid, source={"kind": "user"})


def dump(mid: str, text: str = "hello") -> dict[str, Any]:
    return make_message(mid, text).model_dump(exclude_none=True)


def ids(messages: tuple[Message, ...] | list[Message]) -> list[str]:
    return [message.id for message in messages]


@dataclass
class Recorder:
    """Process-local event recorder matching the emit callback signature."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.events if name == event_type]

    def types(self) -> list[str]:
        return [name for name, _ in self.events]


# -- insertion --------------------------------------------------------------


def test_push_and_prepend_preserve_queue_order() -> None:
    log = FakeSessionLog()
    inbox = Inbox(log)

    inbox.push(TARGET_NEXT_TURN, make_message("m1"))
    inbox.push(TARGET_NEXT_TURN, make_message("m2"))
    inbox.push(TARGET_NEXT_TURN, make_message("m3"), front=True)

    assert ids(inbox.next_turn) == ["m3", "m1", "m2"]
    assert inbox.next_step == ()
    assert inbox.has_pending

    records = log.records_of(SPLICED_EVENT)
    assert [record.data["start"] for record in records] == [0, 1, 0]
    assert records[0].data == {
        "target": TARGET_NEXT_TURN,
        "start": 0,
        "inserted": [dump("m1")],
    }
    assert "removedCount" not in records[0].data
    assert "outcome" not in records[0].data


def test_push_supports_both_targets_and_snapshot_is_detached() -> None:
    inbox = Inbox()
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))
    inbox.append(TARGET_NEXT_TURN, make_message("t1"))
    inbox.prepend(TARGET_NEXT_STEP, make_message("s0"))

    assert ids(inbox.next_step) == ["s0", "s1"]
    assert ids(inbox.next_turn) == ["t1"]
    assert inbox.snapshot() == {
        TARGET_NEXT_TURN: [dump("t1")],
        TARGET_NEXT_STEP: [dump("s0"), dump("s1")],
    }

    snapshot = inbox.snapshot()
    snapshot[TARGET_NEXT_TURN].append({"id": "bogus"})
    snapshot[TARGET_NEXT_TURN][0]["id"] = "mutated"
    assert ids(inbox.next_turn) == ["t1"]
    assert [message.id for message in inbox.next_step] == ["s0", "s1"]


def test_push_rejects_unknown_target_and_non_message() -> None:
    log = FakeSessionLog()
    inbox = Inbox(log)

    with pytest.raises(ValueError, match="unknown inbox target"):
        inbox.push("bogus", make_message("m1"))
    with pytest.raises(TypeError, match="Message"):
        inbox.push(TARGET_NEXT_TURN, {"id": "m1"})  # type: ignore[arg-type]
    assert inbox.has_pending is False
    assert log.records_of(SPLICED_EVENT) == []


def test_duplicate_ids_across_queues_are_rejected_without_writing() -> None:
    log = FakeSessionLog()
    inbox = Inbox(log)
    inbox.push(TARGET_NEXT_TURN, make_message("m1"))

    with pytest.raises(SessionError, match='message "m1" is already pending'):
        inbox.push(TARGET_NEXT_STEP, make_message("m1"))
    with pytest.raises(SessionError, match='message "m1" is already pending'):
        inbox.splice(TARGET_NEXT_STEP, 0, 0, [make_message("m1")])

    assert ids(inbox.next_turn) == ["m1"]
    assert inbox.next_step == ()
    assert len(log.records_of(SPLICED_EVENT)) == 1


# -- splice normalization ---------------------------------------------------


@pytest.mark.parametrize(
    ("start", "delete_count", "inserted", "remaining", "removed"),
    [
        (-1, 1, [], ["a", "b", "c"], ["d"]),
        (-99, 1, [], ["b", "c", "d"], ["a"]),
        (1.9, 1.9, [], ["a", "c", "d"], ["b"]),
        (float("nan"), 2, [], ["c", "d"], ["a", "b"]),
        (1, float("inf"), [], ["a"], ["b", "c", "d"]),
        (1, -5, ["x"], ["a", "x", "b", "c", "d"], []),
        (99, 0, ["x"], ["a", "b", "c", "d", "x"], []),
    ],
)
def test_splice_normalizes_coordinates(
    start: Any,
    delete_count: Any,
    inserted: list[str],
    remaining: list[str],
    removed: list[str],
) -> None:
    log = FakeSessionLog()
    inbox = Inbox(log)
    for mid in ["a", "b", "c", "d"]:
        inbox.push(TARGET_NEXT_STEP, make_message(mid))

    deleted = inbox.splice(TARGET_NEXT_STEP, start, delete_count, [make_message(mid) for mid in inserted])

    assert ids(deleted) == removed
    assert ids(inbox.next_step) == remaining
    record = log.records_of(SPLICED_EVENT)[-1]
    assert isinstance(record.data["start"], int)
    assert 0 <= record.data["start"] <= 4
    assert record.data["inserted"] == [dump(mid) for mid in inserted]


@pytest.mark.parametrize(
    ("start", "delete_count", "inserted"),
    [
        (0, 0, []),
        (float("inf"), 0, []),
        (-float("inf"), 0, []),
        (99, 3, []),
        (float("nan"), -3, []),
    ],
)
def test_noop_splice_writes_no_record(start: Any, delete_count: Any, inserted: list[Message]) -> None:
    log = FakeSessionLog()
    inbox = Inbox(log)
    inbox.push(TARGET_NEXT_STEP, make_message("a"))

    removed = inbox.splice(TARGET_NEXT_STEP, start, delete_count, inserted)

    assert removed == []
    assert ids(inbox.next_step) == ["a"]
    assert len(log.records_of(SPLICED_EVENT)) == 1  # only the push


def test_splice_discard_marks_cancellation_and_emits_events() -> None:
    log = FakeSessionLog()
    recorder = Recorder()
    inbox = Inbox(log, emit=recorder)
    for mid in ["m1", "m2", "m3"]:
        inbox.push(TARGET_NEXT_TURN, make_message(mid))
    recorder.events.clear()

    removed = inbox.splice(TARGET_NEXT_TURN, 1, 1, [], discard_removed=True)

    assert ids(removed) == ["m2"]
    assert ids(inbox.next_turn) == ["m1", "m3"]
    record = log.records_of(SPLICED_EVENT)[-1]
    assert record.data == {
        "target": TARGET_NEXT_TURN,
        "start": 1,
        "removedCount": 1,
        "inserted": [],
        "outcome": "canceled",
    }
    assert recorder.events == [(DISCARDED_EVENT, {"message": dump("m2")})]

    recorder.events.clear()
    removed = inbox.splice(TARGET_NEXT_TURN, 0, 1, [])
    assert ids(removed) == ["m1"]
    assert "outcome" not in log.records_of(SPLICED_EVENT)[-1].data
    assert recorder.events == []


def test_splice_insertion_emits_targeted_inserted_event() -> None:
    recorder = Recorder()
    inbox = Inbox(emit=recorder)
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))

    inbox.splice(TARGET_NEXT_STEP, 0, 0, [make_message("s0")])

    assert ids(inbox.next_step) == ["s0", "s1"]
    assert recorder.of(INSERTED_EVENT)[-1] == {
        "message": dump("s0"),
        "target": TARGET_NEXT_STEP,
    }


# -- claiming ---------------------------------------------------------------


def test_claim_consumes_all_next_step_then_one_next_turn() -> None:
    log = FakeSessionLog()
    recorder = Recorder()
    inbox = Inbox(log, emit=recorder)
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))
    inbox.push(TARGET_NEXT_STEP, make_message("s2"))
    inbox.push(TARGET_NEXT_TURN, make_message("t1"))
    inbox.push(TARGET_NEXT_TURN, make_message("t2"))
    recorder.events.clear()

    claimed = inbox.claim(TARGET_NEXT_TURN, turn=7)

    assert ids(claimed) == ["s1", "s2", "t1"]
    assert inbox.next_step == ()
    assert ids(inbox.next_turn) == ["t2"]
    records = log.records_of(SPLICED_EVENT)[-2:]
    assert records[0].data == {
        "target": TARGET_NEXT_STEP,
        "start": 0,
        "removedCount": 2,
        "inserted": [],
    }
    assert records[1].data == {
        "target": TARGET_NEXT_TURN,
        "start": 0,
        "removedCount": 1,
        "inserted": [],
    }
    assert recorder.of(CLAIMED_EVENT) == [
        {"message": dump("s1"), "turn": 7},
        {"message": dump("s2"), "turn": 7},
        {"message": dump("t1"), "turn": 7},
    ]
    assert DISCARDED_EVENT not in recorder.types()


def test_claim_next_step_does_not_consume_a_turn() -> None:
    log = FakeSessionLog()
    inbox = Inbox(log)
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))
    inbox.push(TARGET_NEXT_TURN, make_message("t1"))

    claimed = inbox.claim(TARGET_NEXT_STEP, turn=3)

    assert ids(claimed) == ["s1"]
    assert ids(inbox.next_turn) == ["t1"]
    assert len(log.records_of(SPLICED_EVENT)) == 3  # two pushes and one claim


def test_claim_empty_or_invalid_target_is_a_noop() -> None:
    log = FakeSessionLog()
    inbox = Inbox(log)

    assert inbox.claim(TARGET_NEXT_TURN, turn=1) == []
    assert len(log.records_of(SPLICED_EVENT)) == 0

    inbox.push(TARGET_NEXT_TURN, make_message("t1"))
    with pytest.raises(ValueError, match="unknown inbox target"):
        inbox.claim("bogus", turn=1)
    assert ids(inbox.next_turn) == ["t1"]
    assert len(log.records_of(SPLICED_EVENT)) == 1


# -- clear / remove / replace ----------------------------------------------


def test_clear_cancels_next_step_before_next_turn() -> None:
    log = FakeSessionLog()
    recorder = Recorder()
    inbox = Inbox(log, emit=recorder)
    inbox.push(TARGET_NEXT_TURN, make_message("t1"))
    inbox.push(TARGET_NEXT_TURN, make_message("t2"))
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))
    recorder.events.clear()

    inbox.clear()

    assert inbox.has_pending is False
    assert inbox.next_turn == ()
    assert inbox.next_step == ()
    records = log.records_of(SPLICED_EVENT)[-2:]
    assert records[0].data == {
        "target": TARGET_NEXT_STEP,
        "start": 0,
        "removedCount": 1,
        "inserted": [],
        "outcome": "canceled",
    }
    assert records[1].data == {
        "target": TARGET_NEXT_TURN,
        "start": 0,
        "removedCount": 2,
        "inserted": [],
        "outcome": "canceled",
    }
    assert recorder.of(DISCARDED_EVENT) == [
        {"message": dump("s1")},
        {"message": dump("t1")},
        {"message": dump("t2")},
    ]

    before = len(log.records_of(SPLICED_EVENT))
    inbox.clear()
    assert len(log.records_of(SPLICED_EVENT)) == before


def test_remove_searches_turn_then_step_and_cancels() -> None:
    log = FakeSessionLog()
    recorder = Recorder()
    inbox = Inbox(log, emit=recorder)
    inbox.push(TARGET_NEXT_TURN, make_message("t1"))
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))
    recorder.events.clear()

    assert ids(inbox.remove("s1")) == ["s1"]
    assert inbox.next_step == ()
    assert ids(inbox.next_turn) == ["t1"]
    record = log.records_of(SPLICED_EVENT)[-1]
    assert record.data["outcome"] == "canceled"
    assert record.data["removedCount"] == 1
    assert recorder.of(DISCARDED_EVENT) == [{"message": dump("s1")}]

    before = len(log.records_of(SPLICED_EVENT))
    assert inbox.remove("missing") == []
    assert len(log.records_of(SPLICED_EVENT)) == before


def test_replace_rewrites_in_place_and_validates_identity() -> None:
    log = FakeSessionLog()
    recorder = Recorder()
    inbox = Inbox(log, emit=recorder)
    inbox.push(TARGET_NEXT_TURN, make_message("t1"))
    inbox.push(TARGET_NEXT_TURN, make_message("t2"))
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))
    recorder.events.clear()

    assert inbox.replace("t1", make_message("t1b")) is True
    assert ids(inbox.next_turn) == ["t1b", "t2"]
    assert recorder.of(DISCARDED_EVENT) == [{"message": dump("t1")}]
    assert recorder.of(INSERTED_EVENT)[-1] == {
        "message": dump("t1b"),
        "target": TARGET_NEXT_TURN,
    }

    with pytest.raises(SessionError, match='message "s1" is already pending'):
        inbox.replace("t2", make_message("s1"))
    assert ids(inbox.next_turn) == ["t1b", "t2"]

    before = len(log.records_of(SPLICED_EVENT))
    assert inbox.replace("missing", make_message("x")) is False
    assert len(log.records_of(SPLICED_EVENT)) == before


# -- durable replay ---------------------------------------------------------


def test_from_records_rebuilds_state_and_keeps_appending() -> None:
    log = FakeSessionLog()
    inbox = Inbox(log)
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))
    inbox.push(TARGET_NEXT_TURN, make_message("t1"))
    inbox.push(TARGET_NEXT_TURN, make_message("t2"))
    inbox.claim(TARGET_NEXT_TURN, turn=1)  # consumes s1 + t1

    replayed = Inbox.from_records(log.records, log=log)

    assert replayed.snapshot() == inbox.snapshot()
    assert ids(replayed.next_turn) == ["t2"]
    assert replayed.next_step == ()

    replayed.push(TARGET_NEXT_TURN, make_message("t3"))
    assert ids(replayed.next_turn) == ["t2", "t3"]
    assert log.records_of(SPLICED_EVENT)[-1].data["start"] == 1


def test_from_records_ignores_non_splice_records() -> None:
    log = FakeSessionLog()
    log.append("turn/start", {"turn": 1})
    inbox = Inbox(log)
    inbox.push(TARGET_NEXT_TURN, make_message("t1"))
    log.append("session/title", {"title": "hello"})

    replayed = Inbox.from_records(log.records)

    assert ids(replayed.next_turn) == ["t1"]


def test_from_records_accepts_null_removed_count() -> None:
    records = [
        {"type": SPLICED_EVENT, "seq": 4, "data": {"target": TARGET_NEXT_TURN, "start": 0, "inserted": []}},
        {
            "type": SPLICED_EVENT,
            "seq": 5,
            "data": {
                "target": TARGET_NEXT_TURN,
                "start": 0,
                "removedCount": None,
                "inserted": [dump("t1")],
            },
        },
    ]

    inbox = Inbox.from_records(records)

    assert ids(inbox.next_turn) == ["t1"]


@pytest.mark.parametrize(
    "data",
    [
        {"target": "bogus", "start": 0, "inserted": []},
        {"target": TARGET_NEXT_TURN, "start": -1, "inserted": []},
        {"target": TARGET_NEXT_TURN, "start": 1, "inserted": []},
        {"target": TARGET_NEXT_TURN, "start": 0, "removedCount": 1, "inserted": []},
        {"target": TARGET_NEXT_TURN, "start": 0, "removedCount": -1, "inserted": []},
        {"target": TARGET_NEXT_TURN, "start": 0.0, "inserted": []},
        {"target": TARGET_NEXT_TURN, "start": 0, "removedCount": 0.5, "inserted": []},
        {"target": TARGET_NEXT_TURN, "start": 0, "inserted": "nope"},
        {"target": TARGET_NEXT_TURN, "start": 0, "inserted": [{"id": "broken"}]},
        "not-a-mapping",
    ],
)
def test_from_records_rejects_invalid_persisted_splices(data: Any) -> None:
    records = [{"type": SPLICED_EVENT, "seq": 17, "data": data}]

    with pytest.raises(SessionError, match="invalid persisted inbox splice at session seq 17"):
        Inbox.from_records(records)


def test_from_records_rejects_duplicate_ids_in_persisted_history() -> None:
    records = [
        {
            "type": SPLICED_EVENT,
            "seq": 2,
            "data": {"target": TARGET_NEXT_TURN, "start": 0, "inserted": [dump("same")]},
        },
        {
            "type": SPLICED_EVENT,
            "seq": 3,
            "data": {"target": TARGET_NEXT_STEP, "start": 0, "inserted": [dump("same")]},
        },
    ]

    with pytest.raises(SessionError, match="invalid persisted inbox splice at session seq 3"):
        Inbox.from_records(records)


def test_replays_every_inbox_splice_in_the_fixtures() -> None:
    session_dir = Path(__file__).resolve().parents[2] / "fixtures" / "sessions"
    files = sorted(session_dir.glob("*.jsonl"))
    assert len(files) == 30

    total = 0
    for path in files:
        read = read_session_file(path)
        records = [record for record in read.records if record.type == SPLICED_EVENT]
        inbox = Inbox.from_records(records)
        assert isinstance(inbox.snapshot(), dict)
        total += len(records)
    assert total == 328


def test_real_session_round_trip_through_store(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = store.create(tmp_path)
    inbox = Inbox(session)
    inbox.push(TARGET_NEXT_TURN, make_message("t1"))
    inbox.push(TARGET_NEXT_STEP, make_message("s1"))

    reopened = store.open(session.session_id)
    restored = Inbox.from_records(reopened.records, log=reopened)

    assert restored.snapshot() == inbox.snapshot()
    assert ids(restored.next_turn) == ["t1"]
    assert ids(restored.next_step) == ["s1"]

    restored.push(TARGET_NEXT_STEP, make_message("s2"))
    reopened.close()

    again = store.open(session.session_id)
    final = Inbox.from_records(again.records, log=again)
    assert ids(final.next_step) == ["s1", "s2"]
    again.close()


def test_targets_constant_matches_persisted_vocabulary() -> None:
    assert TARGETS == (TARGET_NEXT_TURN, TARGET_NEXT_STEP)
    assert TARGET_NEXT_TURN == "next-turn"
    assert TARGET_NEXT_STEP == "next-step"
