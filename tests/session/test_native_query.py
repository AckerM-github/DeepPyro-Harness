"""Bounded query, search and metadata writes for native schema 1 sessions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.session.native_index import ARCHIVE_EVENT, list_native_sessions
from deeppyro.session.native_query import (
    NATIVE_QUERY_CORRUPT,
    NATIVE_QUERY_INVALID_FILTER,
    NATIVE_QUERY_INVALID_TITLE,
    NATIVE_QUERY_LOCKED,
    NATIVE_QUERY_NOT_FOUND,
    NativeQueryError,
    NativeSessionFilter,
    NativeSessionQuery,
    NativeSessionQueryLike,
    NativeSessionSort,
    archive_native_session,
    get_native_session,
    is_native_session_archived,
    query_native_sessions,
    read_native_events,
    rename_native_session,
    search_native_sessions,
)
from deeppyro.session.schema import EventKind, NativeSession
from tests.session.native_helpers import (
    CREATED_AT,
    SESSION_ID,
    TITLE,
    write_native,
)

OLDER_ID = "session-00000000-0000-0000-0000-000000000001"
NEWER_ID = "session-00000000-0000-0000-0000-000000000002"


def _write(directory: Path, **kwargs: object) -> Path:
    return write_native(directory, **kwargs)


def _by_id(directory: Path) -> dict[str, object]:
    return {info.id: info for info in list_native_sessions(directory, include_archived=True)}


def test_query_protocol_is_runtime_checkable(tmp_path: Path) -> None:
    _write(tmp_path)

    assert isinstance(NativeSessionQuery(tmp_path), NativeSessionQueryLike)


def test_list_sessions_is_newest_first_and_hides_archived(tmp_path: Path) -> None:
    _write(tmp_path, session_id=OLDER_ID, created_at=CREATED_AT)
    _write(tmp_path, session_id=NEWER_ID, created_at=CREATED_AT + 5_000, archived=True)

    query = NativeSessionQuery(tmp_path)

    assert [info.id for info in query.list_sessions()] == [OLDER_ID]
    assert [info.id for info in query.list_sessions(filter=NativeSessionFilter(archived=True))] == [NEWER_ID]
    assert [info.id for info in query.list_sessions(filter=NativeSessionFilter())] == [NEWER_ID, OLDER_ID]


def test_list_sessions_limits_offset_and_sort(tmp_path: Path) -> None:
    _write(tmp_path, session_id=OLDER_ID, created_at=CREATED_AT, title="Zeta work")
    _write(tmp_path, session_id=NEWER_ID, created_at=CREATED_AT + 5_000, title="Alpha work")

    query = NativeSessionQuery(tmp_path)

    assert [info.id for info in query.list_sessions(limit=1)] == [NEWER_ID]
    assert [info.id for info in query.list_sessions(limit=1, offset=1)] == [OLDER_ID]
    assert [info.title for info in query.list_sessions(sort=NativeSessionSort.TITLE)] == [
        "Alpha work",
        "Zeta work",
    ]
    assert [info.id for info in query.list_sessions(sort=NativeSessionSort.CREATED)] == [NEWER_ID, OLDER_ID]


def test_list_sessions_filters_rows(tmp_path: Path) -> None:
    _write(tmp_path, session_id=OLDER_ID, cwd="C:/work/elsewhere", title="Older note")
    _write(tmp_path, session_id=NEWER_ID, dropped_result=True)
    query = NativeSessionQuery(tmp_path)

    assert [info.id for info in query.list_sessions(filter=NativeSessionFilter(text="parser"))] == [NEWER_ID]
    assert [info.id for info in query.list_sessions(filter=NativeSessionFilter(cwd="elsewhere"))] == [
        OLDER_ID
    ]
    assert [
        info.id for info in query.list_sessions(filter=NativeSessionFilter(include_unreadable=False))
    ] == [OLDER_ID]
    assert query.list_sessions(filter=NativeSessionFilter(min_turns=2)) == []
    assert query.list_sessions(filter=NativeSessionFilter(origin="headless")) == []


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"limit": -1}, NATIVE_QUERY_INVALID_FILTER),
        ({"offset": -1}, NATIVE_QUERY_INVALID_FILTER),
        ({"sort": "updated"}, NATIVE_QUERY_INVALID_FILTER),
    ],
)
def test_list_sessions_rejects_invalid_arguments(
    tmp_path: Path, kwargs: dict[str, object], code: str
) -> None:
    _write(tmp_path)
    query = NativeSessionQuery(tmp_path)

    with pytest.raises(NativeQueryError) as caught:
        query.list_sessions(**kwargs)  # type: ignore[arg-type]

    assert caught.value.code == code


def test_filter_rejects_invalid_values() -> None:
    with pytest.raises(NativeQueryError) as caught:
        NativeSessionFilter(min_turns=-1)

    assert caught.value.code == NATIVE_QUERY_INVALID_FILTER


def test_get_returns_archived_rows_and_reports_missing(tmp_path: Path) -> None:
    _write(tmp_path, archived=True)
    query = NativeSessionQuery(tmp_path)

    assert query.get(SESSION_ID).archived is True
    with pytest.raises(NativeQueryError) as caught:
        query.get("session-00000000-0000-0000-0000-000000000009")
    assert caught.value.code == NATIVE_QUERY_NOT_FOUND
    assert caught.value.detail["code"] == NATIVE_QUERY_NOT_FOUND


def test_get_reports_corrupt_logs(tmp_path: Path) -> None:
    _write(tmp_path)
    Path(tmp_path / f"{SESSION_ID}.jsonl").write_text("not json\n", encoding="utf-8")
    query = NativeSessionQuery(tmp_path)

    with pytest.raises(NativeQueryError) as caught:
        query.get(SESSION_ID)

    assert caught.value.code == NATIVE_QUERY_CORRUPT


def test_read_events_filters_kind_role_after_seq_and_limit(tmp_path: Path) -> None:
    _write(tmp_path)
    query = NativeSessionQuery(tmp_path)

    calls = query.read_events(SESSION_ID, kinds=EventKind.TOOL_CALL)
    assert [match.kind for match in calls] == [EventKind.TOOL_CALL.value]
    assert calls[0].role == "tool"
    assert "read_file" in calls[0].preview

    tool_events = query.read_events(SESSION_ID, role="tool")
    assert [match.kind for match in tool_events] == [EventKind.TOOL_CALL.value, EventKind.TOOL_RESULT.value]

    every = query.read_events(SESSION_ID, kinds=None, limit=100)
    tail = query.read_events(SESSION_ID, after_seq=6, limit=100)
    assert [match.seq for match in tail] == [match.seq for match in every if match.seq > 6]

    first_two = query.read_events(SESSION_ID, limit=2)
    assert len(first_two) == 2
    assert first_two[0].seq == 1


def test_read_events_accepts_plugin_event_kinds(tmp_path: Path) -> None:
    _write(tmp_path, archived=True)
    query = NativeSessionQuery(tmp_path)

    matches = query.read_events(SESSION_ID, kinds=ARCHIVE_EVENT)

    assert len(matches) == 1
    assert matches[0].kind == ARCHIVE_EVENT


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"kinds": "nope.event"}, NATIVE_QUERY_INVALID_FILTER),
        ({"role": "wizard"}, NATIVE_QUERY_INVALID_FILTER),
        ({"after_seq": -2}, NATIVE_QUERY_INVALID_FILTER),
        ({"limit": -1}, NATIVE_QUERY_INVALID_FILTER),
    ],
)
def test_read_events_rejects_invalid_arguments(tmp_path: Path, kwargs: dict[str, object], code: str) -> None:
    _write(tmp_path)
    query = NativeSessionQuery(tmp_path)

    with pytest.raises(NativeQueryError) as caught:
        query.read_events(SESSION_ID, **kwargs)  # type: ignore[arg-type]

    assert caught.value.code == code


def test_search_matches_title_preview_and_event_text(tmp_path: Path) -> None:
    _write(tmp_path, assistant_text="The parser now reads bytes.")
    query = NativeSessionQuery(tmp_path)

    title_hits = query.search("parser")
    assert len(title_hits) == 1
    assert "title" in title_hits[0].matched
    assert title_hits[0].score > 0

    preview_hits = query.search("Please read")
    assert preview_hits[0].matched == ("preview",)

    event_hits = query.search("reads bytes")
    assert event_hits[0].matched == ()
    assert [match.kind for match in event_hits[0].events] == [EventKind.ASSISTANT_MESSAGE.value]


def test_search_filters_by_role_and_limit(tmp_path: Path) -> None:
    _write(tmp_path)
    query = NativeSessionQuery(tmp_path)

    user_hits = query.search("read", role="user")
    assert user_hits[0].events[0].kind == EventKind.USER_MESSAGE.value

    limited = query.search("read", limit=1)
    assert len(limited) == 1


def test_search_skips_corrupt_sessions_and_respects_event_budget(tmp_path: Path) -> None:
    _write(tmp_path)
    Path(tmp_path / "broken.jsonl").write_text("not json\n", encoding="utf-8")
    query = NativeSessionQuery(tmp_path)

    assert len(query.search("parser")) == 1
    starved = NativeSessionQuery(tmp_path, max_events=1)
    assert starved.search("file contents") == []


def test_search_rejects_blank_needle(tmp_path: Path) -> None:
    _write(tmp_path)
    query = NativeSessionQuery(tmp_path)

    with pytest.raises(NativeQueryError) as caught:
        query.search("   ")

    assert caught.value.code == NATIVE_QUERY_INVALID_FILTER


def test_search_single_session_can_be_bounded(tmp_path: Path) -> None:
    _write(tmp_path)
    _write(tmp_path, session_id=NEWER_ID, created_at=CREATED_AT + 10)
    bounded = NativeSessionQuery(tmp_path, max_sessions=1)

    hits = bounded.search("parser")

    assert len(hits) == 1
    assert hits[0].info.id == NEWER_ID


def test_rename_appends_title_event_and_refreshes_row(tmp_path: Path) -> None:
    _write(tmp_path, title=None)

    info = rename_native_session(SESSION_ID, "  Renamed   session ", root=tmp_path)

    assert info.title == "Renamed session"
    assert info.event_count == 9
    assert rename_native_session(SESSION_ID, "LLM title", source="llm", root=tmp_path).title == "LLM title"
    events = read_native_events(SESSION_ID, kinds=EventKind.SESSION_TITLE, root=tmp_path)
    assert [match.kind for match in events] == [EventKind.SESSION_TITLE.value, EventKind.SESSION_TITLE.value]


@pytest.mark.parametrize(
    ("title", "source", "code"),
    [
        ("", "user", NATIVE_QUERY_INVALID_TITLE),
        ("   ", "user", NATIVE_QUERY_INVALID_TITLE),
        ("quiet", "robot", NATIVE_QUERY_INVALID_FILTER),
    ],
)
def test_rename_rejects_invalid_inputs(tmp_path: Path, title: str, source: str, code: str) -> None:
    _write(tmp_path)

    with pytest.raises(NativeQueryError) as caught:
        rename_native_session(SESSION_ID, title, source=source, root=tmp_path)

    assert caught.value.code == code


def test_metadata_writes_report_missing_and_locked_sessions(tmp_path: Path) -> None:
    path = _write(tmp_path)

    with pytest.raises(NativeQueryError) as missing:
        archive_native_session("session-00000000-0000-0000-0000-000000000009", root=tmp_path)
    assert missing.value.code == NATIVE_QUERY_NOT_FOUND

    session = NativeSession.open(path)
    try:
        with pytest.raises(NativeQueryError) as locked:
            rename_native_session(SESSION_ID, "locked out", root=tmp_path)
    finally:
        session.close()
    assert locked.value.code == NATIVE_QUERY_LOCKED


def test_archive_marker_toggles_and_hides_from_listing(tmp_path: Path) -> None:
    _write(tmp_path)
    query = NativeSessionQuery(tmp_path)

    archived = archive_native_session(SESSION_ID, root=tmp_path)
    assert archived.archived is True
    assert is_native_session_archived(SESSION_ID, root=tmp_path) is True
    assert query.list_sessions() == []

    active = archive_native_session(SESSION_ID, archived=False, root=tmp_path)
    assert active.archived is False
    assert is_native_session_archived(SESSION_ID, root=tmp_path) is False
    assert [info.id for info in query.list_sessions()] == [SESSION_ID]


def test_module_level_wrappers(tmp_path: Path) -> None:
    _write(tmp_path)

    assert [info.id for info in query_native_sessions(root=tmp_path)] == [SESSION_ID]
    assert get_native_session(SESSION_ID, root=tmp_path).title == TITLE
    assert len(search_native_sessions("parser", root=tmp_path)) == 1
    assert (
        read_native_events(SESSION_ID, kinds=EventKind.TURN_STARTED, root=tmp_path)[0].kind == "turn.started"
    )
    assert (
        json.loads(json.dumps({"title": get_native_session(SESSION_ID, root=tmp_path).title}))["title"]
        == TITLE
    )
