"""Read-only team and task-board modals.

The M3 team service owns members and tasks; these screens only render a
snapshot supplied by the application.  Rows may be service snapshots
(:class:`deeppyro.team.types.MemberInfo` / :class:`~TaskInfo`), mappings or
activity objects, so every value is read defensively through getattr/dict
fallbacks and missing fields degrade to placeholders.

TeamPanelScreen shows two DataTables (members and tasks) with status/phase
colours; TaskBoardScreen shows a single status-sorted task table.  Both are
opened by the Lead and never reach into app/runtime modules.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from deeppyro.builtin.activity import AgentInfo

#: Dialog title for the team panel.
TITLE: Final[str] = "Agent team"

#: Footer hint shared by both screens.
HINT_TEXT: Final[str] = "Esc to close"

#: Empty-state text for the team panel.
EMPTY_TEXT: Final[str] = "No team members or tasks."

#: Default task-board title.
BOARD_TITLE: Final[str] = "Task board"

#: Empty-state text for the task board.
BOARD_EMPTY_TEXT: Final[str] = "No tasks."

#: Maximum rendered length of a member description.
DESCRIPTION_LIMIT: Final[int] = 60

#: Maximum rendered length of a comma-joined blockedBy list.
BLOCKED_LIMIT: Final[int] = 40

#: Rich colour used for "accent" statuses.  Rich does not understand Textual
#: theme variables, so the theme's accent value is carried as a literal.
ACCENT_STYLE: Final[str] = "#7aa2f7"

#: Rich styles for member phases and task statuses.
STATUS_STYLES: Final[dict[str, str]] = {
    "spawning": ACCENT_STYLE,
    "running": ACCENT_STYLE,
    "in_progress": ACCENT_STYLE,
    "completed": "green",
    "failed": "red",
    "cancelled": "yellow",
}

#: Task statuses in display order.
TASK_STATUS_ORDER: Final[tuple[str, ...]] = ("pending", "in_progress", "completed")

#: Team-member table header labels, in display order.
MEMBER_COLUMNS: Final[tuple[str, ...]] = ("Name", "Phase", "Model", "Description")

#: Team-member table column keys.
MEMBER_COLUMN_KEYS: Final[tuple[str, ...]] = ("name", "phase", "model", "description")

#: Team-task table header labels, in display order.
TASK_COLUMNS: Final[tuple[str, ...]] = ("ID", "Status", "Subject", "Owner", "Blocked by")

#: Team-task table column keys.
TASK_COLUMN_KEYS: Final[tuple[str, ...]] = ("id", "status", "subject", "owner", "blocked_by")

#: Task-board table header labels, in display order.
BOARD_COLUMNS: Final[tuple[str, ...]] = (
    "ID",
    "Status",
    "Subject",
    "Owner",
    "Blocked by",
    "Scopes",
)

#: Task-board table column keys.
BOARD_COLUMN_KEYS: Final[tuple[str, ...]] = (
    "id",
    "status",
    "subject",
    "owner",
    "blocked_by",
    "scopes",
)


@dataclass(frozen=True, slots=True)
class TeamTask:
    """Normalised snapshot of one shared task-board item."""

    id: str
    subject: str = ""
    description: str = ""
    status: str = "pending"
    owner: str = ""
    blocked_by: tuple[str, ...] = ()
    write_scopes: tuple[str, ...] = ()
    revision: int = 0


def _read(row: Any, *names: str, default: Any = "") -> Any:
    """Read the first present attribute or mapping key among *names*."""
    for name in names:
        if isinstance(row, Mapping):
            if name in row and row[name] is not None:
                return row[name]
        else:
            value = getattr(row, name, None)
            if value is not None:
                return value
    return default


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_list(value: Any) -> tuple[str, ...]:
    """Normalise a list-like field to a tuple of non-empty strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, Mapping):
        return ()
    try:
        items = list(value)
    except TypeError:
        return ()
    result: list[str] = []
    for item in items:
        text = _as_text(item)
        if text:
            result.append(text)
    return tuple(result)


def truncate_text(value: Any, limit: int) -> str:
    """Collapse whitespace and cap *value* at *limit* characters.

    An over-long string is shortened with a single trailing ellipsis so the
    returned text is never longer than *limit*.
    """
    text = " ".join(_as_text(value).split())
    if not text:
        return ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def status_style(status: str) -> str:
    """Return the Rich style used for *status* (unknown values stay normal)."""
    return STATUS_STYLES.get(str(status).strip().lower(), "")


def normalize_member(row: Any) -> AgentInfo:
    """Normalise one member object or mapping into an :class:`AgentInfo`."""
    return AgentInfo(
        id=_as_text(_read(row, "id", "member_id", "memberId")),
        name=_as_text(_read(row, "name", "label")),
        description=_as_text(_read(row, "description")),
        phase=_as_text(_read(row, "phase", "status")),
        provider=_as_text(_read(row, "provider")),
        model=_as_text(_read(row, "model")),
        parent=_as_text(_read(row, "parent", "parent_id", "parentId")),
    )


def normalize_task(row: Any) -> TeamTask:
    """Normalise one task object or mapping into a :class:`TeamTask`."""
    status = _as_text(_read(row, "status")) or "pending"
    return TeamTask(
        id=_as_text(_read(row, "id", "task_id", "taskId")),
        subject=_as_text(_read(row, "subject", "title")),
        description=_as_text(_read(row, "description")),
        status=status,
        owner=_as_text(_read(row, "owner", "owner_name", "ownerName")),
        blocked_by=_as_list(_read(row, "blockedBy", "blocked_by", "blocked", default=())),
        write_scopes=_as_list(_read(row, "writeScopes", "write_scopes", "scopes", default=())),
        revision=_as_int(_read(row, "revision", "rev", default=0)),
    )


def task_status_rank(status: str) -> int:
    """Sort key placing pending, in-progress and completed tasks in order."""
    normalized = str(status).strip().lower()
    try:
        return TASK_STATUS_ORDER.index(normalized)
    except ValueError:
        return len(TASK_STATUS_ORDER)


def sort_tasks(tasks: Iterable[TeamTask]) -> list[TeamTask]:
    """Return *tasks* stably sorted pending -> in_progress -> completed."""
    return sorted(tasks, key=lambda task: task_status_rank(task.status))


def summary_text(members: Sequence[AgentInfo], tasks: Sequence[TeamTask]) -> str:
    """Build the one-line team summary shown above the tables."""
    running = sum(1 for member in members if member.phase == "running")
    return f"{len(members)} members · {len(tasks)} tasks · {running} running"


def description_text(value: Any) -> str:
    """Render a member description capped at :data:`DESCRIPTION_LIMIT`."""
    return truncate_text(value, DESCRIPTION_LIMIT) or "-"


def blocked_by_text(task: TeamTask) -> str:
    """Render a task's blockedBy list, capped at :data:`BLOCKED_LIMIT`."""
    return truncate_text(", ".join(task.blocked_by), BLOCKED_LIMIT) or "-"


def scopes_text(task: TeamTask) -> str:
    """Render a task's write scopes as a comma-separated list."""
    return ", ".join(task.write_scopes) or "-"


def status_cell(status: str) -> Text:
    """Build a coloured DataTable cell for a phase or task status."""
    return Text(status or "-", style=status_style(status))


def _name_text(member: AgentInfo) -> str:
    return member.name or member.id or "-"


def _display(value: Any) -> str:
    return _as_text(value) or "-"


class TeamPanelScreen(ModalScreen[None]):
    """Read-only modal showing team members and shared task-board items."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    TeamPanelScreen {
        align: center middle;
        background: $background 70%;
    }
    #team-dialog {
        width: 90%;
        max-width: 140;
        height: auto;
        max-height: 32;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #team-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    #team-summary {
        height: 1;
        color: $text-muted;
    }
    #team-members {
        height: auto;
        max-height: 10;
        margin: 1 0 0 0;
    }
    #team-tasks {
        height: auto;
        max-height: 10;
        margin: 1 0 0 0;
    }
    #team-empty {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    #team-hint {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 0;
    }
    """

    def __init__(
        self,
        members: Iterable[Any] = (),
        tasks: Iterable[Any] = (),
        team_id: str = "",
    ) -> None:
        super().__init__()
        self._members: list[AgentInfo] = [normalize_member(row) for row in members]
        self._tasks: list[TeamTask] = [normalize_task(row) for row in tasks]
        self._team_id = _as_text(team_id)

    @property
    def members(self) -> list[AgentInfo]:
        """Normalised member snapshot, in the order supplied."""
        return list(self._members)

    @property
    def tasks(self) -> list[TeamTask]:
        """Normalised task snapshot, in the order supplied."""
        return list(self._tasks)

    @property
    def empty(self) -> bool:
        """Whether both the member and task snapshots are empty."""
        return not self._members and not self._tasks

    @property
    def team_id(self) -> str:
        """Identifier passed by the caller (may be empty)."""
        return self._team_id

    @property
    def summary(self) -> str:
        """One-line counts: members, tasks and running members."""
        return summary_text(self._members, self._tasks)

    def compose(self) -> ComposeResult:
        with Vertical(id="team-dialog"):
            yield Static(TITLE, id="team-title", markup=False)
            yield Static(self.summary, id="team-summary", markup=False)
            if self.empty:
                yield Static(EMPTY_TEXT, id="team-empty", markup=False)
            else:
                if self._members:
                    yield DataTable(id="team-members")
                if self._tasks:
                    yield DataTable(id="team-tasks")
            yield Static(HINT_TEXT, id="team-hint", markup=False)

    def on_mount(self) -> None:
        if self._members:
            table = self.query_one("#team-members", DataTable)
            self._fill_members(table)
            table.focus()
        if self._tasks:
            table = self.query_one("#team-tasks", DataTable)
            self._fill_tasks(table)
            if not self._members:
                table.focus()

    def _fill_members(self, table: DataTable) -> None:
        table.add_columns(
            *[(label, key) for label, key in zip(MEMBER_COLUMNS, MEMBER_COLUMN_KEYS, strict=True)]
        )
        for member in self._members:
            table.add_row(
                _name_text(member),
                status_cell(member.phase),
                _display(member.model),
                description_text(member.description),
            )

    def _fill_tasks(self, table: DataTable) -> None:
        table.add_columns(*[(label, key) for label, key in zip(TASK_COLUMNS, TASK_COLUMN_KEYS, strict=True)])
        for task in self._tasks:
            table.add_row(
                _display(task.id),
                status_cell(task.status),
                _display(task.subject),
                _display(task.owner),
                blocked_by_text(task),
            )

    def action_close(self) -> None:
        """Close the modal without a result."""
        self.dismiss(None)


class TaskBoardScreen(ModalScreen[None]):
    """Read-only modal listing the shared task board, grouped by status."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    TaskBoardScreen {
        align: center middle;
        background: $background 70%;
    }
    #board-dialog {
        width: 92%;
        max-width: 140;
        height: auto;
        max-height: 30;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #board-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    #board-table {
        height: auto;
        max-height: 22;
        margin: 1 0 0 0;
    }
    #board-empty {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    #board-hint {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, tasks: Iterable[Any] = (), title: str = BOARD_TITLE) -> None:
        super().__init__()
        self._tasks: list[TeamTask] = [normalize_task(row) for row in tasks]
        self._title = _as_text(title) or BOARD_TITLE
        self.title = self._title

    @property
    def tasks(self) -> list[TeamTask]:
        """Normalised tasks in their original input order."""
        return list(self._tasks)

    @property
    def ordered_tasks(self) -> list[TeamTask]:
        """Tasks in display order: pending, in progress, completed."""
        return sort_tasks(self._tasks)

    @property
    def empty(self) -> bool:
        """Whether the board has no tasks."""
        return not self._tasks

    @property
    def board_title(self) -> str:
        """Dialog title supplied by the caller."""
        return self._title

    def compose(self) -> ComposeResult:
        with Vertical(id="board-dialog"):
            yield Static(self._title, id="board-title", markup=False)
            if self.empty:
                yield Static(BOARD_EMPTY_TEXT, id="board-empty", markup=False)
            else:
                yield DataTable(id="board-table")
            yield Static(HINT_TEXT, id="board-hint", markup=False)

    def on_mount(self) -> None:
        if self.empty:
            return
        table = self.query_one("#board-table", DataTable)
        table.add_columns(
            *[(label, key) for label, key in zip(BOARD_COLUMNS, BOARD_COLUMN_KEYS, strict=True)]
        )
        for task in self.ordered_tasks:
            table.add_row(
                _display(task.id),
                status_cell(task.status),
                _display(task.subject),
                _display(task.owner),
                blocked_by_text(task),
                scopes_text(task),
            )
        table.focus()

    def action_close(self) -> None:
        """Close the modal without a result."""
        self.dismiss(None)


__all__ = [
    "ACCENT_STYLE",
    "BLOCKED_LIMIT",
    "BOARD_COLUMNS",
    "BOARD_COLUMN_KEYS",
    "BOARD_EMPTY_TEXT",
    "BOARD_TITLE",
    "DESCRIPTION_LIMIT",
    "EMPTY_TEXT",
    "HINT_TEXT",
    "MEMBER_COLUMNS",
    "MEMBER_COLUMN_KEYS",
    "STATUS_STYLES",
    "TASK_COLUMNS",
    "TASK_COLUMN_KEYS",
    "TASK_STATUS_ORDER",
    "TITLE",
    "TaskBoardScreen",
    "TeamPanelScreen",
    "TeamTask",
    "blocked_by_text",
    "description_text",
    "normalize_member",
    "normalize_task",
    "scopes_text",
    "sort_tasks",
    "status_cell",
    "status_style",
    "summary_text",
    "task_status_rank",
    "truncate_text",
]
