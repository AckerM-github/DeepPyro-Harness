"""Status bar formatting and long-path truncation tests."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult

from deeppyro.tui.widgets.status_bar import MAX_CWD_CHARS, StatusBar, short_cwd


class StatusHost(App[Any]):
    def compose(self) -> ComposeResult:
        yield StatusBar()


def show(status: StatusBar, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "session_id": "session-11112222-3333-4444-5555-666677778888",
        "provider": "local",
        "model": "deepseek-test",
        "cwd": "D:/work",
        "input_tokens": 4096,
        "output_tokens": 512,
        "cached_tokens": 2048,
        "context_window": 8192,
        "approval": "ask",
        "phase": "ready",
    }
    values.update(overrides)
    status.show(**values)


def rendered(status: StatusBar) -> str:
    renderable = status.render()
    if isinstance(renderable, Text):
        return renderable.plain
    return str(renderable)


def test_short_cwd_keeps_short_paths() -> None:
    assert short_cwd("D:/work") == "D:/work"
    assert short_cwd("") == ""


def test_short_cwd_keeps_last_two_segments() -> None:
    long_path = "D:/code/deeppyro/src/deeppyro/tui/widgets/nested/folder"
    shortened = short_cwd(long_path)
    assert len(shortened) <= MAX_CWD_CHARS
    assert shortened.startswith("…")
    assert shortened.endswith("nested/folder")


def test_short_cwd_handles_windows_separators() -> None:
    shortened = short_cwd("D:\\code\\deeppyro\\src\\deeppyro\\tui\\widgets\\nested\\folder")
    assert shortened.startswith("…")
    assert shortened.endswith("nested\\folder")


async def test_show_renders_all_fields() -> None:
    app = StatusHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        show(status)
        await pilot.pause()
        text = rendered(status)
    assert "session 11112222" in text
    assert "local/deepseek-test" in text
    assert "D:/work" in text
    assert "4.1k" in text
    assert "512" in text
    assert "2.0k" in text
    assert "ctx 50%" in text
    assert "ask" in text
    assert "ready" in text


async def test_show_masks_long_model_and_cwd() -> None:
    app = StatusHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        long_model = "deepseek-" + "x" * 80
        long_cwd = "D:/code/deeppyro/src/deeppyro/tui/widgets/nested/folder"
        show(status, model=long_model, cwd=long_cwd)
        await pilot.pause()
        text = rendered(status)
        assert "…" in text
        assert "nested/folder" in text
        assert long_model not in text


async def test_show_warns_when_context_is_nearly_full() -> None:
    app = StatusHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        show(status, input_tokens=7900, context_window=8192)
        await pilot.pause()
        assert "ctx 96%" in rendered(status)
