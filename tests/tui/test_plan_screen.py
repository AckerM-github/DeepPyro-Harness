"""Plan screen tests: grouping, glyphs, empty state and Escape."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Static

from deeppyro.builtin.activity import TodoItem
from deeppyro.tui.screens.plan import (
    PlanScreen,
    group_todos,
    plan_summary,
)


def rendered(widget: Static) -> str:
    content: Any = widget.render()
    if isinstance(content, str):
        return content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


class PlanHost(App[None]):
    def __init__(self, todos: tuple[TodoItem, ...]) -> None:
        super().__init__()
        self._todos = todos

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(PlanScreen(self._todos))


def test_group_todos_orders_groups_and_drops_unknown_statuses() -> None:
    todos = (
        TodoItem("done first", "completed"),
        TodoItem("waiting one", "pending"),
        TodoItem("current", "in_progress"),
        TodoItem("waiting two", "pending"),
        TodoItem("done second", "completed"),
        TodoItem("mystery", "blocked"),
    )
    grouped = group_todos(todos)
    assert [status for status, _items in grouped] == ["in_progress", "pending", "completed"]
    assert [item.content for item in grouped[0][1]] == ["current"]
    assert [item.content for item in grouped[1][1]] == ["waiting one", "waiting two"]
    assert [item.content for item in grouped[2][1]] == ["done first", "done second"]


def test_plan_summary_counts_visible_todos() -> None:
    todos = (
        TodoItem("done first", "completed"),
        TodoItem("done second", "completed"),
        TodoItem("current", "in_progress"),
        TodoItem("waiting", "pending"),
    )
    assert plan_summary(todos) == "2/4 done · 1 in progress · 1 pending"
    assert plan_summary((TodoItem("done", "completed"),)) == "1/1 done"
    assert plan_summary(()) == "0/0 done"


async def test_plan_screen_groups_rows_and_renders_glyphs() -> None:
    todos = (
        TodoItem("done first", "completed"),
        TodoItem("waiting one", "pending"),
        TodoItem("current", "in_progress"),
        TodoItem("waiting two", "pending"),
        TodoItem("done second", "completed"),
    )
    app = PlanHost(todos)
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PlanScreen)
        assert screen.todos == todos
        assert [status for status, _items in screen.grouped] == [
            "in_progress",
            "pending",
            "completed",
        ]
        assert screen.summary == "2/5 done · 1 in progress · 2 pending"

        headings = [rendered(heading) for heading in screen.query(".plan-group")]
        assert headings == ["In progress (1)", "Pending (2)", "Completed (2)"]

        rows = [rendered(row) for row in screen.query(".plan-row")]
        assert len(rows) == 5
        assert rows[0].startswith("◐")
        assert "current" in rows[0]
        assert rows[1].startswith("☐")
        assert "waiting one" in rows[1]
        assert rows[2].startswith("☐")
        assert "waiting two" in rows[2]
        assert rows[3].startswith("✓")
        assert "done first" in rows[3]
        assert rows[4].startswith("✓")
        assert "done second" in rows[4]

        summary = screen.query_one("#plan-summary", Static)
        assert rendered(summary) == "2/5 done · 1 in progress · 2 pending"


async def test_plan_screen_shows_empty_state() -> None:
    app = PlanHost(())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PlanScreen)
        assert screen.todos == ()
        assert screen.grouped == ()
        assert list(screen.query(".plan-row")) == []
        empty = screen.query_one("#plan-empty", Static)
        text = rendered(empty)
        assert "no plan yet" in text.lower()
        assert "has not written a plan" in text.lower()


async def test_plan_screen_ignores_unknown_statuses_in_ui() -> None:
    app = PlanHost(
        (
            TodoItem("current", "in_progress"),
            TodoItem("mystery", "blocked"),
            TodoItem("done", "completed"),
        )
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PlanScreen)
        rows = [rendered(row) for row in screen.query(".plan-row")]
        assert len(rows) == 2
        assert "mystery" not in "\n".join(rows)


async def test_escape_closes_plan_screen() -> None:
    app = PlanHost((TodoItem("next", "pending"),))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PlanScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not screen
        assert not isinstance(app.screen, PlanScreen)
