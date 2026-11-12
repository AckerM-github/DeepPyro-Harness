"""Startup splash screen: pixel wordmark with a line-by-line reveal.

The splash is a modal screen so the chat layout can finish mounting and the
runtime can boot underneath it. It dismisses on any key, or by itself shortly
after the reveal animation finishes. Rows are revealed top-to-bottom; the
runtime line is updated by the app once the kernel is ready.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Static

from deeppyro.tui.format import compact_text
from deeppyro.tui.wordmark import (
    FIRE_STOPS,
    color_at,
    compact_wordmark_line,
    wordmark_lines,
)

#: Delay between two revealed rows.
REVEAL_INTERVAL = 0.04
#: Extra time the splash stays visible after the last row.
HOLD_SECONDS = 1.5
#: Below these sizes the big wordmark does not fit and the compact one is used.
FULL_MIN_WIDTH = 72
FULL_MIN_HEIGHT = 24

_SPARK = "✦"
_TAGLINE = "python core · terminal UI"
_HELP = "/help for commands · Ctrl+C interrupts · Ctrl+D quits"
_HINT = "press any key to continue"


def _center_block(rows: list[Text]) -> list[Text]:
    """Pad every row to the widest one so the block centers as a unit."""
    width = max((len(row.plain) for row in rows), default=0)
    centered: list[Text] = []
    for row in rows:
        pad = max(width - len(row.plain), 0)
        left = pad // 2
        line = Text(" " * left)
        line.append_text(row)
        line.append(" " * (pad - left))
        centered.append(line)
    return centered


class SplashScreen(ModalScreen[None]):
    """Startup banner: pixel wordmark, booting runtime and key hints."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "dismiss_splash", "Continue", show=False),
        Binding("enter", "dismiss_splash", "Continue", show=False),
    ]

    def __init__(
        self,
        *,
        version: str = "",
        cwd: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        super().__init__()
        self._version = version
        self._cwd = cwd
        self._provider = provider
        self._model = model
        self._compact = False
        self._body: Static | None = None
        self._lines: list[Text] = []
        self._visible = 0
        self._interval_timer: Any = None
        self._dismissed = False

    # -- composition -----------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static("", id="splash-body")

    def on_mount(self) -> None:
        self._body = self.query_one(Static)
        size = self.app.size
        self._compact = size.width < FULL_MIN_WIDTH or size.height < FULL_MIN_HEIGHT
        self._lines = self._build_lines()
        self._visible = 0
        self._refresh()
        self._interval_timer = self.set_interval(REVEAL_INTERVAL, self._advance)
        self.set_timer(REVEAL_INTERVAL * len(self._lines) + HOLD_SECONDS, self.action_dismiss_splash)

    # -- public API ------------------------------------------------------
    @property
    def compact(self) -> bool:
        return self._compact

    @property
    def line_count(self) -> int:
        return len(self._lines)

    def set_runtime(self, provider: str, model: str, cwd: str | None = None) -> None:
        """Fill in the runtime line once the kernel has booted."""
        self._provider = provider
        self._model = model
        if cwd is not None:
            self._cwd = str(cwd)
        self._lines = self._build_lines()
        self._visible = max(0, min(self._visible, len(self._lines)))
        self._refresh()

    def visible_text(self) -> str:
        """Plain text of the rows revealed so far (used by tests)."""
        return self._compose_visible().plain

    def full_text(self) -> str:
        """Plain text of every row, revealed or not (used by tests)."""
        text = Text()
        for index, line in enumerate(self._lines):
            if index:
                text.append("\n")
            text.append_text(line)
        return text.plain

    # -- reveal ----------------------------------------------------------
    def _advance(self) -> None:
        if self._visible >= len(self._lines):
            stop = getattr(self._interval_timer, "stop", None)
            if stop is not None:
                stop()
            return
        self._visible += 1
        self._refresh()

    def _refresh(self) -> None:
        if self._body is None:
            return
        self._body.update(self._compose_visible())

    def _compose_visible(self) -> Text:
        text = Text()
        for index in range(min(self._visible, len(self._lines))):
            if index:
                text.append("\n")
            text.append_text(self._lines[index])
        return text

    # -- content ---------------------------------------------------------
    def _build_lines(self) -> list[Text]:
        provider = self._provider.strip()
        model = self._model.strip()
        if provider and model:
            runtime_line = f"{provider} · {model}"
        elif provider:
            runtime_line = provider
        else:
            runtime_line = "loading runtime…"
        cwd = compact_text(self._cwd, max_lines=1, max_chars=58) or self._cwd
        if self._version:
            cwd_line = f"{cwd} · v{self._version}" if cwd else f"v{self._version}"
        else:
            cwd_line = cwd
        spark_style = "bold " + color_at(FIRE_STOPS, 1.0)
        rows: list[Text] = [Text(_SPARK, style=spark_style)]
        if self._compact:
            rows.append(compact_wordmark_line())
        else:
            rows.extend(wordmark_lines())
        rows.append(Text(""))
        rows.append(Text(_TAGLINE, style="bold #7aa2f7"))
        rows.append(Text(""))
        rows.append(Text(runtime_line, style="dim"))
        if cwd_line:
            rows.append(Text(cwd_line, style="dim"))
        rows.append(Text(""))
        rows.append(Text(_HINT, style="italic #e0af68"))
        rows.append(Text(_HELP, style="dim"))
        return _center_block(rows)

    # -- dismissal -------------------------------------------------------
    def action_dismiss_splash(self) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        stop = getattr(self._interval_timer, "stop", None)
        if stop is not None:
            stop()
        self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        self.action_dismiss_splash()
        event.stop()


__all__ = [
    "FULL_MIN_HEIGHT",
    "FULL_MIN_WIDTH",
    "HOLD_SECONDS",
    "REVEAL_INTERVAL",
    "SplashScreen",
]
