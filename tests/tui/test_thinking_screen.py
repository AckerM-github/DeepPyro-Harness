"""Tests for the full-screen thinking viewer."""

from __future__ import annotations

from typing import Any

from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Static

from deeppyro.tui.history import ReasoningStep, TurnReasoning
from deeppyro.tui.screens.thinking import (
    DEFAULT_TITLE,
    EMPTY_TEXT,
    STEP_EMPTY_TEXT,
    ThinkingScreen,
)


class HostApp(App[None]):
    """Bare application used to open the modal and collect its result."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[None] = []


def _text(widget: Static) -> str:
    content: Any = widget.content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


def _turn(*steps: tuple[int, str], turn: int = 1, finished: bool = True) -> TurnReasoning:
    return TurnReasoning(
        turn=turn,
        steps=[ReasoningStep(step=step, text=text) for step, text in steps],
        finished=finished,
    )


async def test_renders_turn_step_headings_and_full_text() -> None:
    turn = _turn((1, "First step text!"), (2, "Second step text."))
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen([turn]), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThinkingScreen)
        assert _text(screen.query_one("#thinking-title", Static)) == DEFAULT_TITLE
        assert [_text(widget) for widget in screen.query(".thinking-turn")] == ["Turn 1"]
        assert [_text(widget) for widget in screen.query(".thinking-step")] == ["Step 1", "Step 2"]
        assert [_text(widget) for widget in screen.query(".thinking-step-text")] == [
            "First step text!",
            "Second step text.",
        ]


async def test_accepts_a_single_turn_and_a_custom_title() -> None:
    turn = _turn((1, "Only step."))
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen(turn, title="Thinking trace"), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThinkingScreen)
        assert screen.turns == [turn]
        assert screen.heading == "Thinking trace"
        assert _text(screen.query_one("#thinking-title", Static)) == "Thinking trace"


async def test_multiple_turns_render_ordered_headings() -> None:
    first = _turn((1, "one"), (2, "two"), turn=1)
    second = _turn((1, "three"), turn=2)
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen([first, second]), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThinkingScreen)
        assert [_text(widget) for widget in screen.query(".thinking-turn")] == ["Turn 1", "Turn 2"]
        assert [_text(widget) for widget in screen.query(".thinking-step")] == [
            "Step 1",
            "Step 2",
            "Step 1",
        ]
        assert [_text(widget) for widget in screen.query(".thinking-step-text")] == [
            "one",
            "two",
            "three",
        ]


async def test_multiline_step_text_is_preserved() -> None:
    body = "Line one.\nLine two.\n\nLine four."
    turn = _turn((1, body))
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen(turn), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThinkingScreen)
        assert _text(screen.query_one(".thinking-step-text", Static)) == body


async def test_bracketed_text_is_not_parsed_as_markup() -> None:
    raw = "[bold]not markup[/bold] and [dim]still text[/dim]"
    turn = _turn((1, raw))
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen(turn, title="[Thinking]"), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThinkingScreen)
        assert _text(screen.query_one(".thinking-step-text", Static)) == raw
        assert _text(screen.query_one("#thinking-title", Static)) == "[Thinking]"


async def test_escape_closes_and_returns_none() -> None:
    turn = _turn((1, "Some reasoning!"))
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen(turn), callback=app.results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
        assert len(app.screen_stack) == 1


async def test_empty_turns_show_the_empty_state() -> None:
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen(), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThinkingScreen)
        assert _text(screen.query_one("#thinking-empty", Static)) == EMPTY_TEXT
        assert list(screen.query(".thinking-turn")) == []
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]


async def test_turn_without_steps_shows_the_step_placeholder() -> None:
    turn = TurnReasoning(turn=5)
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen(turn), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThinkingScreen)
        assert [_text(widget) for widget in screen.query(".thinking-turn")] == ["Turn 5"]
        assert list(screen.query(".thinking-step")) == []
        assert _text(screen.query_one(".thinking-step-text", Static)) == STEP_EMPTY_TEXT


async def test_long_reasoning_is_scrollable() -> None:
    steps = tuple((step, "Reasoning line " + str(step)) for step in range(1, 31))
    turn = _turn(*steps)
    app = HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(ThinkingScreen(turn), callback=app.results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ThinkingScreen)
        body = screen.query_one("#thinking-body", VerticalScroll)
        assert body.max_scroll_y > 0
        assert app.focused is body


def test_screen_does_not_shadow_textual_render() -> None:
    assert "_render" not in ThinkingScreen.__dict__
