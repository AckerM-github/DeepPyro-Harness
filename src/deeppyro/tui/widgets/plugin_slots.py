"""Host widgets that render plugin slot contributions.

The widgets here are passive: they receive a :class:`~deeppyro.tui.slots.SlotRegistry`
and a context factory, then render whatever the plugins registered. Every
callback is isolated: a broken status renderer becomes a dim `!`, a broken
panel becomes an inline error line, and a broken conversation row is reported
without escaping into the Textual message loop.

Integration (see docs/spec/tui-slots.md for the full checklist):

* `PluginStatusBar` - one extra status line below the core status bar.
* `PluginPanelHost` - mounts one panel/settings factory; used by the modal.
* `PluginTable` - selectable list of panel/settings contributions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from rich.text import Text
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from deeppyro.tui.slots import (
    PANEL,
    SETTINGS,
    STATUS,
    SlotContribution,
    SlotRegistry,
    call_slot_factory,
)

STATUS_SEPARATOR = "  ·  "


class PluginStatusBar(Static):
    """A single line rendering every `status` slot item.

    Each renderer receives the host context mapping built by
    `deeppyro.tui.slots.build_slot_context` (or any mapping the host passes to
    :meth:`refresh_items`). Renderers return a plain string or Rich
    `Text`; exceptions are rendered as a dim `!` so one plugin cannot blank
    the whole line.
    """

    DEFAULT_CSS = """
    PluginStatusBar {
        height: 1;
        padding: 0 2;
        background: $surface;
        color: $text-muted;
        text-wrap: nowrap;
        overflow: hidden;
    }
    """

    def __init__(
        self,
        registry: SlotRegistry | None = None,
        context_factory: Callable[[], Mapping[str, Any]] | None = None,
        *,
        id: str = "plugin-status-bar",
    ) -> None:
        super().__init__("", id=id)
        self._registry = registry
        self._context_factory = context_factory
        self._plain = ""
        self.display = False

    def on_mount(self) -> None:
        self.refresh_items()

    # -- binding ---------------------------------------------------------
    def bind(
        self,
        registry: SlotRegistry | None,
        context_factory: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        """Attach the registry and optional context provider."""
        self._registry = registry
        self._context_factory = context_factory
        self.refresh_items()

    @property
    def registry(self) -> SlotRegistry | None:
        """The currently bound registry, if any."""
        return self._registry

    # -- rendering -------------------------------------------------------
    def refresh_items(self, context: Mapping[str, Any] | None = None) -> None:
        """Re-render all status items, optionally with an explicit context."""
        if context is None:
            context = self._resolve_context()
        line = self.render_text(context)
        self._plain = line.plain
        self.update(line)
        self.display = bool(line.plain)

    def render_text(self, context: Mapping[str, Any] | None = None) -> Text:
        """Build the status line without touching widget state (test seam)."""
        parts: list[Text] = []
        registry = self._registry
        if registry is None:
            return Text()
        payload = dict(context or {})
        for contribution in registry.list(STATUS):
            try:
                value = call_slot_factory(contribution.factory, payload)
                parts.append(_to_text(value))
            except Exception:  # noqa: BLE001 - isolate one broken plugin
                parts.append(Text("!", style="dim"))
        if not parts:
            return Text()
        line = Text()
        for index, part in enumerate(parts):
            if index:
                line.append(STATUS_SEPARATOR, style="dim")
            line.append_text(part)
        return line

    @property
    def plain_text(self) -> str:
        """Last rendered line as plain text (used by tests and diagnostics)."""
        return self._plain

    def _resolve_context(self) -> Mapping[str, Any]:
        factory = self._context_factory
        if not callable(factory):
            return {}
        try:
            return factory() or {}
        except Exception:  # noqa: BLE001 - context is best effort
            return {}


def _to_text(value: Any) -> Text:
    if isinstance(value, Text):
        return value
    if value is None:
        return Text()
    if isinstance(value, str):
        return Text(value)
    return Text(str(value))


class PluginPanelHost(Vertical):
    """Mount one panel or settings-page factory, isolating its errors.

    The factory is called with the host context when it accepts an argument
    (see `call_slot_factory`), and may return a single Textual widget, a
    string/Rich Text (wrapped in `Static`), or an iterable of widgets.
    """

    DEFAULT_CSS = """
    PluginPanelHost {
        height: auto;
        width: 1fr;
    }
    PluginPanelHost .plugin-slot-error {
        color: $text-error;
        height: auto;
    }
    """

    def __init__(
        self,
        contribution: SlotContribution | None = None,
        *,
        factory: Callable[..., Any] | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(id="plugin-panel-body")
        self._contribution = contribution
        self._factory = factory if factory is not None else (contribution.factory if contribution else None)
        # NOTE: do not call this attribute ``_context``; Textual's MessagePump
        # defines a ``_context()`` method used while processing messages.
        self._slot_context = dict(context or {})

    @property
    def contribution(self) -> SlotContribution | None:
        """The slot this host renders, when constructed from one."""
        return self._contribution

    def on_mount(self) -> None:
        self.load()

    def load(self, context: Mapping[str, Any] | None = None) -> None:
        """Clear and mount the factory result (or an inline error line)."""
        if context is not None:
            self._slot_context = dict(context)
        self.remove_children()
        factory = self._factory
        if not callable(factory):
            self.mount(_error_widget("panel has no factory"))
            return
        try:
            result = call_slot_factory(factory, dict(self._slot_context))
        except Exception as exc:  # noqa: BLE001 - a panel must not crash the TUI
            self.mount(_error_widget(f"panel failed: {type(exc).__name__}: {exc}"))
            return
        try:
            self._mount_result(result)
        except Exception as exc:  # noqa: BLE001
            self.mount(_error_widget(f"panel failed: {type(exc).__name__}: {exc}"))

    def _mount_result(self, result: Any) -> None:
        if result is None:
            self.mount(Static(Text("empty panel", style="dim")))
            return
        if isinstance(result, (str, Text)):
            self.mount(Static(_to_text(result)))
            return
        if isinstance(result, Widget):
            self.mount(result)
            return
        if isinstance(result, Iterable):
            widgets = [item for item in result if isinstance(item, Widget)]
            if widgets:
                self.mount(*widgets)
                return
        self.mount(Static(Text(f"unsupported panel result: {type(result).__name__}", style="dim")))


def _error_widget(message: str) -> Static:
    return Static(Text(message), classes="plugin-slot-error")


class PluginTable(OptionList):
    """Selectable list of panel or settings contributions.

    Selecting an option posts :class:`PluginTable.Selected`; the host decides
    whether to open `PluginPanelScreen`. The table is intentionally generic so
    one widget serves both panels and settings pages.
    """

    DEFAULT_CSS = """
    PluginTable {
        height: auto;
        max-height: 18;
    }
    """

    class Selected(Message):
        """Posted when the user selects one contribution."""

        def __init__(self, contribution: SlotContribution) -> None:
            super().__init__()
            self.contribution = contribution

    def __init__(
        self,
        contributions: Iterable[SlotContribution] = (),
        *,
        kind: str = PANEL,
        id: str = "plugin-table",
    ) -> None:
        super().__init__(id=id)
        self._kind = kind if kind in (PANEL, SETTINGS) else PANEL
        self._by_option_id: dict[str, SlotContribution] = {}
        self._contributions = list(contributions)

    @property
    def kind(self) -> str:
        """Which slot kind this table displays (`panel` or `settings`)."""
        return self._kind

    def on_mount(self) -> None:
        self.set_contributions(self._contributions)

    def set_contributions(self, contributions: Iterable[SlotContribution]) -> None:
        """Replace the listed contributions, preserving order."""
        self._contributions = [item for item in contributions if item.kind == self._kind]
        options: list[Option] = []
        mapping: dict[str, SlotContribution] = {}
        for index, contribution in enumerate(self._contributions):
            option_id = f"slot-{contribution.kind}-{index}"
            mapping[option_id] = contribution
            label = contribution.title or contribution.id
            options.append(Option(label, id=option_id))
        self._by_option_id = mapping
        self.set_options(options)
        if options:
            self.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        contribution = self._by_option_id.get(str(event.option.id or ""))
        if contribution is not None:
            self.post_message(self.Selected(contribution))


def open_panel_for(
    registry: SlotRegistry | None,
    *,
    kind: str = PANEL,
    slot_id: str | None = None,
) -> SlotContribution | None:
    """Pick a panel contribution, preferring an explicit id."""
    if registry is None:
        return None
    contributions = registry.list(kind)
    if slot_id is None:
        return contributions[0] if contributions else None
    for item in contributions:
        if item.id == slot_id:
            return item
    return None


__all__ = [
    "PluginPanelHost",
    "PluginStatusBar",
    "PluginTable",
    "open_panel_for",
]
