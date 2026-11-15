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
    assert len(title) == 60
    assert title.endswith("\u2026")


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
