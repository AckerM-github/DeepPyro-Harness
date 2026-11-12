"""Splash screen and pixel wordmark tests."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Static

from deeppyro.tui.screens import splash as splash_module
from deeppyro.tui.screens.splash import SplashScreen
from deeppyro.tui.wordmark import (
    ART_WIDTH,
    COMPACT_WORD,
    compact_wordmark_line,
    wordmark_lines,
)


class SplashHost(App[None]):
    """Minimal app that shows only the splash screen."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self._splash_kwargs = kwargs

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(SplashScreen(**self._splash_kwargs))


def test_wordmark_rows_are_padded_and_colored() -> None:
    rows = wordmark_lines()
    assert len(rows) == 13
    assert {len(row.plain) for row in rows} == {ART_WIDTH}
    first = rows[0]
    assert first.plain.strip().startswith("██")
    styles = [str(span.style) for span in first.spans if "#" in str(span.style)]
    assert any("bold" in style for style in styles)
    # A gradient, not one flat color per row.
    assert len(set(styles)) > 5


def test_compact_wordmark_is_one_plain_line() -> None:
    line = compact_wordmark_line()
    assert line.plain == COMPACT_WORD
    assert line.spans


async def test_splash_reveals_rows_and_any_key_dismisses() -> None:
    app = SplashHost(version="0.1.0", cwd="D:/work", provider="fake", model="fake-1")
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, SplashScreen)
        assert screen.compact is False
        assert "python core" not in screen.visible_text()

        await pilot.pause(1.4)
        full = screen.visible_text()
        assert screen.line_count == len(full.splitlines())
        assert "python core · terminal UI" in full
        assert "fake · fake-1" in full
        assert "press any key to continue" in full

        await pilot.press("space")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, SplashScreen)


async def test_splash_uses_compact_wordmark_on_small_terminals() -> None:
    app = SplashHost(version="0.1.0", cwd="D:/work")
    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, SplashScreen)
        assert screen.compact is True

        await pilot.pause(0.8)
        text = screen.visible_text()
        assert COMPACT_WORD in text
        assert "██" not in text


async def test_set_runtime_fills_the_boot_line() -> None:
    app = SplashHost(version="0.1.0", cwd="D:/work")
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.05)
        screen = app.screen
        assert isinstance(screen, SplashScreen)
        assert "loading runtime…" in screen.full_text()

        screen.set_runtime("local", "deepseek-v4", cwd="D:/code/deeppyro")
        await pilot.pause(0.1)
        text = screen.full_text()
        assert "local · deepseek-v4" in text
        assert "D:/code/deeppyro" in text


async def test_splash_dismisses_itself_after_the_hold(monkeypatch: Any) -> None:
    monkeypatch.setattr(splash_module, "REVEAL_INTERVAL", 0.005)
    monkeypatch.setattr(splash_module, "HOLD_SECONDS", 0.02)
    app = SplashHost(version="0.1.0", cwd="D:/work")
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.02)
        assert isinstance(app.screen, SplashScreen)
        for _ in range(40):
            await pilot.pause(0.05)
            if not isinstance(app.screen, SplashScreen):
                break
        assert not isinstance(app.screen, SplashScreen)
