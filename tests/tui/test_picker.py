"""Tests for the generic PickerScreen modal."""

from __future__ import annotations

from typing import Any

from textual.app import App
from textual.widgets import Input, OptionList, Static

from deeppyro.tui.screens.picker import PickerItem, PickerScreen

ITEMS: list[PickerItem] = [
    ("s-1", "Alpha session", "today"),
    ("s-2", "Beta session", "yesterday"),
    ("s-3", "Gamma session", "last week"),
]


class HostApp(App[None]):
    """Bare application used to open the modal and collect its result."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[str | None] = []


def _text(widget: Static) -> str:
    content: Any = widget.content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


def _options(screen: PickerScreen) -> OptionList:
    return screen.query_one("#picker-list", OptionList)


async def test_picker_shows_items_and_focuses_the_filter() -> None:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PickerScreen("Resume a session", ITEMS), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        assert app.focused is not None and app.focused.id == "picker-filter"
        assert _text(screen.query_one("#picker-title", Static)) == "Resume a session"
        option_list = _options(screen)
        assert option_list.option_count == 3
        assert [option_list.get_option_at_index(index).id for index in range(3)] == [
            "pick-0",
            "pick-1",
            "pick-2",
        ]


async def test_filter_narrows_and_enter_returns_the_value() -> None:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PickerScreen("Resume a session", ITEMS), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        screen.query_one("#picker-filter", Input).value = "beta"
        await pilot.pause()
        assert _options(screen).option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.results == ["s-2"]
        assert len(app.screen_stack) == 1


async def test_filter_also_matches_notes_and_values() -> None:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PickerScreen("Resume a session", ITEMS), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        screen.query_one("#picker-filter", Input).value = "week"
        await pilot.pause()
        assert _options(screen).option_count == 1
        screen.query_one("#picker-filter", Input).value = "s-3"
        await pilot.pause()
        assert _options(screen).option_count == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.results == ["s-3"]


async def test_arrow_keys_move_the_highlight() -> None:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PickerScreen("Pick a model", ITEMS), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        option_list = _options(screen)
        await pilot.press("down")
        await pilot.pause()
        assert option_list.highlighted == 1
        await pilot.press("down")
        await pilot.pause()
        assert option_list.highlighted == 2
        await pilot.press("up")
        await pilot.pause()
        assert option_list.highlighted == 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.results == ["s-2"]


async def test_escape_cancels_with_none() -> None:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PickerScreen("Resume a session", ITEMS), callback=app.results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
        assert len(app.screen_stack) == 1


async def test_no_matches_shows_the_empty_hint_and_enter_cancels() -> None:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PickerScreen("Resume a session", ITEMS), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        screen.query_one("#picker-filter", Input).value = "nothing matches"
        await pilot.pause()
        assert _options(screen).option_count == 0
        assert screen.query_one("#picker-empty", Static).display is True
        await pilot.press("enter")
        await pilot.pause()
        assert app.results == [None]


async def test_option_list_select_returns_the_value() -> None:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PickerScreen("Resume a session", ITEMS), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        _options(screen).action_select()
        await pilot.pause()
        assert app.results == ["s-1"]


async def test_empty_item_list_returns_none() -> None:
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(PickerScreen("Resume a session", []), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickerScreen)
        assert _options(screen).option_count == 0
        assert screen.query_one("#picker-empty", Static).display is True
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
