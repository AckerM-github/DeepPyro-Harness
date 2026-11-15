"""The session list must be readable without opening a session."""

from __future__ import annotations

from types import SimpleNamespace

from deeppyro.tui.session_view import listing_lines, matches, picker_items, relative_time

NOW = 1_700_000_000.0


def _info(**overrides: object) -> SimpleNamespace:
    values = {
        "id": "session-3c1d2b4a-0000-4000-8000-000000000000",
        "title": "refactor the sandbox gate",
        "preview": "please refactor the sandbox gate",
        "cwd": "C:/work/deeppyro",
        "createdAt": int(NOW * 1000) - 90_000,
        "updatedAt": int(NOW * 1000) - 30_000,
        "turns": 4,
        "archived": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_relative_time_buckets() -> None:
    assert relative_time(NOW, now=NOW) == "just now"
    assert relative_time(NOW - 90, now=NOW) == "1m ago"
    assert relative_time(NOW - 3 * 3600, now=NOW) == "3h ago"
    assert relative_time(NOW - 2 * 86400, now=NOW) == "2d ago"
    assert relative_time(None) == "-"


def test_relative_time_accepts_milliseconds() -> None:
    assert relative_time(int((NOW - 600) * 1000), now=NOW) == "10m ago"


def test_picker_items_carry_title_age_turns_and_preview() -> None:
    items = picker_items([_info()])
    value, label, note = items[0]
    assert value.startswith("session-3c1d2b4a")
    assert "refactor the sandbox gate" in label
    assert "just now" not in label
    assert "4t" in label
    assert "please refactor" in note
    assert "C:/work/deeppyro" in note


def test_listing_lines_group_by_workspace() -> None:
    lines = listing_lines([_info(), _info(id="session-aaaabbbb", cwd="C:/other")], now=NOW)
    assert lines[0] == "C:/work/deeppyro"
    assert lines[1].startswith("   1. 3c1d2b4a")
    assert lines[2] == "C:/other"


def test_matches_searches_id_title_preview_and_cwd() -> None:
    info = _info()
    assert matches(info, "3c1d2b4a")
    assert matches(info, "sandbox")
    assert matches(info, "deeppyro")
    assert matches(info, "")
    assert not matches(info, "nothing-like-this")
