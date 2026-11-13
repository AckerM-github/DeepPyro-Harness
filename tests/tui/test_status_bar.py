"""Status bar formatting tests."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult

from deeppyro.tui.widgets.status_bar import StatusBar


class StatusHost(App[Any]):
    def compose(self) -> ComposeResult:
        yield StatusBar()


def show(status: StatusBar, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "session_id": "session-11112222-3333-4444-5555-666677778888",
        "provider": "local",
        "model": "deepseek-test",
        "input_tokens": 4096,
        "output_tokens": 512,
        "cached_tokens": 2048,
        "context_window": 8192,
        "phase": "ready",
        "preset": "workspace-write",
        "approval": "ask",
    }
    values.update(overrides)
    status.show(**values)


def rendered(status: StatusBar) -> str:
    renderable = status.render()
    if isinstance(renderable, Text):
        return renderable.plain
    return str(renderable)


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
    assert "4.1k" in text
    assert "512" in text
    assert "2.0k" in text
    assert "ctx 50%" in text
    assert "workspace-write/ask" in text
    assert "ready" in text


async def test_show_masks_long_model() -> None:
    app = StatusHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        long_model = "deepseek-" + "x" * 80
        show(status, model=long_model)
        await pilot.pause()
        text = rendered(status)
    assert "…" in text
    assert long_model not in text


async def test_show_omits_policy_when_not_given() -> None:
    app = StatusHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        show(status, preset="", approval="")
        await pilot.pause()
        text = rendered(status)
    assert "workspace-write" not in text
    assert "ask" not in text


async def test_show_uses_latest_request_for_context_percent() -> None:
    app = StatusHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        # Cumulative usage is large because a turn has many steps, but the
        # live context is only the latest request.
        show(status, input_tokens=40_000, context_tokens=6_000, context_window=32_768)
        await pilot.pause()
        text = rendered(status)
    assert "ctx 18%" in text
    assert "40.0k" in text


async def test_show_warns_when_context_is_nearly_full() -> None:
    app = StatusHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        status = app.query_one(StatusBar)
        show(status, input_tokens=7900, context_window=8192)
        await pilot.pause()
        assert "ctx 96%" in rendered(status)
