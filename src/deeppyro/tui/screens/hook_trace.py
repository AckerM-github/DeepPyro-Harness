"""Read-only viewer for the recent hook decisions of one session.

The hooks bus records one invoked record before a hook runs and one result
record when it returns (see docs/plan/p2-bus.md). The session query pairs them
into rows shaped like {"hook", "mode", "invoked", "result"} and this modal
renders one line per pair: hook name, mode, ok/error/pending, the duration and
either the error or a short preview of the returned value.

The screen is pure presentation: callers pass the already paired rows, it never
touches the session log itself, and Escape closes it returning None like the
other read-only modals. Styling lives in DEFAULT_CSS so no theme edit is
required.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

TITLE = "Hook trace"
EMPTY_TEXT = "no hook activity yet"
HINT_TEXT = "Esc to close"
#: Longest error/preview fragment kept in one rendered line.
DETAIL_LIMIT = 120

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_PENDING = "pending"

#: One colour per status, used for the whole trace line.
STATUS_STYLES: dict[str, str] = {
    STATUS_OK: "green",
    STATUS_ERROR: "red",
    STATUS_PENDING: "yellow",
}


def _field(row: Mapping[str, Any], name: str) -> Any:
    """Read one field from a paired row, tolerating odd inputs."""
    try:
        return row.get(name)
    except Exception:  # noqa: BLE001 - a bad row must not break the viewer
        return None


def _invoked(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The invoked record of a row, when the pairing kept one."""
    invoked = _field(row, "invoked")
    if isinstance(invoked, Mapping):
        return invoked
    return {}


def _result(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The result record of a row, or None while the hook is still pending.

    Flat rows (a single record that already carries ok/durationMs/preview)
    are accepted as their own result, so /hooks keeps working if a caller
    hands over unpaired records.
    """
    result = _field(row, "result")
    if isinstance(result, Mapping):
        return result
    if result is None and "ok" in row:
        return row
    return None


def hook_name(row: Mapping[str, Any]) -> str:
    """Hook name of a paired row, falling back to the invoked record."""
    name = _field(row, "hook")
    if not name:
        name = _invoked(row).get("hook")
    return str(name or "?")


def hook_mode(row: Mapping[str, Any]) -> str:
    """Mode of a paired row, falling back to the invoked record."""
    mode = _field(row, "mode")
    if not mode:
        mode = _invoked(row).get("mode")
    return str(mode or "?")


def hook_status(row: Mapping[str, Any]) -> str:
    """One of STATUS_OK, STATUS_ERROR or STATUS_PENDING."""
    result = _result(row)
    if result is None:
        return STATUS_PENDING
    ok = result.get("ok")
    if ok is None:
        return STATUS_PENDING
    return STATUS_OK if ok else STATUS_ERROR


def hook_duration(row: Mapping[str, Any]) -> int | None:
    """Duration in milliseconds, when the result recorded one."""
    result = _result(row)
    value = result.get("durationMs") if result is not None else None
    if value is None:
        value = _field(row, "durationMs")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(text: str) -> str:
    """One line only: newlines and tabs become single spaces."""
    return " ".join(text.replace("\r", " ").replace("\n", " ").replace("\t", " ").split())


def _truncate(text: str) -> str:
    """Flatten and clip a detail fragment to DETAIL_LIMIT characters."""
    clean = _clean(text)
    if len(clean) <= DETAIL_LIMIT:
        return clean
    return clean[: DETAIL_LIMIT - 1].rstrip() + "…"


def hook_detail(row: Mapping[str, Any]) -> str:
    """Error or preview fragment of a row, flattened and clipped."""
    result = _result(row)
    if result is not None:
        error = result.get("error")
        if isinstance(error, Mapping):
            name = str(error.get("name") or "")
            message = str(error.get("message") or "")
            if name and message:
                return _truncate(f"{name}: {message}")
            return _truncate(name or message)
        preview = result.get("preview")
        if preview is not None:
            return _truncate(str(preview))
    preview = _field(row, "preview")
    if preview is not None:
        return _truncate(str(preview))
    return ""


def hook_trace_line(row: Mapping[str, Any]) -> str:
    """One human readable line for a paired hook record."""
    parts = [hook_name(row), hook_mode(row), hook_status(row)]
    duration = hook_duration(row)
    if duration is not None:
        parts.append(f"{duration}ms")
    detail = hook_detail(row)
    if detail:
        parts.append(detail)
    return "  ".join(parts)


class HookTraceScreen(ModalScreen[None]):
    """Scrollable viewer for the recent hook decisions of a session."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    HookTraceScreen {
        align: center middle;
        background: $background 70%;
    }
    #hooks-dialog {
        width: 90%;
        height: 80%;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #hooks-title {
        height: 1;
        color: $accent;
        text-style: bold;
    }
    #hooks-body {
        height: 1fr;
        margin: 1 0 0 0;
    }
    .hook-row {
        color: $text;
    }
    #hooks-empty {
        color: $text-muted;
    }
    #hooks-hint {
        height: 1;
        color: $text-muted;
        text-style: italic;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, rows: Iterable[Mapping[str, Any]] = ()) -> None:
        super().__init__()
        self._entries: list[dict[str, Any]] = [dict(row) for row in rows if isinstance(row, Mapping)]

    @property
    def entries(self) -> list[dict[str, Any]]:
        """The paired rows displayed by this screen, in order."""
        return [dict(entry) for entry in self._entries]

    @property
    def rows(self) -> list[str]:
        """The rendered line for every displayed row."""
        return [hook_trace_line(entry) for entry in self._entries]

    @property
    def empty(self) -> bool:
        """True when the session has no hook activity yet."""
        return not self._entries

    def compose(self) -> ComposeResult:
        with Vertical(id="hooks-dialog"):
            yield Static(TITLE, id="hooks-title", markup=False)
            with VerticalScroll(id="hooks-body"):
                if not self._entries:
                    yield Static(EMPTY_TEXT, id="hooks-empty", markup=False)
                for index, entry in enumerate(self._entries):
                    style = STATUS_STYLES.get(hook_status(entry), "")
                    yield Static(
                        Text(hook_trace_line(entry), style=style),
                        id=f"hook-row-{index}",
                        classes="hook-row",
                        markup=False,
                    )
            yield Static(HINT_TEXT, id="hooks-hint", markup=False)

    def on_mount(self) -> None:
        # The focus is optional chrome: a screen that is dismissed before its
        # children mount must not fail, and a missing body is harmless.
        try:
            self.query_one("#hooks-body", VerticalScroll).focus()
        except Exception:  # noqa: BLE001, S110 - optional chrome never blocks rendering
            pass

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = [
    "DETAIL_LIMIT",
    "EMPTY_TEXT",
    "HINT_TEXT",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_PENDING",
    "STATUS_STYLES",
    "TITLE",
    "HookTraceScreen",
    "hook_detail",
    "hook_duration",
    "hook_mode",
    "hook_name",
    "hook_status",
    "hook_trace_line",
]
