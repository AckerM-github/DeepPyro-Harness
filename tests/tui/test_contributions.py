"""External plugin contributions: ui service, screen slots, health rendering."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from textual.app import App
from textual.widgets import DataTable, Static

from deeppyro.api.context import PluginContext
from deeppyro.tui import health
from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.screens.plugins import PluginScreen
from deeppyro.tui.screens.slot_screen import SlotScreen, open_slot_screen
from deeppyro.tui.slots import SCREEN, SlotError, SlotRegistry
from deeppyro.tui.ui import NOTIFY, OPEN_SCREEN, UiAction, UiService
from deeppyro.tui.widgets.plugin_slots import PluginStatusBar
from tests.plugins.support import FakeManager, make_info
from tests.tui.test_app import FakeRuntime, _runtime_with_slots


class _Reflect:
    """Minimal reflection stub exposing named services through get(name)."""

    def __init__(self, services: dict[str, Any]) -> None:
        self.services = services

    def get(self, name: str) -> Any:
        return self.services.get(name)


class _RawContext:
    def __init__(self, services: dict[str, Any]) -> None:
        self.reflect = _Reflect(services)


class _Recorder:
    def __init__(self) -> None:
        self.actions: list[UiAction] = []

    def handle_ui_action(self, action: UiAction) -> None:
        self.actions.append(action)


class _Exploder:
    def handle_ui_action(self, action: UiAction) -> None:
        raise RuntimeError("boom")


class _PluginHost(App[None]):
    """Host app that pushes the plugin manager modal."""

    def __init__(self, screen: PluginScreen) -> None:
        super().__init__()
        self.plugin_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self.plugin_screen)


class _UiKernel:
    def __init__(self, registry: SlotRegistry, service: UiService) -> None:
        self.registry = registry
        self.service_ = service

    def service(self, name: str) -> Any:
        if name == "slots":
            return self.registry
        if name == "ui":
            return self.service_
        raise LookupError(name)


def _runtime_with_ui(registry: SlotRegistry, service: UiService):
    async def factory(app: DeepPyroApp) -> FakeRuntime:
        runtime = FakeRuntime(app)
        runtime.kernel_app = _UiKernel(registry, service)
        return runtime

    return factory


def test_ui_service_headless_is_a_counted_noop() -> None:
    service = UiService()
    assert service.available is False
    assert service.post(NOTIFY, text="hello") is False
    assert service.notify("hello") is False
    assert service.open_screen("demo") is False
    assert service.refresh() is False
    stats = service.stats()
    assert stats["bound"] is False
    assert stats["dropped"] == 4
    assert stats["queued"] == 0


def test_drain_delivers_actions_in_fifo_order() -> None:
    service = UiService()
    backend = _Recorder()
    service.bind(backend)
    for index in range(5):
        assert service.post(NOTIFY, text=str(index)) is True
    assert service.pending == 5
    assert service.drain() == 5
    assert [action.payload["text"] for action in backend.actions] == ["0", "1", "2", "3", "4"]
    assert service.stats()["delivered"] == 5


def test_background_thread_posts_keep_their_order() -> None:
    service = UiService()
    backend = _Recorder()
    service.bind(backend)

    def produce() -> None:
        for index in range(10):
            service.post(OPEN_SCREEN, slot_id=f"s{index}")

    worker = threading.Thread(target=produce)
    worker.start()
    worker.join()
    assert service.pending == 10
    assert service.drain() == 10
    assert [action.payload["slot_id"] for action in backend.actions] == [f"s{i}" for i in range(10)]


def test_drain_isolates_a_broken_action() -> None:
    service = UiService()
    service.bind(_Exploder())
    assert service.post(NOTIFY, text="x") is True
    assert service.drain() == 0
    assert service.stats()["errors"] == 1
    assert "boom" in service.error_messages[0]


def test_plugin_context_ui_falls_back_to_noop() -> None:
    provided = UiService()
    context = PluginContext(_RawContext({"ui": provided}), plugin_id="demo")
    assert context.ui is provided
    bare = PluginContext(_RawContext({}), plugin_id="demo")
    assert bare.ui.available is False
    assert bare.ui.post(NOTIFY, text="x") is False


def test_screen_slots_register_list_and_validate() -> None:
    registry = SlotRegistry()
    dispose = registry.add_screen("hud", title="HUD", renderer=lambda context: Static("hud"))
    screens = registry.screens()
    assert [contribution.id for contribution in screens] == ["hud"]
    contribution = registry.get(SCREEN, "hud")
    assert contribution is not None
    assert contribution.kind == SCREEN
    assert contribution.title == "HUD"
    with pytest.raises(SlotError, match="duplicate"):
        registry.add_screen("hud", renderer=lambda context: Static("again"))
    with pytest.raises(SlotError, match="only registers"):
        registry.add_screen("bad", kind="panel", renderer=lambda context: Static("x"))
    with pytest.raises(SlotError, match="must not be empty"):
        registry.add_screen(" ", renderer=lambda context: Static("x"))
    with pytest.raises(SlotError, match="known:"):
        registry._add(
            "hologram",
            factory=lambda context: None,
            order=100,
            slot_id="bad-kind",
            plugin_id="",
            title="",
            metadata=None,
        )
    dispose()
    assert registry.screens() == []


PLUGIN_TOML = """[package]
id = "bad"
name = "Bad Plugin"
version = "0.1.0"
module = "bad_plugin"
class = "BadPlugin"
api_version = 1

[ui]
slots = ["hologram"]
"""


def test_scan_health_surfaces_unknown_declared_slot(tmp_path: Path) -> None:
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "plugin.toml").write_text(PLUGIN_TOML, encoding="utf-8")
    snapshot = health.scan_health(tmp_path)
    record = snapshot.get("bad")
    assert record is not None
    assert record.healthy is False
    assert record.state == "metadata-invalid"
    messages = [issue.message for issue in record.issues]
    assert any("unknown slot kind" in message for message in messages)
    assert any("hologram" in message for message in messages)
    assert snapshot.failed_count >= 1


async def test_slot_screen_opens_and_renders() -> None:
    registry = SlotRegistry()
    registry.add_screen(
        "hud",
        title="HUD Screen",
        renderer=lambda context: Static(f"cwd={context.get('cwd')}"),
    )
    app = App[None]()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = open_slot_screen(app, registry, slot_id="hud", context={"cwd": "D:/work"})
        assert isinstance(screen, SlotScreen)
        await pilot.pause()
        assert isinstance(app.screen, SlotScreen)
        assert str(app.screen.query_one("#slot-screen-title", Static).content) == "HUD Screen"
        texts = [str(widget.content) for widget in app.screen.query(Static)]
        assert any("cwd=D:/work" in text for text in texts)
        assert open_slot_screen(app, registry, slot_id="missing") is None


async def test_slot_screen_rejects_async_renderer() -> None:
    async def renderer(context: Any) -> Static:
        return Static("late")

    registry = SlotRegistry()
    registry.add_screen("late", title="Late", renderer=renderer)
    app = App[None]()
    async with app.run_test(size=(80, 24)) as pilot:
        open_slot_screen(app, registry, slot_id="late")
        await pilot.pause()
        errors = list(app.screen.query(".plugin-slot-error"))
        assert errors
        assert "synchronous" in str(errors[0].content)


async def test_plugins_screen_shows_failed_health() -> None:
    snapshot = health.HealthSnapshot(
        records=(
            health.PluginHealth(
                "demo",
                version="0.1.0",
                issues=(health.HealthIssue("metadata-invalid", "bad slot", "fix it"),),
            ),
        ),
        errors=("scan failed",),
    )
    screen = PluginScreen(FakeManager([make_info("demo", name="Demo")]), health=snapshot)
    host = _PluginHost(screen)
    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        summary = str(screen.query_one("#plugin-health", Static).content)
        assert "\u5931\u8d25" in summary
        assert "scan failed" in summary
        table = screen.query_one("#plugin-table", DataTable)
        assert "metadata-invalid" in str(table.get_row_at(0)[3])
        await pilot.press("i")
        await pilot.pause()
        detail = str(screen.query_one("#plugin-detail", Static).content)
        assert "fix it" in detail


async def test_plugins_screen_shows_healthy_summary() -> None:
    snapshot = health.HealthSnapshot(records=(health.PluginHealth("demo", version="0.1.0"),))
    screen = PluginScreen(FakeManager([make_info("demo")]), health=snapshot)
    host = _PluginHost(screen)
    async with host.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        summary = str(screen.query_one("#plugin-health", Static).content)
        assert "\u6b63\u5e38" in summary
        assert "\u5931\u8d25" in summary


async def test_app_drains_background_ui_actions() -> None:
    registry = SlotRegistry()
    registry.add_screen("hud", title="Threaded HUD", renderer=lambda context: Static("thread body"))
    service = UiService()
    app = DeepPyroApp(runtime_factory=_runtime_with_ui(registry, service), splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert service.available is True
        worker = threading.Thread(target=lambda: service.open_screen("hud"))
        worker.start()
        worker.join()
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, SlotScreen):
                break
        assert isinstance(app.screen, SlotScreen)
        service.set_status("hud", "online")
        bar = app.query_one(PluginStatusBar)
        for _ in range(40):
            await pilot.pause(0.05)
            if "online" in bar.plain_text:
                break
        assert "online" in bar.plain_text
        assert service.stats()["delivered"] >= 2


async def test_panel_command_opens_screen_slot() -> None:
    registry = SlotRegistry()
    registry.add_screen("hud", title="HUD", renderer=lambda context: Static("panel fallback"))
    app = DeepPyroApp(runtime_factory=_runtime_with_slots(registry), splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.panel_command("hud")
        await pilot.pause()
        assert isinstance(app.screen, SlotScreen)


def test_tui_ui_module_imports_without_textual() -> None:
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; import deeppyro.tui.ui; import deeppyro.tui.health; assert 'textual' not in sys.modules"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


class _KernelContext:
    def __init__(self) -> None:
        self.raw = object()
        self.provided: list[tuple[str, Any]] = []

    def provide(self, name: str, value: Any) -> None:
        self.provided.append((name, value))


def test_slots_plugin_provides_the_ui_service(monkeypatch, tmp_path: Path) -> None:
    from deeppyro.tui.slots import SlotsPlugin

    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path))
    ctx = _KernelContext()
    plugin = SlotsPlugin(ctx, {})
    plugin.setup()
    provided = dict(ctx.provided)
    assert provided["ui"] is plugin.ui
    assert provided["slots"] is plugin.registry
    assert plugin.ui.available is False
