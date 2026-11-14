"""SessionQuery.hook_trace: pairing hook/invoked with hook/result records.

The trace feed behind the TUI /hooks view pairs each dispatch with the record
it settled into, using seq order and the hook name only.  These tests pin the
pairing rules (most recent unmatched invocation, orphan results ignored),
the limit window and the error codes.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from deeppyro.session_query import INVALID_FILTER, SESSION_NOT_FOUND, SessionQuery, SessionQueryError
from deeppyro.session_query.query import INVOKED_RECORD_TYPE, RESULT_RECORD_TYPE
from tests.session_query.support import new_session, tmp_store


def invoked(hook: str = "llm/stream", mode: str = "waterfall", **extra: Any) -> dict[str, Any]:
    return {"type": INVOKED_RECORD_TYPE, "data": {"hook": hook, "mode": mode, **extra}}


def result(hook: str = "llm/stream", mode: str = "waterfall", **extra: Any) -> dict[str, Any]:
    return {"type": RESULT_RECORD_TYPE, "data": {"hook": hook, "mode": mode, "ok": True, **extra}}


def test_pairs_invocation_with_its_result(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    new_session(store, [invoked(turn=1, step=2), result(durationMs=4, outcome="value")])

    trace = SessionQuery(store).hook_trace("session-query-test")
    assert len(trace) == 1
    assert trace[0]["hook"] == "llm/stream"
    assert trace[0]["mode"] == "waterfall"
    assert trace[0]["invoked"] == {"hook": "llm/stream", "mode": "waterfall", "turn": 1, "step": 2}
    assert trace[0]["result"]["durationMs"] == 4
    assert trace[0]["result"]["outcome"] == "value"


def test_unsettled_invocation_has_no_result(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    new_session(store, [invoked(turn=1)])

    trace = SessionQuery(store).hook_trace("session-query-test")
    assert len(trace) == 1
    assert trace[0]["result"] is None


def test_orphan_result_is_ignored(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    new_session(store, [result(durationMs=1), invoked(turn=1), result(durationMs=2)])

    trace = SessionQuery(store).hook_trace("session-query-test")
    assert len(trace) == 1
    assert trace[0]["invoked"]["turn"] == 1
    assert trace[0]["result"]["durationMs"] == 2


def test_result_pairs_with_the_most_recent_unmatched_invocation(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    new_session(store, [invoked(turn=1), invoked(turn=2), result(durationMs=7)])

    trace = SessionQuery(store).hook_trace("session-query-test")
    assert [pair["invoked"]["turn"] for pair in trace] == [1, 2]
    assert trace[0]["result"] is None
    assert trace[1]["result"]["durationMs"] == 7


def test_results_do_not_cross_hooks(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    new_session(
        store,
        [
            invoked(hook="tool/call", mode="serial"),
            invoked(hook="llm/stream", mode="waterfall"),
            result(hook="llm/stream", durationMs=5),
            result(hook="tool/call", durationMs=6),
        ],
    )

    trace = SessionQuery(store).hook_trace("session-query-test")
    by_hook = {pair["hook"]: pair for pair in trace}
    assert by_hook["tool/call"]["result"]["durationMs"] == 6
    assert by_hook["llm/stream"]["result"]["durationMs"] == 5


def test_limit_keeps_the_newest_pairs_in_seq_order(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    events: list[dict[str, Any]] = []
    for turn in (1, 2, 3):
        events.extend([invoked(turn=turn), result(turn=turn)])
    new_session(store, events)

    trace = SessionQuery(store).hook_trace("session-query-test", limit=2)
    assert [pair["invoked"]["turn"] for pair in trace] == [2, 3]
    assert [pair["result"]["turn"] for pair in trace] == [2, 3]


def test_empty_session_has_an_empty_trace(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    new_session(store, [])
    assert SessionQuery(store).hook_trace("session-query-test") == []


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "5"])
def test_invalid_limit_is_a_filter_error(tmp_path: pathlib.Path, limit: Any) -> None:
    store = tmp_store(tmp_path)
    new_session(store, [invoked()])
    with pytest.raises(SessionQueryError) as excinfo:
        SessionQuery(store).hook_trace("session-query-test", limit=limit)
    assert excinfo.value.code == INVALID_FILTER


def test_unknown_session_is_reported_as_not_found(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    with pytest.raises(SessionQueryError) as excinfo:
        SessionQuery(store).hook_trace("session-missing")
    assert excinfo.value.code == SESSION_NOT_FOUND
