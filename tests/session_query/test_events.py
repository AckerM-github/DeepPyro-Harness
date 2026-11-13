"""Event listings, windows, and literal filters."""

from __future__ import annotations

import itertools
import pathlib

import pytest

from deeppyro.session import SURFACE_EVENTS, read_session_file
from deeppyro.session_query import (
    EVENT_NOT_FOUND,
    INVALID_FILTER,
    SESSION_NOT_FOUND,
    EventFilter,
    EventWindow,
    SessionQuery,
    SessionQueryError,
)
from tests.session.support import (
    assistant_message,
    event,
    session_files,
    surface,
    tool_call,
    user_message,
)
from tests.session_query.support import new_session, tmp_store


def _fixture_ids() -> list[str]:
    return [path.name[: -len(".jsonl")] for path in session_files()]


def test_list_events_matches_raw_records(query: SessionQuery) -> None:
    for session_id in _fixture_ids():
        records = read_session_file(next(path for path in session_files() if path.stem == session_id)).records
        events = query.list_events(session_id, limit=100_000)
        assert events == [record for record in records if record.seq > 0]
        assert all(left.seq < right.seq for left, right in itertools.pairwise(events))


def test_list_events_type_and_surface_filters(query: SessionQuery) -> None:
    session_id = next(iter(_fixture_ids()))
    events = query.list_events(session_id, limit=100_000)
    types = tuple(sorted({record.type for record in events}))
    picked = types[0]
    only = query.list_events(session_id, types=[picked], limit=100_000)
    assert [record.seq for record in only] == [record.seq for record in events if record.type == picked]
    assert query.list_events(session_id, types=[], limit=100_000) == []
    surface_events = query.list_events(session_id, surface=True, limit=100_000)
    assert surface_events and all(record.type in SURFACE_EVENTS for record in surface_events)
    non_surface = query.list_events(session_id, surface=False, limit=100_000)
    assert non_surface and all(record.type not in SURFACE_EVENTS for record in non_surface)
    assert len(surface_events) + len(non_surface) == len(events)


def test_list_events_after_seq_is_exclusive(query: SessionQuery) -> None:
    events = query.list_events(next(iter(_fixture_ids())), limit=100_000)
    pivot = events[len(events) // 2].seq
    after = query.list_events(next(iter(_fixture_ids())), after_seq=pivot, limit=100_000)
    assert after == [record for record in events if record.seq > pivot]


def test_list_events_limit(query: SessionQuery) -> None:
    session_id = next(iter(_fixture_ids()))
    assert len(query.list_events(session_id)) <= 500
    assert len(query.list_events(session_id, limit=1)) == 1
    with pytest.raises(SessionQueryError) as excinfo:
        query.list_events(session_id, limit=0)
    assert excinfo.value.code == INVALID_FILTER


def test_list_events_rejects_bad_arguments(query: SessionQuery) -> None:
    session_id = next(iter(_fixture_ids()))
    cases = [
        {"after_seq": -1},
        {"limit": -3},
        {"types": ["bogus/type"]},
        {"types": "user/message"},
        {"surface": "yes"},
    ]
    for kwargs in cases:
        with pytest.raises(SessionQueryError) as excinfo:
            query.list_events(session_id, **kwargs)
        assert excinfo.value.code == INVALID_FILTER, kwargs
    with pytest.raises(SessionQueryError) as excinfo:
        query.list_events("session-missing")
    assert excinfo.value.code == SESSION_NOT_FOUND


def test_read_event_windows_and_clamping(query: SessionQuery) -> None:
    session_id = next(iter(_fixture_ids()))
    events = query.list_events(session_id, limit=100_000)
    assert len(events) > 6
    middle = events[len(events) // 2]
    window = query.read_event(session_id, middle.seq, before=2, after=3)
    assert isinstance(window, EventWindow)
    assert window.session_id == session_id
    assert window.seq == middle.seq
    assert window.event == middle
    expected = query.list_events(session_id, after_seq=0, limit=100_000)
    index = expected.index(middle)
    assert window.before == expected[max(0, index - 2) : index]
    assert window.after == expected[index + 1 : index + 4]
    # First and last persisted events clamp at the edges without error.
    raw = read_session_file(next(path for path in session_files() if path.stem == session_id)).records
    assert query.read_event(session_id, raw[0].seq, before=5).before == []
    assert query.read_event(session_id, raw[-1].seq, after=5).after == []


def test_read_event_clamps_to_read_window_max(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    events = [event("turn/start", 0, {"turn": 1})]
    events += [event("step/start", seq, {"turn": 1, "step": seq}) for seq in range(1, 10)]
    new_session(store, events)
    query = SessionQuery(store, read_window_max=3)
    window = query.read_event("session-query-test", 5, before=100, after=100)
    assert len(window.before) == 3
    assert len(window.after) == 3


def test_read_event_missing_and_invalid(query: SessionQuery) -> None:
    session_id = next(iter(_fixture_ids()))
    with pytest.raises(SessionQueryError) as excinfo:
        query.read_event(session_id, 10**9)
    assert excinfo.value.code == EVENT_NOT_FOUND
    with pytest.raises(SessionQueryError) as excinfo:
        query.read_event(session_id, -1)
    assert excinfo.value.code == INVALID_FILTER
    with pytest.raises(SessionQueryError) as excinfo:
        query.read_event(session_id, 0, before=-1)
    assert excinfo.value.code == INVALID_FILTER


def test_filter_events_literal_case_and_whitespace(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    events = [
        surface("user/message", 0, user_message("msg_u", "  Hello   World  ")),
        surface("assistant/message", 1, assistant_message("msg_a", "bye")),
        event("tool/call", 2, tool_call("call_1", "read")),
    ]
    new_session(store, events)
    query = SessionQuery(store)
    matches = query.filter_events("session-query-test", EventFilter(text="hello world"))
    assert [record.seq for record in matches] == [0]
    assert query.filter_events("session-query-test", EventFilter(text="  ")) is not None
    assert len(query.filter_events("session-query-test", EventFilter(text="  "))) == 3


def test_filter_events_ranges_types_and_surface(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    events = [
        surface("user/message", 0, user_message("msg_u", "first")),
        surface("assistant/message", 1, assistant_message("msg_a", "second")),
        event("tool/call", 2, tool_call("call_1", "read")),
    ]
    new_session(store, events)
    query = SessionQuery(store)
    assert [r.seq for r in query.filter_events("session-query-test", EventFilter(seq_from=0, seq_to=0))] == [
        0
    ]
    assert [r.seq for r in query.filter_events("session-query-test", EventFilter(seq_from=1, seq_to=2))] == [
        1,
        2,
    ]
    assert [
        r.seq
        for r in query.filter_events(
            "session-query-test", EventFilter(types=("user/message", "assistant/message"))
        )
    ] == [0, 1]
    assert [r.seq for r in query.filter_events("session-query-test", EventFilter(surface_only=True))] == [
        0,
        1,
    ]
    assert [r.seq for r in query.filter_events("session-query-test", EventFilter(surface_only=False))] == [2]
    assert query.filter_events("session-query-test", EventFilter(types=())) == []
    assert [r.seq for r in query.filter_events("session-query-test", EventFilter(text="read"))] == [2]


def test_event_filter_validation() -> None:
    for kwargs in (
        {"seq_from": -1},
        {"seq_to": -2},
        {"seq_from": 5, "seq_to": 1},
        {"types": "user/message"},
        {"types": ["bogus/type"]},
        {"surface_only": "yes"},
        {"text": 5},
    ):
        with pytest.raises(SessionQueryError) as excinfo:
            EventFilter(**kwargs)
        assert excinfo.value.code == INVALID_FILTER, kwargs


def test_filter_events_unknown_session(query: SessionQuery) -> None:
    with pytest.raises(SessionQueryError) as excinfo:
        query.filter_events("session-missing", EventFilter())
    assert excinfo.value.code == SESSION_NOT_FOUND


def test_filter_events_matches_session_title(query: SessionQuery, manifest: dict) -> None:
    titled = next(entry for entry in manifest["sessions"] if entry.get("types", {}).get("session/title"))
    title_events = query.list_events(titled["id"], types=["session/title"], limit=100)
    title = title_events[0].data["title"]
    matches = query.filter_events(titled["id"], EventFilter(text=title))
    assert title_events[0].seq in [record.seq for record in matches]
