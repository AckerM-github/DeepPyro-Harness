"""Session ancestry and positional replacement traces."""

from __future__ import annotations

import itertools

import pytest

from deeppyro.session import read_session_file
from deeppyro.session_query import (
    EVENT_NOT_FOUND,
    INVALID_FILTER,
    SESSION_NOT_FOUND,
    SessionQuery,
    SessionQueryError,
)
from tests.session.support import session_files


def test_trace_session_ancestors_and_descendants(query: SessionQuery, manifest: dict) -> None:
    known = {entry["id"] for entry in manifest["sessions"]}
    for entry in manifest["sessions"]:
        trace = query.trace_session(entry["id"])
        parent = entry.get("parentSession")
        if parent and parent in known:
            assert [item.id for item in trace.ancestors][-1] == parent
            assert entry["id"] in [item.id for item in query.trace_session(parent).descendants]
        else:
            assert trace.ancestors == []
        assert entry["id"] not in [item.id for item in trace.descendants]


def test_trace_session_fork_chain(query: SessionQuery) -> None:
    trace = query.trace_session("session-66de8981-4091-4bdc-9ff1-b23470ad6fa9")
    assert [item.id for item in trace.ancestors] == ["session-c423d78a-a941-4ac9-baec-8ab59ba6462c"]
    assert trace.descendants == []
    parent_trace = query.trace_session("session-c423d78a-a941-4ac9-baec-8ab59ba6462c")
    assert "session-66de8981-4091-4bdc-9ff1-b23470ad6fa9" in [item.id for item in parent_trace.descendants]


def test_trace_session_non_seeded_subagent_has_parent_edge(query: SessionQuery) -> None:
    trace = query.trace_session("7fdb12e1-c6c4-4059-8a53-e7d9d566de7c")
    assert [item.id for item in trace.ancestors] == ["session-ddb1ba3d-d055-48c1-9b60-2ad5897dd31b"]


def test_trace_session_unknown(query: SessionQuery) -> None:
    with pytest.raises(SessionQueryError) as excinfo:
        query.trace_session("session-missing")
    assert excinfo.value.code == SESSION_NOT_FOUND


def test_trace_event_system_prompt_replacement(query: SessionQuery) -> None:
    chain = query.trace_event("session-ddb1ba3d-d055-48c1-9b60-2ad5897dd31b", 8)
    assert [record.seq for record in chain] == [8, 50]
    assert chain[-1].type == "system/message"


def test_trace_event_developer_retirement(query: SessionQuery) -> None:
    chain = query.trace_event("session-d745f174-c79f-4b69-a147-d6c57947e3f7", 13)
    assert [record.seq for record in chain] == [13, 146]
    assert chain[-1].type == "developer/message"
    assert chain[-1].sourceEventSeqs == [13]


def test_trace_event_follows_chains_to_the_final_node(query: SessionQuery) -> None:
    checked = 0
    for path in session_files():
        session_id = path.stem
        records = read_session_file(path).records
        replaced = {
            seq
            for record in records
            if record.replacement() is not None
            for seq in range(record.replacement().startSeq, record.replacement().endSeq + 1)
        }
        for record in records:
            replacement = record.replacement()
            if replacement is None:
                continue
            chain = query.trace_event(session_id, replacement.startSeq)
            assert next(item.seq for item in chain) == replacement.startSeq
            assert all(left.seq < right.seq for left, right in itertools.pairwise(chain))
            assert chain[-1].seq not in replaced
            shadowed = chain[-1].sourceEventSeqs or []
            assert set(shadowed) <= {item.seq for item in chain}
            checked += 1
    assert checked >= 9


def test_trace_event_unknown_and_invalid(query: SessionQuery) -> None:
    session_id = "session-d745f174-c79f-4b69-a147-d6c57947e3f7"
    with pytest.raises(SessionQueryError) as excinfo:
        query.trace_event(session_id, 10**9)
    assert excinfo.value.code == EVENT_NOT_FOUND
    with pytest.raises(SessionQueryError) as excinfo:
        query.trace_event(session_id, -1)
    assert excinfo.value.code == INVALID_FILTER
    with pytest.raises(SessionQueryError) as excinfo:
        query.trace_event("session-missing", 0)
    assert excinfo.value.code == SESSION_NOT_FOUND
