"""One-row activity bar showing plan, background jobs and the agent team.

The widget is a pure view over the ActivityState dataclass from
deeppyro.builtin.activity; the application calls ActivityBar.update_state
after refreshing the activity service. Every segment renders plain text and
is implemented as a small Static so the bar can be tested without a running
agent.

Clicking a non-empty segment posts ActivityBar.SegmentClicked; the app maps
plan to PlanScreen and jobs/agents to their future panels.
"""

from __future__ import annotations

from typing import Final

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static

from deeppyro.builtin.activity import ActivityState, current_todo, plan_counts

#: Segment names in display order.
SEGMENTS: Final[tuple[str, ...]] = ("plan", "jobs", "agents")

PLAN: Final[str] = "plan"
JOBS: Final[str] = "jobs"
AGENTS: Final[str] = "agents"

#: Maximum characters of the current todo shown next to the plan counter.
CURRENT_ITEM_LIMIT: Final[int] = 48

_SEGMENT_BY_ID: Final[dict[str, str]] = {f"activity-{segment}": segment for segment in SEGMENTS}


def _one_line(value: str, limit: int = CURRENT_ITEM_LIMIT) -> str:
    """Collapse whitespace and trim a value to one short line."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)].rstrip() + "…"


def plan_segment(state: ActivityState) -> str:
    """Build the plan segment: completed/total plus the current item.

    Returns an empty string when there is no plan, which hides the segment.
    The current item is the first in-progress todo, falling back to the first
    pending one so a fresh plan still shows what comes next.
    """
    if not state.todos:
        return ""
    counts = plan_counts(state.todos)
    label = f"{PLAN} {counts['completed']}/{counts['total']}"
    item = current_todo(state.todos)
    if item is not None:
        label += f" · {_one_line(item.content)}"
    return label


def jobs_segment(state: ActivityState) -> str:
    """Build the jobs segment: the number of currently running jobs."""
    running = sum(1 for job in state.jobs if job.status == "running")
    if running <= 0:
        return ""
    noun = "job" if running == 1 else "jobs"
    return f"{running} {noun}"


def agents_segment(state: ActivityState) -> str:
    """Build the agents segment: the number of known team members/subagents."""
    count = len(state.agents)
    if count <= 0:
        return ""
    noun = "agent" if count == 1 else "agents"
    return f"{count} {noun}"


class ActivityBar(Horizontal):
    """One-line strip of activity segments, hidden when everything is empty."""

    DEFAULT_CSS = """
    ActivityBar {
        height: 1;
        width: 1fr;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
        overflow: hidden;
    }
    ActivityBar .activity-segment {
        width: auto;
        height: 1;
        padding: 0 1;
    }
    ActivityBar .activity-segment:hover {
        background: $panel;
        color: $text;
    }
    #activity-plan {
        color: $text-accent;
    }
    #activity-jobs,
    #activity-agents {
        color: $text;
        border-left: solid $border;
    }
    """

    class SegmentClicked(Message):
        """Posted when the user clicks the plan, jobs or agents segment."""

        def __init__(self, segment: str) -> None:
            if segment not in SEGMENTS:
                raise ValueError(f"unknown activity segment: {segment!r}")
            super().__init__()
            self.segment = segment

        def __repr__(self) -> str:
            return f"SegmentClicked(segment={self.segment!r})"

    def __init__(self) -> None:
        super().__init__(id="activity-bar")
        self._state = ActivityState()
        self._segments: dict[str, Static] = {
            segment: Static("", id=f"activity-{segment}", classes="activity-segment") for segment in SEGMENTS
        }
        self.display = False

    def compose(self) -> ComposeResult:
        for segment in SEGMENTS:
            yield self._segments[segment]

    def update_state(self, state: ActivityState) -> None:
        """Render state; empty segments and the whole bar become hidden."""
        self._state = state
        rendered = {
            PLAN: plan_segment(state),
            JOBS: jobs_segment(state),
            AGENTS: agents_segment(state),
        }
        for segment, text in rendered.items():
            widget = self._segments[segment]
            widget.update(text)
            widget.display = bool(text)
        self.display = any(rendered.values())

    @property
    def state(self) -> ActivityState:
        """The last state passed to update_state."""
        return self._state

    @property
    def plan_text(self) -> str:
        return plan_segment(self._state)

    @property
    def jobs_text(self) -> str:
        return jobs_segment(self._state)

    @property
    def agents_text(self) -> str:
        return agents_segment(self._state)

    def on_click(self, event: events.Click) -> None:
        """Post SegmentClicked for clicks on a known segment."""
        segment = _SEGMENT_BY_ID.get(getattr(event.widget, "id", ""))
        if segment is None:
            return
        self.post_message(self.SegmentClicked(segment))
        event.stop()


__all__ = [
    "AGENTS",
    "CURRENT_ITEM_LIMIT",
    "JOBS",
    "PLAN",
    "SEGMENTS",
    "ActivityBar",
    "agents_segment",
    "jobs_segment",
    "plan_segment",
]
