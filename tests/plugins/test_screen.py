"""Rendering and interaction tests for the plugin management modal."""

from __future__ import annotations

from textual.app import App
from textual.widgets import DataTable, Static

from deeppyro.plugins import PluginError
from deeppyro.tui.screens.plugins import PluginScreen
from tests.plugins.support import FakeManager, make_info


class _HostApp(App[None]):
    """Host app that pushes the plugin screen and records its dismissal."""

    def __init__(self, screen: PluginScreen) -> None:
        super().__init__()
        self.plugin_screen = screen
        self.dismissed: list[str | None] = []

    def on_mount(self) -> None:
        self.push_screen(self.plugin_screen, self.dismissed.append)


def _table(screen: PluginScreen) -> DataTable:
    return screen.query_one("#plugin-table", DataTable)


def _status(screen: PluginScreen, row: int) -> str:
    return str(_table(screen).get_row_at(row)[3])


def _detail(screen: PluginScreen) -> str:
    return str(screen.query_one("#plugin-detail", Static).content)


def _notifications(app: App[None]) -> list[str]:
    return [str(notification.message) for notification in app._notifications]


def test_manager_can_be_injected() -> None:
    manager = FakeManager()
    screen = PluginScreen(manager)
    assert screen.manager is manager


def test_constructor_builds_default_manager(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr("deeppyro.plugins.manager.PluginManager", lambda: sentinel)
    screen = PluginScreen()
    assert screen.manager is sentinel


async def test_renders_plugin_table() -> None:
    manager = FakeManager(
        [
            make_info("demo", name="Demo Plugin", enabled=False),
            make_info("switched", name="Switched Plugin", enabled=True),
        ]
    )
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        table = _table(screen)
        assert table.row_count == 2
        labels = [str(column.label) for column in table.columns.values()]
        assert labels == ["名称", "版本", "来源", "状态", "说明"]
        first = table.get_row_at(0)
        assert str(first[0]) == "Demo Plugin"
        assert str(first[1]) == "0.1.0"
        assert str(first[2]) == "插件目录"
        assert str(first[3]) == "禁用"
        assert str(first[4]) == "a demo plugin"
        assert _status(screen, 1) == "启用"


async def test_broken_row_is_reported() -> None:
    manager = FakeManager([make_info("broken", error="invalid plugin.toml")])
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert _status(screen, 0) == "错误"
        assert "invalid plugin.toml" in str(_table(screen).get_row_at(0)[4])


async def test_enable_selected_plugin() -> None:
    manager = FakeManager([make_info("demo", name="Demo Plugin", enabled=False)])
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert manager.calls == [("enable", "demo")]
        assert _status(screen, 0) == "启用"
        assert "demo" in _detail(screen)
        assert any("已启用" in message for message in _notifications(host))


async def test_disable_selected_plugin() -> None:
    manager = FakeManager([make_info("demo", name="Demo Plugin", enabled=True)])
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert manager.calls == [("disable", "demo")]
        assert _status(screen, 0) == "禁用"
        assert any("已禁用" in message for message in _notifications(host))


async def test_refresh_reloads_the_table() -> None:
    manager = FakeManager(
        [make_info("demo")],
        refresh_rows=[make_info("demo"), make_info("fresh", name="Fresh Plugin")],
    )
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert _table(screen).row_count == 1
        await pilot.press("r")
        await pilot.pause()
        assert ("refresh", None) in manager.calls
        assert _table(screen).row_count == 2
        assert any("刷新" in message for message in _notifications(host))


async def test_refresh_error_is_notified() -> None:
    manager = FakeManager([make_info("demo")], failures={"refresh": RuntimeError("refresh boom")})
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(host.screen, PluginScreen)
        messages = _notifications(host)
        assert any("刷新失败" in message and "refresh boom" in message for message in messages)


async def test_details_shows_validation_result() -> None:
    manager = FakeManager(
        [
            make_info(
                "demo",
                name="Demo Plugin",
                provides=("demo",),
                target="demo_plugin:DemoPlugin",
            )
        ]
    )
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert ("validate", "demo") in manager.calls
        detail = _detail(screen)
        assert "校验: 通过" in detail
        assert "demo_plugin:DemoPlugin" in detail
        assert "提供: demo" in detail


async def test_details_failure_is_notified() -> None:
    manager = FakeManager([make_info("demo")], failures={"validate": RuntimeError("bad target")})
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        assert isinstance(host.screen, PluginScreen)
        messages = _notifications(host)
        assert any("校验失败" in message and "bad target" in message for message in messages)


async def test_enable_failure_is_notified() -> None:
    manager = FakeManager(
        [make_info("demo", name="Demo Plugin", enabled=False)],
        failures={"enable": PluginError("cannot enable plugin 'demo': boom")},
    )
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(host.screen, PluginScreen)
        assert _status(screen, 0) == "禁用"
        assert any("cannot enable" in message for message in _notifications(host))


async def test_empty_list_shows_hint_and_warns_on_enable() -> None:
    manager = FakeManager()
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert _table(screen).row_count == 0
        assert "没有发现外部插件" in _detail(screen)
        await pilot.press("e")
        await pilot.pause()
        assert manager.calls == []
        assert any("没有可操作的插件" in message for message in _notifications(host))


async def test_escape_dismisses_with_none() -> None:
    manager = FakeManager([make_info("demo")])
    screen = PluginScreen(manager)
    host = _HostApp(screen)

    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        assert isinstance(host.screen, PluginScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert host.dismissed == [None]
        assert not isinstance(host.screen, PluginScreen)
