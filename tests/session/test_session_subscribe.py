"""Process-local subscription to the durable log."""

from __future__ import annotations

import pathlib
from typing import Any

from deeppyro.session.store import SessionStore


def _record(session: Any, marker: str) -> None:
    session.append("session/title", {"title": marker, "source": {"kind": "user"}})


def test_subscribers_see_type_data_and_seq_in_order(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(str(tmp_path / "ws"))
    seen: list[tuple[str, dict[str, Any], int]] = []

    session.subscribe(lambda event_type, data, seq: seen.append((event_type, data, seq)))
    _record(session, "one")
    _record(session, "two")

    assert [event[0] for event in seen] == ["session/title", "session/title"]
    assert [event[1]["title"] for event in seen] == ["one", "two"]
    assert [event[2] for event in seen] == [0, 1]
    session.close()


def test_unsubscribe_stops_delivery(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(str(tmp_path / "ws"))
    seen: list[str] = []

    def callback(event_type: str, data: dict[str, Any], seq: int) -> None:
        seen.append(data["title"])

    dispose = session.subscribe(callback)
    _record(session, "one")
    dispose()
    dispose()
    _record(session, "two")

    assert seen == ["one"]
    session.close()


def test_a_raising_subscriber_never_breaks_the_log(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(str(tmp_path / "ws"))
    seen: list[str] = []

    def broken(event_type: str, data: dict[str, Any], seq: int) -> None:
        raise RuntimeError("boom")

    session.subscribe(broken)
    session.subscribe(lambda event_type, data, seq: seen.append(data["title"]))

    _record(session, "still-durable")

    assert seen == ["still-durable"]
    assert [record.type for record in session.records][-1] == "session/title"
    session.close()


def test_a_session_without_subscribers_appends_normally(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(str(tmp_path / "ws"))

    _record(session, "plain")

    assert session.records[-1].data["title"] == "plain"
    assert session._subscriptions == []
    session.close()
