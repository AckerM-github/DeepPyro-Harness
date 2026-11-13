"""Modal screen hosting one plugin panel or settings page.

The screen is intentionally thin: it draws the dialog chrome (title, border,
Escape binding) and delegates the body to
:class:`~deeppyro.tui.widgets.plugin_slots.PluginPanelHost`, which isolates
factory errors. The host opens it from a command, a `PluginTable.Selected`
message, or a key binding; it returns `None` on close.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from deeppyro.tui.slots import PANEL, SlotContribution, SlotRegistry
from deeppyro.tui.widgets.plugin_slots import PluginPanelHost, open_panel_for


class PluginPanelScreen(ModalScreen[None]):
    """Centered modal rendering a single slot contribution.

    Args:
        contribution: Panel/settings contribution whose factory is rendered.
        factory: Explicit factory, used when constructing an ad-hoc panel.
        title: Override for the dialog title; defaults to the contribution title.
        context: Mapping passed to the factory (see `build_slot_context`).
    """

    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "close", "Close", show=False)]

    DEFAULT_CSS = """
    PluginPanelScreen {
        align: center middle;
        background: $background 70%;
    }
    #plugin-panel-dialog {
        width: 88;
        height: auto;
        max-height: 28;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #plugin-panel-title {
        color: $accent;
        text-style: bold;
    }
    #plugin-panel-body {
        height: auto;
        max-height: 1fr;
        margin: 1 0 0 0;
    }
    """

    def __init__(
        self,
        contribution: SlotContribution | None = None,
        *,
        factory: Callable[..., Any] | None = None,
        title: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        if contribution is None and factory is None:
            raise ValueError("PluginPanelScreen needs a contribution or a factory")
        self._contribution = contribution
        self._factory = factory if factory is not None else contribution.factory
        self._title = title or (
            (contribution.title or contribution.id) if contribution is not None else "Plugin panel"
        )
        self._slot_context = dict(context or {})

    @property
    def contribution(self) -> SlotContribution | None:
        """The contribution rendered by this screen, if any."""
        return self._contribution

    @property
    def title_text(self) -> str:
        """The dialog title currently displayed."""
        return self._title

    def compose(self) -> ComposeResult:
        with Vertical(id="plugin-panel-dialog"):
            yield Static(self._title, id="plugin-panel-title")
            yield PluginPanelHost(
                self._contribution,
                factory=self._factory,
                context=self._slot_context,
            )

    def action_close(self) -> None:
        """Close the modal without a result."""
        self.dismiss(None)


def open_plugin_panel(
    app: Any,
    registry: SlotRegistry | None,
    *,
    kind: str = PANEL,
    slot_id: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> PluginPanelScreen | None:
    """Push the first (or named) panel contribution onto the app's screen stack.

    Returns the screen, or None when no matching contribution exists. The
    caller is responsible for building the context; `build_slot_context(app)`
    is the usual choice.
    """
    contribution = open_panel_for(registry, kind=kind, slot_id=slot_id)
    if contribution is None:
        return None
    screen = PluginPanelScreen(contribution, context=context)
    app.push_screen(screen)
    return screen


__all__ = ["PluginPanelScreen", "open_plugin_panel"]
