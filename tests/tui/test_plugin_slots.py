"""Widget and modal tests for plugin slots, using tiny Textual host apps."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from deeppyro.tui.screens.plugin_panel import PluginPanelScreen, open_plugin_panel
from deeppyro.tui.slots import PANEL, SlotRegistry
from deeppyro.tui.widgets.plugin_slots import (
    PluginPanelHost,
    PluginStatusBar,
    PluginTable,
    open_panel_for,
)


def rendered(widget: Static) -> str:
    content: Any = widget.render()
    if isinstance(content, Text):
        return content.plain
    if isinstance(content, str):
        return content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


class StatusHost(App[Any]):
    def __init__(
        self,
        registry: SlotRegistry | None,
        context_factory: Any = None,
    ) -> None:
        super().__init__()
        self.bar = PluginStatusBar(registry, context_factory)

    def compose(self) -> ComposeResult:
        yield self.bar


async def test_status_bar_renders_items_and_isolates_errors() -> None:
    registry = SlotRegistry()
    registry.add_status_item(lambda context: "alpha:" + context["model"], order=1, id="alpha")
    registry.add_status_item(lambda: "plain", order=2, id="plain")
    registry.add_status_item(lambda context: 1 / 0, order=3, id="broken")
    app = StatusHost(registry, lambda: {"model": "m1"})
    async with app.run_test(size=(100, 10)) as pilot:
        await pilot.pause()
        assert app.bar.plain_text == "alpha:m1  ·  plain  ·  !"
        assert app.bar.display is True
        assert app.bar.registry is registry


async def test_status_bar_hides_when_empty_or_missing_registry() -> None:
    app = StatusHost(None, dict)
    async with app.run_test(size=(100, 10)) as pilot:
        await pilot.pause()
        assert app.bar.plain_text == ""
        assert app.bar.display is False


async def test_status_bar_context_factory_failure_is_ignored() -> None:
    registry = SlotRegistry()
    registry.add_status_item(lambda context: "ok" if context == {} else "unexpected", id="ok")
    app = StatusHost(registry, lambda: (_ for _ in ()).throw(RuntimeError("no context")))
    async with app.run_test(size=(100, 10)) as pilot:
        await pilot.pause()
        assert app.bar.plain_text == "ok"


async def test_status_bar_accepts_rich_text_and_non_string_values() -> None:
    registry = SlotRegistry()
    registry.add_status_item(lambda: Text("rich", style="bold"), order=1, id="rich")
    registry.add_status_item(lambda: 42, order=2, id="number")
    app = StatusHost(registry)
    async with app.run_test(size=(100, 10)) as pilot:
        await pilot.pause()
        assert app.bar.plain_text == "rich  ·  42"


class PanelHostApp(App[Any]):
    def __init__(self, factory: Any, context: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.panel = PluginPanelHost(factory=factory, context=context)

    def compose(self) -> ComposeResult:
        yield self.panel


async def test_panel_host_mounts_widget_with_context() -> None:
    app = PanelHostApp(lambda context: Static("hello " + context["name"]), {"name": "slot"})
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        static = app.panel.query_one(Static)
        assert rendered(static) == "hello slot"


async def test_panel_host_wraps_string_and_empty_result() -> None:
    app = PanelHostApp(lambda: "plain text")
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        assert rendered(app.panel.query_one(Static)) == "plain text"

    empty = PanelHostApp(lambda: None)
    async with empty.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        assert "empty panel" in rendered(empty.panel.query_one(Static))


async def test_panel_host_reports_factory_error_inline() -> None:
    app = PanelHostApp(lambda: (_ for _ in ()).throw(RuntimeError("panic")))
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        error = app.panel.query_one(".plugin-slot-error", Static)
        assert "panic" in rendered(error)
        assert "panel failed" in rendered(error)


async def test_panel_host_reports_mount_failure() -> None:
    class BadWidget(Static):
        def __init__(self) -> None:
            super().__init__("bad")

    app = PanelHostApp(lambda: BadWidget())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        assert app.panel.query_one(Static) is not None


class ScreenHost(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("base")


async def test_panel_screen_shows_title_and_closes_on_escape() -> None:
    registry = SlotRegistry()
    registry.add_panel("Slots", lambda context: Static("panel body"))
    contribution = registry.panels()[0]
    app = ScreenHost()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(PluginPanelScreen(contribution))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PluginPanelScreen)
        assert screen.title_text == "Slots"
        assert screen.contribution is contribution
        assert rendered(screen.query_one("#plugin-panel-title", Static)) == "Slots"
        assert "panel body" in rendered(screen.query_one("#plugin-panel-body Static", Static))
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PluginPanelScreen)


async def test_panel_screen_isolates_factory_errors() -> None:
    app = ScreenHost()
    screen = PluginPanelScreen(factory=lambda context: (_ for _ in ()).throw(RuntimeError("nope")))
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(screen)
        await pilot.pause()
        error = app.screen.query_one(".plugin-slot-error", Static)
        assert "nope" in rendered(error)


async def test_open_plugin_panel_pushes_matching_screen() -> None:
    registry = SlotRegistry()
    registry.add_panel("First", lambda context: Static("one"), order=1)
    registry.add_panel("Second", lambda context: Static("two"), order=2)
    app = ScreenHost()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = open_plugin_panel(app, registry, slot_id="second")
        await pilot.pause()
        assert isinstance(screen, PluginPanelScreen)
        assert isinstance(app.screen, PluginPanelScreen)
        assert app.screen.title_text == "Second"
        assert open_plugin_panel(app, SlotRegistry()) is None


async def test_plugin_table_lists_and_posts_selected() -> None:
    registry = SlotRegistry()
    registry.add_panel("Alpha", lambda context: "a", order=1)
    registry.add_panel("Beta", lambda context: "b", order=2)

    class TableHost(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.table = PluginTable(registry.panels())
            self.selected: list[str] = []

        def compose(self) -> ComposeResult:
            yield self.table

        def on_plugin_table_selected(self, event: PluginTable.Selected) -> None:
            self.selected.append(event.contribution.title)

    app = TableHost()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        assert app.table.option_count == 2
        app.table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected == ["Alpha"]
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.selected == ["Alpha", "Beta"]


def test_open_panel_for_selection_rules() -> None:
    registry = SlotRegistry()
    registry.add_panel("B", lambda context: "b", order=2)
    registry.add_panel("A", lambda context: "a", order=1)
    assert open_panel_for(None) is None
    assert open_panel_for(SlotRegistry()) is None
    assert open_panel_for(registry).title == "A"
    assert open_panel_for(registry, kind=PANEL, slot_id="b") is not None
    assert open_panel_for(registry, kind=PANEL, slot_id="b").title == "B"
    assert open_panel_for(registry, kind=PANEL, slot_id="missing") is None
