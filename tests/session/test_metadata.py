"""Session titles and archive flags are plain log records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from deeppyro.session.metadata import (
    ARCHIVE_EVENT,
    TITLE_EVENT,
    derive_title,
    existing_title,
    is_archived,
    preview_text,
    record_archive,
    record_title,
)


@dataclass
class Record:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


class FakeSession:
    def __init__(self) -> None:
        self.records: list[Record] = []

    def append(self, type: str, data: dict[str, Any], **_: Any) -> int:
        self.records.append(Record(type, data))
        return len(self.records)


def test_derive_title_collapses_whitespace_and_truncates() -> None:
    assert derive_title("  hello   world  ") == "hello world"
    assert derive_title("") is None
    assert derive_title("   ") is None
    long = "x" * 200
    title = derive_title(long)
    assert title is not None
    assert len(title) == 24
    assert title.endswith("\u2026")


def test_derive_title_stops_at_the_first_sentence() -> None:
    assert derive_title("fix the sandbox gate. then run the tests") == "fix the sandbox gate"
    assert derive_title("\u4f60\u597d\u3002\u6211\u60f3\u770b\u770b\u4ee3\u7801") == "\u4f60\u597d"
    assert derive_title("upgrade to v0.1.0 please") == "upgrade to v0.1.0 please"
    assert derive_title("first line\nsecond line") == "first line"


def test_derive_title_drops_a_leading_command() -> None:
    assert derive_title("/rename fix the gate") == "fix the gate"
    # A path is not a command: the slash has to introduce a bare word.
    path_title = derive_title("/etc/passwd is world readable") or ""
    assert path_title.startswith("/etc/passwd")
    assert derive_title("/resume") == "/resume"


def test_preview_text_keeps_one_line() -> None:
    assert preview_text("a\nb\nc") == "a b c"
    assert preview_text(None) is None


def test_title_records_last_write_wins() -> None:
    session = FakeSession()
    assert existing_title(session.records) is None
    record_title(session, "first")
    record_title(session, "second", source="command")
    assert [record.type for record in session.records] == [TITLE_EVENT, TITLE_EVENT]
    assert existing_title(session.records) == "second"


def test_record_title_rejects_empty() -> None:
    with pytest.raises(ValueError):
        record_title(FakeSession(), "   ")


def test_archive_records_toggle() -> None:
    session = FakeSession()
    assert is_archived(session.records) is False
    assert record_archive(session, True) is True
    assert is_archived(session.records) is True
    record_archive(session, False)
    assert is_archived(session.records) is False
    assert [record.type for record in session.records] == [ARCHIVE_EVENT, ARCHIVE_EVENT]
