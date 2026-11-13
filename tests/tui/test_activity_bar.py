"""Activity bar tests: pure helpers, visibility and click messages."""

from __future__ import annotations

from typing import Any

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Static

from deeppyro.builtin.activity import ActivityState, AgentInfo, JobInfo, TodoItem
from deeppyro.tui.widgets.activity_bar import (
    ActivityBar,
    agents_segment,
    jobs_segment,
    plan_segment,
)


def rendered(widget: Static) -> str:
    content: Any = widget.render()
    if isinstance(content, str):
        return content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


class ActivityHost(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.bar = ActivityBar()
        self.clicks: list[str] = []

    def compose(self) -> ComposeResult:
        yield self.bar

    @on(ActivityBar.SegmentClicked)
    def _capture(self, event: ActivityBar.SegmentClicked) -> None:
        self.clicks.append(event.segment)


def test_plan_segment_prefers_in_progress_then_pending() -> None:
    state = ActivityState(
        todos=(
            TodoItem("done", "completed"),
            TodoItem("next", "pending"),
            TodoItem("now", "in_progress"),
            TodoItem("later", "pending"),
        )
    )
    assert plan_segment(state) == "plan 1/4 · now"

    no_progress = ActivityState(todos=(TodoItem("first", "pending"), TodoItem("second", "pending")))
    assert plan_segment(no_progress) == "plan 0/2 · first"

    all_done = ActivityState(todos=(TodoItem("a", "completed"), TodoItem("b", "completed")))
    assert plan_segment(all_done) == "plan 2/2"
    assert plan_segment(ActivityState()) == ""


def test_plan_segment_collapses_whitespace_and_trims_long_items() -> None:
    state = ActivityState(todos=(TodoItem("line one\nline two", "pending"),))
    assert plan_segment(state) == "plan 0/1 · line one line two"

    long_state = ActivityState(todos=(TodoItem("x" * 100, "pending"),))
    text = plan_segment(long_state)
    assert text.startswith("plan 0/1 · ")
    assert text.endswith("…")
    assert len(text) <= len("plan 0/1 · ") + 48


def test_jobs_segment_counts_only_running_jobs() -> None:
    state = ActivityState(
        jobs=(
            JobInfo("a", status="running"),
            JobInfo("b", status="done"),
            JobInfo("c", status="running"),
        )
    )
    assert jobs_segment(state) == "2 jobs"
    assert jobs_segment(ActivityState(jobs=(JobInfo("b", status="done"),))) == ""
    assert jobs_segment(ActivityState()) == ""


def test_agents_segment_counts_known_agents() -> None:
    assert agents_segment(ActivityState(agents=(AgentInfo("a"),))) == "1 agent"
    assert agents_segment(ActivityState(agents=(AgentInfo("a"), AgentInfo("b")))) == "2 agents"
    assert agents_segment(ActivityState()) == ""


def test_segment_clicked_validates_segment() -> None:
    assert ActivityBar.SegmentClicked("plan").segment == "plan"
    with pytest.raises(ValueError, match="unknown activity segment"):
        ActivityBar.SegmentClicked("other")


async def test_bar_is_hidden_for_empty_state() -> None:
    app = ActivityHost()
    async with app.run_test(size=(100, 10)) as pilot:
        await pilot.pause()
        assert app.bar.display is False
        app.bar.update_state(ActivityState())
        await pilot.pause()
        assert app.bar.display is False
        for segment in ("plan", "jobs", "agents"):
            widget = app.bar.query_one(f"#activity-{segment}", Static)
            assert widget.display is False
            assert rendered(widget) == ""
        assert app.bar.plan_text == ""
        assert app.bar.jobs_text == ""
        assert app.bar.agents_text == ""


async def test_plan_segment_renders_hides_siblings_and_keeps_one_row() -> None:
    state = ActivityState(todos=(TodoItem("done", "completed"), TodoItem("now", "in_progress")))
    app = ActivityHost()
    async with app.run_test(size=(100, 10)) as pilot:
        await pilot.pause()
        app.bar.update_state(state)
        await pilot.pause()
        plan = app.bar.query_one("#activity-plan", Static)
        jobs = app.bar.query_one("#activity-jobs", Static)
        agents = app.bar.query_one("#activity-agents", Static)
        assert app.bar.display is True
        assert app.bar.size.height == 1
        assert plan.display is True
        assert rendered(plan) == "plan 1/2 · now"
        assert jobs.display is False
        assert agents.display is False
        assert app.bar.state == state


async def test_all_segments_render_when_state_has_items() -> None:
    state = ActivityState(
        todos=(TodoItem("next", "pending"),),
        jobs=(JobInfo("j1", status="running"),),
        agents=(AgentInfo("a"), AgentInfo("b")),
    )
    app = ActivityHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        app.bar.update_state(state)
        await pilot.pause()
        assert app.bar.display is True
        for segment in ("plan", "jobs", "agents"):
            assert app.bar.query_one(f"#activity-{segment}", Static).display is True
        assert app.bar.plan_text == "plan 0/1 · next"
        assert app.bar.jobs_text == "1 job"
        assert app.bar.agents_text == "2 agents"
        assert rendered(app.bar.query_one("#activity-plan", Static)) == "plan 0/1 · next"
        assert rendered(app.bar.query_one("#activity-jobs", Static)) == "1 job"
        assert rendered(app.bar.query_one("#activity-agents", Static)) == "2 agents"


async def test_clicking_each_segment_posts_clicked_message() -> None:
    state = ActivityState(
        todos=(TodoItem("next", "pending"),),
        jobs=(JobInfo("j1", status="running"),),
        agents=(AgentInfo("a"),),
    )
    app = ActivityHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        app.bar.update_state(state)
        await pilot.pause()
        for segment in ("plan", "jobs", "agents"):
            await pilot.click(f"#activity-{segment}")
            await pilot.pause()
    assert app.clicks == ["plan", "jobs", "agents"]
