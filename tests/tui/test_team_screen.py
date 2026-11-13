"""Tests for the team panel and task-board modals."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from deeppyro.builtin.activity import AgentInfo
from deeppyro.team.types import MemberInfo, TaskInfo
from deeppyro.tui.screens.team import (
    ACCENT_STYLE,
    BLOCKED_LIMIT,
    BOARD_COLUMNS,
    BOARD_EMPTY_TEXT,
    BOARD_TITLE,
    DESCRIPTION_LIMIT,
    EMPTY_TEXT,
    HINT_TEXT,
    MEMBER_COLUMNS,
    TASK_COLUMNS,
    TITLE,
    TaskBoardScreen,
    TeamPanelScreen,
    TeamTask,
    blocked_by_text,
    description_text,
    normalize_member,
    normalize_task,
    scopes_text,
    sort_tasks,
    status_cell,
    status_style,
    summary_text,
    task_status_rank,
    truncate_text,
)


class TeamHost(App[None]):
    """Open the team panel and record its dismissal result."""

    def __init__(self, members: Any = (), tasks: Any = (), team_id: str = "") -> None:
        super().__init__()
        self._members = members
        self._tasks = tasks
        self._team_id = team_id
        self.results: list[None] = []

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(
            TeamPanelScreen(self._members, self._tasks, self._team_id),
            callback=self.results.append,
        )


class BoardHost(App[None]):
    """Open the task board and record its dismissal result."""

    def __init__(self, tasks: Any = (), title: str = BOARD_TITLE) -> None:
        super().__init__()
        self._tasks = tasks
        self._title = title
        self.results: list[None] = []

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(
            TaskBoardScreen(self._tasks, self._title),
            callback=self.results.append,
        )


def _plain(widget: Static) -> str:
    content: Any = widget.content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


def _cell(cell: Any) -> str:
    plain = getattr(cell, "plain", None)
    return str(plain if plain is not None else cell)


MEMBERS: tuple[Any, ...] = (
    AgentInfo(
        "m1",
        name="Alice",
        description="Lead helper with a rather long description",
        phase="running",
        provider="deepseek",
        model="v3",
        parent="lead",
    ),
    {
        "id": "m2",
        "name": "Bob",
        "description": "Reviewer",
        "phase": "idle",
        "provider": "deepseek",
        "model": "r1",
    },
    MemberInfo(
        id="m3",
        name="Carol",
        description="Tester",
        provider="local",
        model="qwen",
        phase="completed",
    ),
)

TASKS: tuple[Any, ...] = (
    {
        "id": "t1",
        "subject": "Write tests",
        "status": "in_progress",
        "owner": "Bob",
        "blockedBy": ["t0"],
        "writeScopes": ["tests/"],
        "revision": 2,
    },
    {
        "id": "t2",
        "subject": "Ship it",
        "status": "pending",
        "owner": "Alice",
    },
    TaskInfo(
        id="t3",
        revision=4,
        subject="Review",
        description="Review the patch",
        status="completed",
        blocked_by=("t1",),
        write_scopes=("src/", "docs/"),
        owner="Carol",
    ),
)


def test_truncate_text_collapses_whitespace_and_caps_length() -> None:
    assert truncate_text(None, 10) == ""
    assert truncate_text("  a\n b  ", 10) == "a b"
    assert truncate_text("abcdef", 10) == "abcdef"
    assert truncate_text("abcdef", 5) == "abcd…"
    assert truncate_text("abcdef", 1) == "…"
    assert truncate_text("abcdef", 0) == ""
    long_text = truncate_text("x" * 200, DESCRIPTION_LIMIT)
    assert len(long_text) == DESCRIPTION_LIMIT
    assert long_text.endswith("…")


def test_status_style_mapping_and_cells() -> None:
    assert status_style("running") == ACCENT_STYLE
    assert status_style("in_progress") == ACCENT_STYLE
    assert status_style("spawning") == ACCENT_STYLE
    assert status_style("completed") == "green"
    assert status_style("failed") == "red"
    assert status_style("cancelled") == "yellow"
    assert status_style("mystery") == ""
    assert status_style(" RUNNING ") == ACCENT_STYLE

    running = status_cell("running")
    assert isinstance(running, Text)
    assert running.plain == "running"
    assert running.style == ACCENT_STYLE
    assert status_cell("").plain == "-"


def test_normalize_member_accepts_activity_mappings_and_service_objects() -> None:
    original = AgentInfo("m1", name="Alice", phase="running", provider="p", model="m")
    assert normalize_member(original) == original
    assert normalize_member({"id": "m2", "name": "Bob", "phase": "idle"}) == AgentInfo(
        id="m2", name="Bob", phase="idle"
    )
    assert normalize_member({"memberId": "m3", "label": "Carol", "status": "failed"}) == AgentInfo(
        id="m3", name="Carol", phase="failed"
    )
    assert normalize_member(MemberInfo(id="m4", name="Dave", phase="completed")) == AgentInfo(
        id="m4", name="Dave", phase="completed", provider="", model=""
    )
    assert normalize_member({"id": "m5", "parentId": "lead"}) == AgentInfo(id="m5", parent="lead")
    assert normalize_member({}) == AgentInfo(id="")


def test_normalize_task_accepts_mappings_and_service_objects() -> None:
    assert normalize_task({"id": "t1"}) == TeamTask(id="t1", status="pending")
    camel = normalize_task(
        {
            "id": "t2",
            "subject": "Do it",
            "description": "desc",
            "status": "completed",
            "owner": "Alice",
            "blockedBy": ["t1", "", "t0"],
            "writeScopes": ["src/", "tests/"],
            "revision": 3,
        }
    )
    assert camel == TeamTask(
        id="t2",
        subject="Do it",
        description="desc",
        status="completed",
        owner="Alice",
        blocked_by=("t1", "t0"),
        write_scopes=("src/", "tests/"),
        revision=3,
    )
    service = normalize_task(
        TaskInfo(
            id="t3",
            revision=2,
            subject="Review",
            status="in_progress",
            blocked_by=("t1",),
            write_scopes=("docs/",),
            owner="Bob",
        )
    )
    assert service.id == "t3"
    assert service.status == "in_progress"
    assert service.blocked_by == ("t1",)
    assert service.write_scopes == ("docs/",)
    assert service.owner == "Bob"
    assert service.revision == 2
    assert normalize_task({"id": "t4", "blockedBy": "t1", "scopes": "src/"}).blocked_by == ("t1",)
    assert normalize_task({"id": "t4", "blockedBy": None}).blocked_by == ()


def test_description_and_blocked_by_rendering_caps() -> None:
    assert description_text("") == "-"
    assert description_text("  hello \n world ") == "hello world"
    long_description = description_text("x" * 200)
    assert len(long_description) == DESCRIPTION_LIMIT
    assert long_description.endswith("…")

    task = TeamTask(
        id="t1",
        blocked_by=tuple(f"task-{index:02d}" for index in range(10)),
        write_scopes=("src/", "tests/"),
    )
    blocked = blocked_by_text(task)
    assert len(blocked) == BLOCKED_LIMIT
    assert blocked.endswith("…")
    assert scopes_text(task) == "src/, tests/"
    assert blocked_by_text(TeamTask(id="t2")) == "-"
    assert scopes_text(TeamTask(id="t2")) == "-"


def test_sort_tasks_orders_statuses_and_keeps_unknown_last() -> None:
    tasks = (
        TeamTask(id="c", status="completed"),
        TeamTask(id="p1", status="pending"),
        TeamTask(id="i", status="in_progress"),
        TeamTask(id="p2", status="pending"),
        TeamTask(id="u", status="mystery"),
    )
    assert [task.id for task in sort_tasks(tasks)] == ["p1", "p2", "i", "c", "u"]
    assert [task_status_rank(status) for status in ("pending", "in_progress", "completed")] == [
        0,
        1,
        2,
    ]
    assert task_status_rank("unknown") == 3


def test_summary_text_counts_members_tasks_and_running() -> None:
    members = (
        AgentInfo("m1", phase="running"),
        AgentInfo("m2", phase="idle"),
        AgentInfo("m3", phase="running"),
    )
    tasks = (TeamTask(id="t1"), TeamTask(id="t2"))
    assert summary_text(members, tasks) == "3 members · 2 tasks · 2 running"
    assert summary_text((), ()) == "0 members · 0 tasks · 0 running"


def test_screen_properties_are_normalised_snapshots() -> None:
    screen = TeamPanelScreen(MEMBERS, TASKS, team_id="team-1")
    assert screen.empty is False
    assert screen.team_id == "team-1"
    assert [member.id for member in screen.members] == ["m1", "m2", "m3"]
    assert [task.id for task in screen.tasks] == ["t1", "t2", "t3"]
    assert screen.summary == "3 members · 3 tasks · 1 running"
    assert screen.members[0].phase == "running"
    assert screen.tasks[0].blocked_by == ("t0",)

    empty = TeamPanelScreen()
    assert empty.empty is True
    assert empty.members == []
    assert empty.tasks == []
    assert empty.summary == "0 members · 0 tasks · 0 running"


async def test_team_panel_renders_tables_columns_and_rows() -> None:
    app = TeamHost(MEMBERS, TASKS, "team-1")
    async with app.run_test(size=(130, 42)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TeamPanelScreen)
        assert _plain(screen.query_one("#team-title", Static)) == TITLE
        assert _plain(screen.query_one("#team-summary", Static)) == "3 members · 3 tasks · 1 running"
        assert _plain(screen.query_one("#team-hint", Static)) == HINT_TEXT
        assert screen.query_one("#team-dialog") is not None
        assert list(screen.query("#team-empty")) == []

        members = screen.query_one("#team-members", DataTable)
        assert [column.label.plain for column in members.columns.values()] == list(MEMBER_COLUMNS)
        assert members.row_count == 3
        assert [_cell(cell) for cell in members.get_row_at(0)] == [
            "Alice",
            "running",
            "v3",
            "Lead helper with a rather long description",
        ]
        assert [_cell(cell) for cell in members.get_row_at(1)] == [
            "Bob",
            "idle",
            "r1",
            "Reviewer",
        ]
        assert [_cell(cell) for cell in members.get_row_at(2)] == [
            "Carol",
            "completed",
            "qwen",
            "Tester",
        ]

        tasks = screen.query_one("#team-tasks", DataTable)
        assert [column.label.plain for column in tasks.columns.values()] == list(TASK_COLUMNS)
        assert tasks.row_count == 3
        assert [_cell(cell) for cell in tasks.get_row_at(0)] == [
            "t1",
            "in_progress",
            "Write tests",
            "Bob",
            "t0",
        ]
        assert [_cell(cell) for cell in tasks.get_row_at(1)] == ["t2", "pending", "Ship it", "Alice", "-"]
        assert [_cell(cell) for cell in tasks.get_row_at(2)] == [
            "t3",
            "completed",
            "Review",
            "Carol",
            "t1",
        ]
        assert app.focused is members


async def test_team_panel_status_cells_have_rich_styles() -> None:
    members = (
        AgentInfo("m1", name="run", phase="running"),
        AgentInfo("m2", name="spawn", phase="spawning"),
        AgentInfo("m3", name="done", phase="completed"),
        AgentInfo("m4", name="fail", phase="failed"),
        AgentInfo("m5", name="cancel", phase="cancelled"),
    )
    tasks = (
        TeamTask(id="t1", status="in_progress"),
        TeamTask(id="t2", status="completed"),
        TeamTask(id="t3", status="failed"),
        TeamTask(id="t4", status="cancelled"),
    )
    app = TeamHost(members, tasks)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        member_table = app.screen.query_one("#team-members", DataTable)
        phase_cells = [member_table.get_row_at(index)[1] for index in range(member_table.row_count)]
        assert all(isinstance(cell, Text) for cell in phase_cells)
        assert [cell.plain for cell in phase_cells] == [
            "running",
            "spawning",
            "completed",
            "failed",
            "cancelled",
        ]
        assert [cell.style for cell in phase_cells] == [
            ACCENT_STYLE,
            ACCENT_STYLE,
            "green",
            "red",
            "yellow",
        ]

        task_table = app.screen.query_one("#team-tasks", DataTable)
        status_cells = [task_table.get_row_at(index)[1] for index in range(task_table.row_count)]
        assert all(isinstance(cell, Text) for cell in status_cells)
        assert [cell.style for cell in status_cells] == [ACCENT_STYLE, "green", "red", "yellow"]


async def test_team_panel_truncates_long_description_and_blocked_by() -> None:
    members = (AgentInfo("m1", name="Alice", description="x" * 100),)
    tasks = (TeamTask(id="t1", blocked_by=tuple(f"task-{index:02d}" for index in range(10))),)
    app = TeamHost(members, tasks)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        member_row = app.screen.query_one("#team-members", DataTable).get_row_at(0)
        assert len(_cell(member_row[3])) == DESCRIPTION_LIMIT
        assert _cell(member_row[3]).endswith("…")
        task_row = app.screen.query_one("#team-tasks", DataTable).get_row_at(0)
        assert len(_cell(task_row[4])) == BLOCKED_LIMIT
        assert _cell(task_row[4]).endswith("…")


async def test_team_panel_with_only_members_omits_task_table() -> None:
    app = TeamHost((AgentInfo("m1", name="Alice", phase="running"),), ())
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TeamPanelScreen)
        assert screen.query_one("#team-members", DataTable).row_count == 1
        assert list(screen.query("#team-tasks")) == []
        assert app.focused is screen.query_one("#team-members", DataTable)


async def test_team_panel_with_only_tasks_omits_member_table() -> None:
    app = TeamHost((), (TaskInfo(id="t1", subject="Ship", status="pending"),))
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TeamPanelScreen)
        assert list(screen.query("#team-members")) == []
        table = screen.query_one("#team-tasks", DataTable)
        assert table.row_count == 1
        assert app.focused is table


async def test_team_panel_empty_state_and_escape() -> None:
    app = TeamHost()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TeamPanelScreen)
        assert screen.empty is True
        assert _plain(screen.query_one("#team-empty", Static)) == EMPTY_TEXT
        assert _plain(screen.query_one("#team-summary", Static)) == "0 members · 0 tasks · 0 running"
        assert list(screen.query("#team-members")) == []
        assert list(screen.query("#team-tasks")) == []
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
        assert len(app.screen_stack) == 1


async def test_team_panel_escape_dismisses_with_data() -> None:
    app = TeamHost(MEMBERS, TASKS)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TeamPanelScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
        assert app.screen is not screen
        assert len(app.screen_stack) == 1


async def test_task_board_sorts_by_status_and_renders_scopes() -> None:
    tasks = (
        TeamTask(id="done", status="completed", subject="Done", owner="Alice"),
        TeamTask(
            id="wait-1",
            status="pending",
            subject="Wait one",
            owner="Bob",
            blocked_by=("done",),
            write_scopes=("src/",),
        ),
        TeamTask(id="active", status="in_progress", subject="Active", owner="Carol"),
        TeamTask(
            id="wait-2",
            status="pending",
            subject="Wait two",
            owner="Dave",
            write_scopes=("tests/", "docs/"),
        ),
    )
    app = BoardHost(tasks)
    async with app.run_test(size=(130, 36)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TaskBoardScreen)
        assert screen.empty is False
        assert screen.board_title == BOARD_TITLE
        assert [task.id for task in screen.tasks] == ["done", "wait-1", "active", "wait-2"]
        assert [task.id for task in screen.ordered_tasks] == [
            "wait-1",
            "wait-2",
            "active",
            "done",
        ]
        assert _plain(screen.query_one("#board-title", Static)) == BOARD_TITLE
        assert _plain(screen.query_one("#board-hint", Static)) == HINT_TEXT

        table = screen.query_one("#board-table", DataTable)
        assert [column.label.plain for column in table.columns.values()] == list(BOARD_COLUMNS)
        assert table.row_count == 4
        assert [_cell(cell) for cell in table.get_row_at(0)] == [
            "wait-1",
            "pending",
            "Wait one",
            "Bob",
            "done",
            "src/",
        ]
        assert [_cell(cell) for cell in table.get_row_at(1)] == [
            "wait-2",
            "pending",
            "Wait two",
            "Dave",
            "-",
            "tests/, docs/",
        ]
        assert [_cell(cell) for cell in table.get_row_at(2)] == [
            "active",
            "in_progress",
            "Active",
            "Carol",
            "-",
            "-",
        ]
        assert [_cell(cell) for cell in table.get_row_at(3)] == [
            "done",
            "completed",
            "Done",
            "Alice",
            "-",
            "-",
        ]
        assert app.focused is table


async def test_task_board_custom_title_and_status_styles() -> None:
    tasks = (
        TeamTask(id="p", status="pending"),
        TeamTask(id="i", status="in_progress"),
        TeamTask(id="c", status="completed"),
    )
    app = BoardHost(tasks, title="Sprint 7")
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TaskBoardScreen)
        assert screen.board_title == "Sprint 7"
        assert screen.title == "Sprint 7"
        assert _plain(screen.query_one("#board-title", Static)) == "Sprint 7"
        table = screen.query_one("#board-table", DataTable)
        status_cells = [table.get_row_at(index)[1] for index in range(table.row_count)]
        assert all(isinstance(cell, Text) for cell in status_cells)
        assert [cell.style for cell in status_cells] == ["", ACCENT_STYLE, "green"]


async def test_task_board_empty_state_and_escape() -> None:
    app = BoardHost(())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, TaskBoardScreen)
        assert screen.empty is True
        assert screen.tasks == []
        assert _plain(screen.query_one("#board-empty", Static)) == BOARD_EMPTY_TEXT
        assert list(screen.query("#board-table")) == []
        await pilot.press("escape")
        await pilot.pause()
        assert app.results == [None]
        assert len(app.screen_stack) == 1


def test_widgets_do_not_define_private_render() -> None:
    assert "_render" not in TeamPanelScreen.__dict__
    assert "_render" not in TaskBoardScreen.__dict__
