"""SessionInfo carries the facts a session list needs."""

from __future__ import annotations

import pathlib

from deeppyro.core.ids import new_message_id
from deeppyro.core.wire import Message, TextPart
from deeppyro.session.metadata import record_archive, record_title
from deeppyro.session.store import SessionStore


def _user(text: str) -> Message:
    return Message(id=new_message_id(), role="user", content=[TextPart(text=text)], source={"kind": "user"})


def test_list_derives_a_title_and_preview_from_the_first_user_message(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(str(tmp_path / "ws"))
    session.append("user/message", _user("please refactor the sandbox gate").model_dump(exclude_none=True))
    info = store.list()[0]
    assert info.title == "please refactor the san\u2026"
    assert info.preview == "please refactor the sandbox gate"
    assert info.updatedAt >= info.createdAt
    session.close()


def test_explicit_title_wins_over_the_derived_one(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(str(tmp_path / "ws"))
    session.append("user/message", _user("hello there").model_dump(exclude_none=True))
    record_title(session, "renamed by hand")
    assert store.list()[0].title == "renamed by hand"
    session.close()


def test_archived_sessions_are_hidden_unless_requested(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(str(tmp_path / "ws"))
    session.append("user/message", _user("archive me").model_dump(exclude_none=True))
    record_archive(session, True)

    assert store.list() == []
    everything = store.list(include_archived=True)
    assert [info.id for info in everything] == [session.session_id]
    assert everything[0].archived is True

    record_archive(session, False)
    assert [info.id for info in store.list()] == [session.session_id]
    session.close()


def test_sessions_without_user_messages_stay_untitled(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path)
    session = store.create(str(tmp_path / "ws"))
    info = store.list()[0]
    assert info.title is None
    assert info.preview is None
    assert info.turns == 0
    session.close()
