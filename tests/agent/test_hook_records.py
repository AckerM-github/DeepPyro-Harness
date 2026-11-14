"""Hook journal records written through AgentSession.record_hook_event.

The P2 hook journal observes every hook dispatch and persists a pair of plain
records per dispatch (hook/invoked then hook/result).  These tests pin the
writer contract: only the two hook record types are accepted, seqs keep
increasing, the record is durable before the live sink event, and neither
derivation nor the session header is affected.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.errors import SessionError
from deeppyro.session import SessionStore
from deeppyro.session_query import SessionQuery

from .fakes import FakePermission, FakeRegistry, FakeSessionLog, RecordingSink, ScriptedProvider


def make_agent(tmp_path: pathlib.Path, session: Any | None = None) -> tuple[Any, RecordingSink, AgentSession]:
    """Build a loop over an in-memory (or real) session log."""
    log = session if session is not None else FakeSessionLog(cwd=tmp_path)
    sink = RecordingSink(log)
    agent = AgentSession(
        session=log,
        provider=ScriptedProvider([]),
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
    )
    return log, sink, agent


def test_records_get_increasing_seqs_and_keep_their_payload(tmp_path: pathlib.Path) -> None:
    """Two dispatches append two records, in order, with the original payload."""
    log, _sink, agent = make_agent(tmp_path)
    invoked = agent.record_hook_event(
        "hook/invoked", {"hook": "llm/stream", "mode": "waterfall", "turn": 1, "step": 2}
    )
    result = agent.record_hook_event(
        "hook/result", {"hook": "llm/stream", "mode": "waterfall", "ok": True, "durationMs": 3}
    )

    assert (invoked, result) == (1, 2)
    assert [record.type for record in log.records] == ["hook/invoked", "hook/result"]
    assert log.records[0].seq < log.records[1].seq
    assert log.records[0].data == {
        "hook": "llm/stream",
        "mode": "waterfall",
        "turn": 1,
        "step": 2,
    }
    assert log.records[1].data["durationMs"] == 3


def test_hook_records_are_not_surface_records(tmp_path: pathlib.Path) -> None:
    """Journal records carry no surface envelope, so the fold never sees them."""
    log, _sink, agent = make_agent(tmp_path)
    agent.record_hook_event("hook/invoked", {"hook": "tool/call", "mode": "serial"})

    record = log.records[0]
    assert getattr(record, "surfaceOp", None) is None
    assert getattr(record, "sourceEventSeqs", None) is None


def test_live_event_mirrors_the_persisted_record(tmp_path: pathlib.Path) -> None:
    """The sink sees {type, seq, **data} only after the record is on the log."""
    log, sink, agent = make_agent(tmp_path)
    seq = agent.record_hook_event(
        "hook/result", {"hook": "tool/call", "mode": "serial", "ok": False, "outcome": "error"}
    )

    event = sink.events[-1]
    assert event["type"] == "hook/result"
    assert event["seq"] == seq
    assert event["hook"] == "tool/call"
    assert event["mode"] == "serial"
    assert event["ok"] is False
    assert event["outcome"] == "error"
    # The record existed before the event was emitted.
    assert sink.record_counts[-1] == len(log.records) == 1


def test_payload_is_copied_before_persisting(tmp_path: pathlib.Path) -> None:
    """Mutating the caller's dict after the call cannot rewrite the record."""
    log, sink, agent = make_agent(tmp_path)
    data: dict[str, Any] = {"hook": "agent/request", "mode": "waterfall"}
    agent.record_hook_event("hook/invoked", data)

    data["hook"] = "mutated"
    assert log.records[0].data["hook"] == "agent/request"
    assert sink.events[-1]["hook"] == "agent/request"


@pytest.mark.parametrize("record_type", ["user/message", "turn/start", "hook/unknown", ""])
def test_rejects_non_hook_record_types(tmp_path: pathlib.Path, record_type: str) -> None:
    """Anything but the two journal record types is a contract violation."""
    log, sink, agent = make_agent(tmp_path)
    with pytest.raises(SessionError):
        agent.record_hook_event(record_type, {"hook": "llm/stream"})
    assert log.records == []
    assert sink.events == []


def test_hook_records_leave_derivation_and_header_untouched(tmp_path: pathlib.Path) -> None:
    """The canonical surface and the header are unaffected by journal records."""
    log, _sink, agent = make_agent(tmp_path)
    header_before = log.header.model_dump()
    agent.record_hook_event("hook/invoked", {"hook": "llm/stream", "mode": "waterfall"})
    agent.record_hook_event("hook/result", {"hook": "llm/stream", "mode": "waterfall", "ok": True})
    agent._append(
        "user/message",
        {"id": "msg-hook-1", "role": "user", "content": [{"type": "text", "text": "hello"}]},
        surfaceOp="append",
    )

    messages = log.derive_messages()
    assert [message.role for message in messages] == ["user"]
    assert messages[0].text() == "hello"
    assert log.header.model_dump() == header_before


def test_roundtrip_and_hook_trace_over_a_real_store(tmp_path: pathlib.Path) -> None:
    """Journal records survive close/open and the query service can pair them."""
    store = SessionStore(tmp_path / "sessions")
    session = store.create(tmp_path, session_id="session-hook-journal")
    _log, _sink, agent = make_agent(tmp_path, session=session)
    agent.record_hook_event("hook/invoked", {"hook": "llm/stream", "mode": "waterfall", "turn": 1, "step": 1})
    agent.record_hook_event(
        "hook/result", {"hook": "llm/stream", "mode": "waterfall", "ok": True, "durationMs": 2}
    )
    session.close()

    reopened = store.open("session-hook-journal")
    try:
        assert [record.type for record in reopened.records] == ["hook/invoked", "hook/result"]
        assert [getattr(record, "surfaceOp", None) for record in reopened.records] == [None, None]
        assert reopened.derive_messages() == []
        trace = SessionQuery(store).hook_trace("session-hook-journal")
        assert [pair["hook"] for pair in trace] == ["llm/stream"]
        assert trace[0]["invoked"]["step"] == 1
        assert trace[0]["result"]["durationMs"] == 2
    finally:
        reopened.close()
