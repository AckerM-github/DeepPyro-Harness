"""Read-only plan viewer modal opened by the /plan command.

PlanScreen snapshots a sequence of TodoItem objects from
deeppyro.builtin.activity and groups it in execution order: in-progress first,
then pending, then completed. The screen owns no state and never talks to the
activity service; the application passes the current projection in when it
opens the modal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import ClassVar, Final

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from deeppyro.builtin.activity import TodoItem, plan_counts

#: Display order for the three todo statuses.
STATUS_ORDER: Final[tuple[str, ...]] = ("in_progress", "pending", "completed")

STATUS_LABELS: Final[dict[str, str]] = {
    "in_progress": "In progress",
    "pending": "Pending",
    "completed": "Completed",
}

STATUS_GLYPHS: Final[dict[str, str]] = {
    "in_progress": "◐",
    "pending": "☐",
    "completed": "✓",
}

EMPTY_MESSAGE = "No plan yet.\nThe agent has not written a plan for this session."


def group_todos(todos: Iterable[TodoItem]) -> tuple[tuple[str, tuple[TodoItem, ...]], ...]:
    """Group todos by status in display order, dropping unknown statuses."""
    buckets: dict[str, list[TodoItem]] = {status: [] for status in STATUS_ORDER}
    for item in todos:
        bucket = buckets.get(item.status)
        if bucket is not None:
            bucket.append(item)
    return tuple((status, tuple(items)) for status, items in buckets.items() if items)


def plan_summary(todos: Sequence[TodoItem]) -> str:
    """One-line count summary used above the grouped rows."""
    counts = plan_counts(tuple(todos))
    parts = [f"{counts['completed']}/{counts['total']} done"]
    if counts["in_progress"]:
        parts.append(f"{counts['in_progress']} in progress")
    if counts["pending"]:
        parts.append(f"{counts['pending']} pending")
    return " · ".join(parts)


def _row_text(item: TodoItem) -> str:
    """Render one todo row as a single line with its status glyph."""
    glyph = STATUS_GLYPHS.get(item.status, "•")
    content = " ".join(str(item.content).split()) or "(empty)"
    return f"{glyph}  {content}"


class PlanScreen(ModalScreen[None]):
    """Grouped, scrollable snapshot of the current session plan."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    PlanScreen {
        align: center middle;
        background: $background 70%;
    }
    #plan-dialog {
        width: 78;
        height: auto;
        max-height: 30;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #plan-title {
        color: $accent;
        text-style: bold;
    }
    #plan-summary {
        color: $text-muted;
        margin: 0;
    }
    #plan-body {
        height: auto;
        max-height: 20;
        margin: 1 0 0 0;
        scrollbar-size: 1 1;
    }
    .plan-group {
        color: $accent;
        text-style: bold;
        margin: 1 0 0 0;
    }
    .plan-row {
        height: auto;
        padding: 0 1;
        color: $text;
    }
    .plan-row.plan-row-in-progress {
        color: $text-warning;
    }
    .plan-row.plan-row-completed {
        color: $text-muted;
    }
    #plan-empty {
        color: $text-muted;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, todos: Sequence[TodoItem]) -> None:
        super().__init__()
        self._todos = tuple(todos)

    @property
    def todos(self) -> tuple[TodoItem, ...]:
        """The snapshot passed to the constructor."""
        return self._todos

    @property
    def grouped(self) -> tuple[tuple[str, tuple[TodoItem, ...]], ...]:
        """Todos grouped in display order, omitting empty groups."""
        return group_todos(self._todos)

    @property
    def summary(self) -> str:
        """Count summary for the currently visible todos."""
        visible = tuple(item for _status, items in self.grouped for item in items)
        return plan_summary(visible)

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-dialog"):
            yield Static("Plan", id="plan-title")
            groups = self.grouped
            if not groups:
                yield Static(EMPTY_MESSAGE, id="plan-empty")
                return
            yield Static(self.summary, id="plan-summary")
            with VerticalScroll(id="plan-body"):
                for status, items in groups:
                    yield Static(
                        f"{STATUS_LABELS[status]} ({len(items)})",
                        classes=f"plan-group plan-group-{status.replace('_', '-')}",
                    )
                    for item in items:
                        yield Static(
                            _row_text(item),
                            classes=f"plan-row plan-row-{status.replace('_', '-')}",
                        )

    def action_close(self) -> None:
        """Close the modal without a result."""
        self.dismiss(None)


__all__ = [
    "EMPTY_MESSAGE",
    "STATUS_GLYPHS",
    "STATUS_LABELS",
    "STATUS_ORDER",
    "PlanScreen",
    "group_todos",
    "plan_summary",
]
