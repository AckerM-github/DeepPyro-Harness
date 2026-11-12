"""Tests for the multiline PromptArea composer, history and completion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult

from deeppyro.tui.commands import COMMANDS
from deeppyro.tui.widgets.completion import MAX_VISIBLE_OPTIONS, CompletionPopup
from deeppyro.tui.widgets.history import (
    HISTORY_PATH_ENV,
    MAX_HISTORY,
    History,
    default_history_path,
)
from deeppyro.tui.widgets.prompt_area import (
    BORDER_HEIGHT,
    MAX_VISIBLE_LINES,
    PromptArea,
)


class PromptHost(App[None]):
    """Minimal app that mounts a PromptArea and records submissions."""

    def __init__(self, history_path: Path) -> None:
        super().__init__()
        self._history_path = history_path
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield PromptArea(history_path=self._history_path)

    def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        self.submitted.append(event.text)


def _history_path(tmp_path: Path) -> Path:
    return tmp_path / "history.jsonl"


def _popup(app: App[Any]) -> CompletionPopup:
    return app.screen.query_one(CompletionPopup)


async def test_enter_submits_and_clears_the_prompt(tmp_path: Path) -> None:
    path = _history_path(tmp_path)
    app = PromptHost(path)
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["hi"]
        assert prompt.text == ""
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["text"] == "hi"


async def test_enter_on_blank_prompt_is_ignored(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        await pilot.press("enter")
        await pilot.press("space", "space", "space")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == []
        assert prompt.text == "   "


async def test_shift_enter_inserts_a_newline_and_grows(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        await pilot.press("a")
        await pilot.press("shift+enter")
        await pilot.press("b")
        await pilot.pause()
        assert prompt.text == "a\nb"
        assert prompt.region.height == 2 + BORDER_HEIGHT
        assert app.submitted == []


async def test_height_caps_at_eight_visible_lines(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "\n".join("line " + str(index) for index in range(3))
        await pilot.pause()
        assert prompt.wrapped_document.height == 3
        assert prompt.region.height == 3 + BORDER_HEIGHT
        prompt.text = "\n".join("line " + str(index) for index in range(15))
        await pilot.pause()
        assert prompt.wrapped_document.height == 15
        assert prompt.region.height == MAX_VISIBLE_LINES + BORDER_HEIGHT


async def test_history_recall_restores_the_draft(tmp_path: Path) -> None:
    path = _history_path(tmp_path)
    seed = History(path=path)
    seed.append("first")
    seed.append("second")
    app = PromptHost(path)
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "draft"
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert prompt.text == "second"
        await pilot.press("up")
        await pilot.pause()
        assert prompt.text == "first"
        await pilot.press("down")
        await pilot.pause()
        assert prompt.text == "second"
        await pilot.press("down")
        await pilot.pause()
        assert prompt.text == "draft"


async def test_history_survives_a_restart(tmp_path: Path) -> None:
    path = _history_path(tmp_path)
    first_app = PromptHost(path)
    async with first_app.run_test(size=(80, 24)) as pilot:
        prompt = first_app.query_one(PromptArea)
        prompt.focus()
        await pilot.press("h", "e", "l", "l", "o")
        await pilot.press("enter")
        await pilot.pause()
        assert first_app.submitted == ["hello"]

    second_app = PromptHost(path)
    async with second_app.run_test(size=(80, 24)) as pilot:
        prompt = second_app.query_one(PromptArea)
        prompt.focus()
        await pilot.press("up")
        await pilot.pause()
        assert prompt.text == "hello"


async def test_history_recall_does_not_reopen_completion(tmp_path: Path) -> None:
    path = _history_path(tmp_path)
    seed = History(path=path)
    seed.append("/help")
    app = PromptHost(path)
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        await pilot.press("up")
        await pilot.pause()
        assert prompt.text == "/help"
        assert _popup(app).is_open is False


def test_history_keeps_only_the_latest_entries(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    history = History(path=path)
    for index in range(MAX_HISTORY + 7):
        history.append("prompt-" + str(index))
    assert len(history.entries) == MAX_HISTORY
    assert history.entries[0] == "prompt-7"
    assert history.entries[-1] == "prompt-" + str(MAX_HISTORY + 6)
    reloaded = History(path=path)
    reloaded.load()
    assert reloaded.entries == history.entries


def test_history_skips_blank_and_consecutive_duplicates(tmp_path: Path) -> None:
    history = History(path=tmp_path / "history.jsonl")
    history.append("same")
    history.append("same")
    history.append("   ")
    history.append("other")
    assert history.entries == ["same", "other"]


def test_history_load_tolerates_mixed_lines(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"text": "hello", "at": "2026-01-01T00:00:00+00:00"}\n\n123\nplain prompt\n{"other": 1}\n',
        encoding="utf-8",
    )
    history = History(path=path)
    history.load()
    assert history.entries == ["hello", "plain prompt"]


def test_history_save_failure_is_silent(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    history = History(path=blocker / "history.jsonl")
    history.append("still works")
    assert history.entries == ["still works"]


def test_default_history_path_honours_the_environment(tmp_path: Path, monkeypatch: Any) -> None:
    override = tmp_path / "custom" / "history.jsonl"
    monkeypatch.setenv(HISTORY_PATH_ENV, str(override))
    assert default_history_path() == override
    monkeypatch.delenv(HISTORY_PATH_ENV)
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    assert default_history_path() == tmp_path / "home" / "history.jsonl"


async def test_slash_opens_the_completion_popup(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "/"
        await pilot.pause()
        popup = _popup(app)
        assert popup.is_open is True
        assert popup.option_count == len(COMMANDS)
        assert popup.highlighted == 0
        assert popup.region.height == MAX_VISIBLE_OPTIONS + BORDER_HEIGHT


async def test_completion_filters_while_typing(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "/he"
        await pilot.pause()
        popup = _popup(app)
        assert popup.is_open is True
        assert popup.option_count == 1
        highlighted = popup.highlighted_option
        assert highlighted is not None and highlighted.id == "help"


async def test_completion_up_down_moves_the_highlight(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "/"
        await pilot.pause()
        popup = _popup(app)
        await pilot.press("down")
        await pilot.pause()
        assert popup.highlighted == 1
        await pilot.press("up")
        await pilot.pause()
        assert popup.highlighted == 0
        await pilot.press("up")
        await pilot.pause()
        assert popup.highlighted == popup.option_count - 1


async def test_tab_accepts_the_highlighted_command(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "/"
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert prompt.text == "/help "
        assert _popup(app).is_open is False
        assert app.submitted == []


async def test_enter_accepts_completion_instead_of_submitting(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "/"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == []
        assert prompt.text == "/help "
        assert _popup(app).is_open is False


async def test_escape_closes_the_completion_popup(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "/"
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert _popup(app).is_open is False
        assert prompt.text == "/"


async def test_exact_command_hides_the_popup(tmp_path: Path) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "/help"
        await pilot.pause()
        assert _popup(app).is_open is False
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["/help"]
        assert prompt.text == ""


async def test_completion_is_disabled_for_unknown_names_and_arguments(
    tmp_path: Path,
) -> None:
    app = PromptHost(_history_path(tmp_path))
    async with app.run_test(size=(80, 24)) as pilot:
        prompt = app.query_one(PromptArea)
        prompt.focus()
        prompt.text = "/zzz"
        await pilot.pause()
        assert _popup(app).is_open is False
        prompt.text = "/model fake-1"
        await pilot.pause()
        assert _popup(app).is_open is False
        prompt.text = "/he\nsecond line"
        await pilot.pause()
        assert _popup(app).is_open is False
