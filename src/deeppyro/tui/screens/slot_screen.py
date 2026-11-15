"""Modal screen hosting one plugin `screen` contribution.

Screens are full-window plugin UI: unlike panels, they borrow the whole app
surface and therefore carry stricter rules. The renderer receives the host
snapshot mapping and must return a Textual widget (or an iterable of widgets)
synchronously; it must not perform IO or touch the host widget tree.

The host isolates renderer failures so one broken plugin can never take down
the app, and it rejects coroutine results explicitly instead of leaking an
un-awaited coroutine warning.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from deeppyro.tui.slots import SCREEN, SlotContribution, SlotError, SlotRegistry, call_slot_factory
from deeppyro.tui.widgets.plugin_slots import PluginPanelHost, open_panel_for


class SlotScreen(ModalScreen[Any]):
    """Full-window modal rendering a single `screen` slot contribution.

    Args:
        contribution: Screen contribution whose renderer is displayed.
        title: Override for the dialog title; defaults to the contribution title.
        context: Mapping passed to the renderer (see `build_slot_context`).
    """

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "close", "Close", show=False)]

    DEFAULT_CSS = """
    SlotScreen {
        align: center middle;
        background: $background 70%;
    }
    #slot-screen-dialog {
        width: 96%;
        height: 92%;
        max-height: 40;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #slot-screen-title {
        color: $accent;
        text-style: bold;
    }
    #slot-screen-body {
        height: 1fr;
        margin: 1 0 0 0;
    }
    """

    def __init__(
        self,
        contribution: SlotContribution | None = None,
        *,
        title: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        if contribution is None:
            raise ValueError("SlotScreen needs a screen contribution")
        self._contribution = contribution
        self._title = title or contribution.title or contribution.id
        self._slot_context = dict(context or {})

    @property
    def contribution(self) -> SlotContribution:
        """The screen contribution rendered by this modal."""
        return self._contribution

    @property
    def title_text(self) -> str:
        """The dialog title currently displayed."""
        return self._title

    def compose(self) -> ComposeResult:
        with Vertical(id="slot-screen-dialog"):
            yield Static(self._title, id="slot-screen-title")
            yield PluginPanelHost(
                self._contribution,
                factory=self._render_contribution,
                context=self._slot_context,
            )

    def _render_contribution(self, context: Mapping[str, Any]) -> Any:
        result = call_slot_factory(self._contribution.factory, dict(context))
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            raise SlotError("screen renderer must be synchronous (no async/await)")
        return result

    def action_close(self) -> None:
        """Close the modal without a result."""
        self.dismiss(None)


def open_slot_screen(
    app: Any,
    registry: SlotRegistry | None,
    *,
    slot_id: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> SlotScreen | None:
    """Push the first (or named) `screen` contribution onto the app stack.

    Returns the screen, or ``None`` when no matching contribution exists.
    """
    contribution = open_panel_for(registry, kind=SCREEN, slot_id=slot_id)
    if contribution is None:
        return None
    screen = SlotScreen(contribution, context=context)
    app.push_screen(screen)
    return screen


__all__ = ["SlotScreen", "open_slot_screen"]
