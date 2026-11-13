"""Background-jobs panel modal.

JobsPanelScreen is a read-only snapshot of the session job registry. The
application passes the current rows (deeppyro.builtin.activity.JobInfo objects
or plain mappings) in when it opens the modal; the screen never talks to the
jobs service itself and owns no state beyond the snapshot.

Layout: a DataTable with one row per job (ID / kind / status / label / started
/ finished), a title line and an Esc hint. Status cells use Rich Text colours;
timestamps are epoch milliseconds rendered as local HH:MM:SS. An empty
snapshot shows a single placeholder line instead of the table.

The Lead wires this screen; it is deliberately free of app imports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, ClassVar, Final

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from deeppyro.builtin.activity import JobInfo

#: Dialog title.
TITLE: Final[str] = "Background jobs"

#: Hint shown in the footer of the dialog.
HINT_TEXT: Final[str] = "Esc to close"

#: Empty-state placeholder.
EMPTY_TEXT: Final[str] = "No background jobs."

#: Table header labels, in display order.
COLUMNS: Final[tuple[str, ...]] = ("ID", "Kind", "Status", "Label", "Started", "Finished")

#: Stable table column keys (used by tests and future update paths).
COLUMN_KEYS: Final[tuple[str, ...]] = ("id", "kind", "status", "label", "started", "finished")

#: Fixed display widths so the table never pushes Finished off the dialog.
COLUMN_WIDTHS: Final[tuple[int, ...]] = (26, 7, 9, 44, 10, 10)

#: Rich styles for a job status; an empty style means the default text colour.
STATUS_STYLES: Final[dict[str, str]] = {
    "running": "green",
    "completed": "",
    "done": "",
    "failed": "red",
    "cancelled": "yellow",
    "killed": "yellow",
}


def format_time(value: Any, *, missing: str = "-") -> str:
    """Render epoch milliseconds as local HH:MM:SS, or *missing* when absent."""
    if value is None:
        return missing
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return missing
    if milliseconds <= 0:
        return missing
    try:
        moment = datetime.fromtimestamp(milliseconds / 1000)
    except (OverflowError, OSError, ValueError):
        return missing
    return moment.strftime("%H:%M:%S")


def status_style(status: str) -> str:
    """Return the Rich style used for *status* (unknown statuses stay normal)."""
    return STATUS_STYLES.get(str(status).strip().lower(), "")


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


def _as_ms(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_optional_ms(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_job(row: Any) -> JobInfo:
    """Normalise one JobInfo object or mapping into a JobInfo.

    Missing attributes and keys fall back to the JobInfo defaults, so a bare
    mapping such as {"id": "job-1"} is a valid row.
    """
    status = _as_text(_read(row, "status")) or "running"
    return JobInfo(
        id=_as_text(_read(row, "id")),
        kind=_as_text(_read(row, "kind")),
        label=_as_text(_read(row, "label")),
        status=status,
        owner=_as_text(_read(row, "owner")),
        started_at=_as_ms(_read(row, "started_at", "startedAt", default=0)),
        finished_at=_as_optional_ms(_read(row, "finished_at", "finishedAt", default=None)),
        call_id=_as_text(_read(row, "call_id", "callId")),
    )


def _status_cell(job: JobInfo) -> Text:
    return Text(job.status, style=status_style(job.status))


def _fit(value: str, width: int) -> str:
    """Trim a display cell to *width* characters, keeping the head."""
    text = str(value or "")
    if width <= 0 or len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + "…"


class JobsPanelScreen(ModalScreen[None]):
    """Read-only modal listing the session background jobs."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    JobsPanelScreen {
        align: center middle;
        background: $background 70%;
    }
    #jobs-dialog {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 24;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #jobs-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    #jobs-table {
        height: auto;
        max-height: 16;
        margin: 1 0 0 0;
    }
    #jobs-empty {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    #jobs-hint {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, jobs: Iterable[Any] = ()) -> None:
        super().__init__()
        self._jobs: list[JobInfo] = [normalize_job(row) for row in jobs]

    @property
    def jobs(self) -> list[JobInfo]:
        """Normalised snapshot rows, in the order they were passed in."""
        return list(self._jobs)

    @property
    def rows(self) -> list[tuple[str, str, str, str, str, str]]:
        """Plain-text table rows: ID, kind, status, label, started, finished."""
        return [
            (
                job.id,
                job.kind,
                job.status,
                job.label,
                format_time(job.started_at),
                format_time(job.finished_at),
            )
            for job in self._jobs
        ]

    @property
    def empty(self) -> bool:
        """Whether the snapshot has no jobs."""
        return not self._jobs

    def compose(self) -> ComposeResult:
        with Vertical(id="jobs-dialog"):
            yield Static(TITLE, id="jobs-title", markup=False)
            if self._jobs:
                yield DataTable(id="jobs-table")
            else:
                yield Static(EMPTY_TEXT, id="jobs-empty", markup=False)
            yield Static(HINT_TEXT, id="jobs-hint", markup=False)

    def on_mount(self) -> None:
        if not self._jobs:
            return
        table = self.query_one("#jobs-table", DataTable)
        for label, key, width in zip(COLUMNS, COLUMN_KEYS, COLUMN_WIDTHS, strict=True):
            table.add_column(label, key=key, width=width)
        for job in self._jobs:
            table.add_row(
                _fit(job.id, COLUMN_WIDTHS[0]),
                _fit(job.kind, COLUMN_WIDTHS[1]),
                _status_cell(job),
                _fit(job.label, COLUMN_WIDTHS[3]),
                format_time(job.started_at),
                format_time(job.finished_at),
            )
        table.focus()

    def action_close(self) -> None:
        """Close the modal without a result."""
        self.dismiss(None)


__all__ = [
    "COLUMNS",
    "COLUMN_KEYS",
    "EMPTY_TEXT",
    "HINT_TEXT",
    "STATUS_STYLES",
    "TITLE",
    "JobsPanelScreen",
    "format_time",
    "normalize_job",
    "status_style",
]
