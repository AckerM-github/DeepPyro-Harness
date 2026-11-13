"""Tests for the background-jobs panel modal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from deeppyro.builtin.activity import JobInfo
from deeppyro.tui.screens.jobs import (
    COLUMNS,
    EMPTY_TEXT,
    HINT_TEXT,
    STATUS_STYLES,
    TITLE,
    JobsPanelScreen,
    format_time,
    normalize_job,
    status_style,
)

SAMPLES: tuple[JobInfo, ...] = (
    JobInfo(
        "job-1",
        kind="shell",
        label="npm test",
        status="running",
        started_at=1700000000000,
    ),
    JobInfo(
        "job-2",
        kind="shell",
        label="build",
        status="completed",
        started_at=1700000001000,
        finished_at=1700000002500,
    ),
    JobInfo(
        "job-3",
        kind="random",
        label="boom",
        status="failed",
        started_at=1700000003000,
        finished_at=1700000004000,
    ),
)


class HostApp(App[None]):
    """Bare application that opens the modal and records its result."""

    def __init__(self, jobs: Any = ()) -> None:
        super().__init__()
        self._jobs = jobs
        self.results: list[None] = []

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(JobsPanelScreen(self._jobs), callback=self.results.append)


def _plain(widget: Static) -> str:
    content: Any = widget.content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


def _cell(cell: Any) -> str:
    plain = getattr(cell, "plain", None)
    return str(plain if plain is not None else cell)


def _clock(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000).strftime("%H:%M:%S")


def test_format_time_renders_local_clock_and_dash() -> None:
    assert format_time(None) == "-"
    assert format_time(0) == "-"
    assert format_time(-5) == "-"
    assert format_time("not-a-time") == "-"
    assert format_time(1700000000000) == _clock(1700000000000)
    assert len(format_time(1700000000000)) == 8
    assert format_time(None, missing="n/a") == "n/a"


def test_status_style_mapping() -> None:
    assert status_style("running") == "green"
    assert status_style("completed") == ""
    assert status_style("done") == ""
    assert status_style("failed") == "red"
    assert status_style("cancelled") == "yellow"
    assert status_style("killed") == "yellow"
    assert status_style("unknown") == ""
    assert status_style(" RUNNING ") == "green"
    assert STATUS_STYLES["running"] == "green"


def test_normalize_job_accepts_mappings_and_objects() -> None:
    minimal = normalize_job({"id": "job-a"})
    assert minimal == JobInfo(id="job-a", status="running")

    camel = normalize_job(
        {
            "id": "job-b",
            "kind": "shell",
            "status": "failed",
            "startedAt": 1700000000000,
            "finishedAt": 1700000005000,
            "callId": "call-1",
        }
    )
    assert camel.id == "job-b"
    assert camel.kind == "shell"
    assert camel.status == "failed"
    assert camel.started_at == 1700000000000
    assert camel.finished_at == 1700000005000
    assert camel.call_id == "call-1"

    original = JobInfo("job-c", kind="random", label="label", started_at=7)
    assert normalize_job(original) == original
    assert normalize_job({}) == JobInfo(id="", status="running")


def test_screen_exposes_jobs_rows_and_empty() -> None:
    screen = JobsPanelScreen((*SAMPLES, {"id": "job-4", "status": "cancelled"}))
    assert screen.empty is False
    assert [job.id for job in screen.jobs] == ["job-1", "job-2", "job-3", "job-4"]
    assert screen.rows == [
        ("job-1", "shell", "running", "npm test", _clock(1700000000000), "-"),
        ("job-2", "shell", "completed", "build", _clock(1700000001000), _clock(1700000002500)),
        ("job-3", "random", "failed", "boom", _clock(1700000003000), _clock(1700000004000)),
        ("job-4", "", "cancelled", "", "-", "-"),
    ]


def test_empty_screen_exposes_empty_snapshot() -> None:
    screen = JobsPanelScreen(())
    assert screen.empty is True
    assert screen.jobs == []
    assert screen.rows == []


async def test_table_columns_rows_and_focus() -> None:
    app = HostApp(SAMPLES)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, JobsPanelScreen)
        assert screen.empty is False
        assert _plain(screen.query_one("#jobs-title", Static)) == TITLE
        assert _plain(screen.query_one("#jobs-hint", Static)) == HINT_TEXT
        assert screen.query_one("#jobs-dialog") is not None

        table = screen.query_one("#jobs-table", DataTable)
        assert [column.label.plain for column in table.columns.values()] == list(COLUMNS)
        assert table.row_count == 3
        assert [_cell(cell) for cell in table.get_row_at(0)] == [
            "job-1",
            "shell",
            "running",
            "npm test",
            _clock(1700000000000),
            "-",
        ]
        assert [_cell(cell) for cell in table.get_row_at(1)] == [
            "job-2",
            "shell",
            "completed",
            "build",
            _clock(1700000001000),
            _clock(1700000002500),
        ]
        assert app.focused is table


async def test_status_cells_use_rich_styles() -> None:
    jobs = (
        JobInfo("r", status="running"),
        JobInfo("c", status="completed"),
        JobInfo("f", status="failed"),
        JobInfo("x", status="cancelled"),
        JobInfo("k", status="killed"),
        JobInfo("u", status="mystery"),
    )
    app = HostApp(jobs)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        table = app.screen.query_one("#jobs-table", DataTable)
        statuses = [table.get_row_at(index)[2] for index in range(table.row_count)]
        assert all(isinstance(cell, Text) for cell in statuses)
        assert [cell.plain for cell in statuses] == [
            "running",
            "completed",
            "failed",
            "cancelled",
            "killed",
            "mystery",
        ]
        assert [cell.style for cell in statuses] == ["green", "", "red", "yellow", "yellow", ""]


async def test_missing_finished_timestamp_renders_dash() -> None:
    app = HostApp((JobInfo("job-1", status="running", started_at=0, finished_at=None),))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        row = app.screen.query_one("#jobs-table", DataTable).get_row_at(0)
        assert _cell(row[4]) == "-"
        assert _cell(row[5]) == "-"


async def test_empty_state_shows_placeholder_and_no_table() -> None:
    app = HostApp(())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, JobsPanelScreen)
        assert screen.empty is True
        assert _plain(screen.query_one("#jobs-empty", Static)) == EMPTY_TEXT
        assert list(screen.query("#jobs-table")) == []
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
        assert len(app.screen_stack) == 1


async def test_escape_dismisses_the_modal() -> None:
    app = HostApp(SAMPLES)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, JobsPanelScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
        assert app.screen is not screen
        assert len(app.screen_stack) == 1


def test_widget_does_not_define_private_render() -> None:
    assert "_render" not in JobsPanelScreen.__dict__
