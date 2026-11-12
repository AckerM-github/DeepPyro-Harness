"""Cross-cutting invariants over all real session fixtures."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.protocols import SessionLogLike
from deeppyro.core.wire import LogRecord, Message, ReplaceOp
from deeppyro.session import Session, SessionStore, read_session_file
from deeppyro.session.derive import APPEND, REPLACE, fold_surface, plan_surface_transition
from deeppyro.session.events import KNOWN_EVENT_TYPES, SURFACE_EVENTS
from tests.session import support

if not support.FIXTURES.is_dir():
    pytest.skip(
        "real DSH fixtures not present; run: node tools/gen_fixtures.mjs",
        allow_module_level=True,
    )

MESSAGE_ROLES = {"system", "user", "assistant", "tool", "developer"}


@pytest.fixture(scope="module")
def fixture_sessions() -> list[tuple[Path, Any]]:
    return [(path, read_session_file(path)) for path in support.session_files()]


def _message_ids(record: LogRecord) -> list[str]:
    if record.type == "user/message":
        return [record.data["id"]]
    if record.type in {"system/message", "developer/message", "assistant/message", "tool/result"}:
        return [record.data["message"]["id"]]
    return []


def _surface_fold(records: list[LogRecord]) -> tuple[list[LogRecord], list[tuple[LogRecord, list[int]]]]:
    """Replay the fold while recording every replacement and its shadowed seqs."""
    by_seq = {record.seq: record for record in records}
    nodes: list[LogRecord] = []
    replacements: list[tuple[LogRecord, list[int]]] = []
    for record in records:
        kind, start, end = plan_surface_transition(nodes, record, strict=True, records_by_seq=by_seq)
        if kind == APPEND:
            nodes.append(record)
        elif kind == REPLACE:
            assert start is not None and end is not None
            replacements.append((record, [node.seq for node in nodes[start : end + 1]]))
            nodes[start : end + 1] = [record]
    return nodes, replacements


def test_every_surface_record_carries_a_marker_and_others_do_not(
    fixture_sessions: list[tuple[Path, Any]],
) -> None:
    for path, read in fixture_sessions:
        for record in read.records:
            if record.type in SURFACE_EVENTS:
                assert record.surfaceOp is not None, (path.name, record.seq, record.type)
                assert isinstance(record.surfaceOp, (str, ReplaceOp)), (path.name, record.seq)
                if isinstance(record.surfaceOp, str):
                    assert record.surfaceOp == "append"
            else:
                assert record.surfaceOp is None, (path.name, record.seq, record.type)


def test_replacements_reference_contiguous_earlier_shadowed_nodes(
    fixture_sessions: list[tuple[Path, Any]],
) -> None:
    saw_replacement = False
    for path, read in fixture_sessions:
        _nodes, replacements = _surface_fold(read.records)
        for record, shadowed in replacements:
            saw_replacement = True
            op = record.surfaceOp
            assert isinstance(op, ReplaceOp), (path.name, record.seq)
            assert 0 <= op.startSeq <= op.endSeq < record.seq, (path.name, record.seq)
            assert shadowed, (path.name, record.seq)
            assert all(seq < record.seq for seq in shadowed)
            assert set(shadowed) <= set(record.sourceEventSeqs or []), (path.name, record.seq, shadowed)
            assert record.type in SURFACE_EVENTS
    assert saw_replacement


def test_source_event_seqs_are_earlier_unique_and_ordered(
    fixture_sessions: list[tuple[Path, Any]],
) -> None:
    for path, read in fixture_sessions:
        for record in read.records:
            sources = record.sourceEventSeqs
            if sources is None:
                continue
            assert sources, (path.name, record.seq)
            assert sources == sorted(sources), (path.name, record.seq)
            assert len(set(sources)) == len(sources), (path.name, record.seq)
            assert all(seq < record.seq for seq in sources), (path.name, record.seq)
            assert record.type != "assistant/message", (path.name, record.seq)


def test_tool_results_pair_with_calls(fixture_sessions: list[tuple[Path, Any]]) -> None:
    for path, read in fixture_sessions:
        calls = {record.data["callId"]: record.seq for record in read.records if record.type == "tool/call"}
        for record in read.records:
            if record.type != "tool/result":
                continue
            message = record.data["message"]
            call_id = message["source"]["callId"]
            assert message["toolCallId"] == call_id, (path.name, record.seq)
            assert record.surfaceOp == "append", (path.name, record.seq)
            if call_id in calls:
                assert calls[call_id] in (record.sourceEventSeqs or []), (path.name, record.seq, call_id)
            else:
                assert read.trimmed, (path.name, record.seq, call_id)


def test_nontrimmed_tool_results_are_a_subset_of_tool_calls(
    fixture_sessions: list[tuple[Path, Any]],
) -> None:
    for path, read in fixture_sessions:
        if read.trimmed:
            continue
        calls = {record.data["callId"] for record in read.records if record.type == "tool/call"}
        results = {
            record.data["message"]["source"]["callId"]
            for record in read.records
            if record.type == "tool/result"
        }
        assert results <= calls, (path.name, results - calls)


def test_every_tool_call_is_visible_in_an_assistant_message(
    fixture_sessions: list[tuple[Path, Any]],
) -> None:
    for path, read in fixture_sessions:
        calls = {record.data["callId"] for record in read.records if record.type == "tool/call"}
        parts: set[str] = set()
        for record in read.records:
            if record.type != "assistant/message":
                continue
            for part in record.data["message"]["content"]:
                if part.get("type") == "tool-call":
                    parts.add(part["id"])
        assert calls <= parts, (path.name, calls - parts)


def test_strict_fold_and_derive_agree(fixture_sessions: list[tuple[Path, Any]]) -> None:
    for path, read in fixture_sessions:
        nodes = fold_surface(read.records, strict=True)
        assert {node.seq for node in nodes} <= {record.seq for record in read.records}
        derived = Session(
            path=path,
            header=read.header,
            records=read.records,
            truncated=read.truncated,
            trimmed=read.trimmed,
            zstd=path.name.endswith(".zstd"),
        ).derive_messages()
        assert len(derived) <= len(nodes)


def test_derived_messages_are_valid_wire_messages(
    fixture_sessions: list[tuple[Path, Any]],
) -> None:
    for path, read in fixture_sessions:
        derived = Session(
            path=path, header=read.header, records=read.records, trimmed=read.trimmed
        ).derive_messages()
        ids = [message.id for message in derived]
        assert len(set(ids)) == len(ids), path.name
        known_ids = {message_id for record in read.records for message_id in _message_ids(record)}
        for message in derived:
            assert isinstance(message, Message), path.name
            assert message.role in MESSAGE_ROLES, path.name
            assert message.id and message.id in known_ids, (path.name, message.id)
            assert isinstance(message.content, list), path.name
            if message.role in {"system", "developer", "assistant"}:
                assert message.content, (path.name, message.id)
            if message.role == "tool":
                assert message.toolCallId, (path.name, message.id)
                assert isinstance(message.isError, bool), (path.name, message.id)
        assert all(message.role != "developer" for message in derived), path.name


def test_manifest_aggregates_match_observed_logs(fixture_sessions: list[tuple[Path, Any]]) -> None:
    manifest = support.load_manifest()
    by_file = {entry["file"]: entry for entry in manifest["sessions"]}
    assert len(fixture_sessions) == 30
    total_records = sum(len(read.records) for _path, read in fixture_sessions)
    total_expected = sum(
        by_file[path.name]["keptCount"]
        if by_file[path.name]["trimmed"]
        else by_file[path.name]["recordCount"]
        for path, _read in fixture_sessions
    )
    assert total_records == total_expected
    assert sum(1 for _path, read in fixture_sessions if read.trimmed) == 4
    assert sum(1 for _path, read in fixture_sessions if read.truncated) == 0
    types = Counter(record.type for _path, read in fixture_sessions for record in read.records)
    assert set(types) <= KNOWN_EVENT_TYPES
    surfaced = Counter(
        record.type
        for _path, read in fixture_sessions
        for record in read.records
        if record.type in SURFACE_EVENTS
    )
    assert sum(surfaced.values()) == sum(
        count for event_type, count in types.items() if event_type in SURFACE_EVENTS
    )


def test_fixture_statistics_are_stable() -> None:
    path = support.SESSIONS_DIR / "session-d745f174-c79f-4b69-a147-d6c57947e3f7.jsonl"
    read = read_session_file(path)
    session = Session(path=path, header=read.header, records=read.records)
    assert session.usage() == {"input": 92530, "output": 21643, "cached": 397184}
    assert session.turn_count() == 3
    assert session.last_step() == (3, 1)
    assert len(session.derive_messages()) == 41


def test_session_satisfies_the_log_protocol(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(Path("C:/work"))
    assert isinstance(session, SessionLogLike)
    assert session.append("turn/start", {"turn": 1}) == 0
    assert isinstance(session.derive_messages(), list)
