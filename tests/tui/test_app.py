"""Smoke tests for the Textual application with an injected fake runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.widgets.control_bar import CUSTOM_WORKSPACE, ControlBar
from deeppyro.tui.widgets.conversation import ConversationView
from deeppyro.tui.widgets.prompt_area import PromptArea
from deeppyro.tui.widgets.status_bar import StatusBar


class FakeSession:
    session_id = "session-11112222-3333-4444-5555-666677778888"

    def __init__(self) -> None:
        self.records: list[Any] = []

    def derive_messages(self) -> list[Any]:
        return []

    def usage(self) -> dict[str, int]:
        return {"input": 12, "output": 3, "cached": 0}

    def turn_count(self) -> int:
        return 0


class FakePermission:
    def __init__(self) -> None:
        self.preset = "workspace-write"
        self.approval = "ask"

    def set_preset(self, preset: str) -> None:
        self.preset = preset

    def set_approval(self, approval: str) -> None:
        self.approval = approval


class FakeCompactor:
    async def compact(self, session: Any, messages: Any = None) -> bool:
        return True


class FakeAgent:
    def __init__(self, sink: Any) -> None:
        self.sink = sink
        self.busy = False
        self.interrupted = False
        self.compactor = FakeCompactor()

    async def send(self, text: str) -> str:
        self.busy = True
        try:
            self.sink.emit({"type": "status", "phase": "thinking"})
            self.sink.emit({"type": "thinking", "text": "pondering"})
            self.sink.emit({"type": "text", "text": "hello "})
            self.sink.emit({"type": "text", "text": "world"})
            self.sink.emit({"type": "usage", "input": 12, "output": 3, "cached": 0})
            self.sink.emit({"type": "final", "text": "hello world"})
        finally:
            self.busy = False
        return "hello world"

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeSessionInfo:
    def __init__(self, session_id: str, title: str, cwd: str) -> None:
        self.id = session_id
        self.title = title
        self.cwd = cwd


class FakePreset:
    def __init__(self, preset_id: str, name: str, description: str = "") -> None:
        self.id = preset_id
        self.name = name
        self.description = description


class FakeRuntime:
    def __init__(self, app: DeepPyroApp) -> None:
        self.sink = app.sink
        self.cwd = Path("D:/work")
        self.provider_name = "fake"
        self.model = "fake-1"
        self.context_window = 8192
        self.permission = FakePermission()
        self.session = FakeSession()
        self.agent = FakeAgent(self.sink)
        self.kernel_app = None
        self.closed = False
        self.opened_sessions: list[str] = []
        self.agent_preset = "default"

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def busy(self) -> bool:
        return self.agent.busy

    async def new_session(self) -> Any:
        self.session = FakeSession()
        self.agent = FakeAgent(self.sink)
        return self.session

    async def open_session(self, session_id: str) -> Any:
        self.opened_sessions.append(session_id)
        return self.session

    async def set_model(self, model_id: str) -> None:
        self.model = model_id

    def models(self) -> list[str]:
        return ["fake-1", "fake-2"]

    def presets(self) -> list[Any]:
        return [FakePreset("default", "Default"), FakePreset("reviewer", "Reviewer", "review only")]

    async def set_agent_preset(self, preset: str) -> str:
        self.agent_preset = preset
        return preset

    def known_workspaces(self, limit: int = 12) -> list[str]:
        workspaces = [str(Path("D:/work")), str(Path("D:/other"))]
        return workspaces[:limit]

    async def set_preset(self, preset: str) -> None:
        self.permission.set_preset(preset)

    async def set_approval(self, approval: str) -> None:
        self.permission.set_approval(approval)

    async def set_workspace(self, path: Any) -> Path:
        workspace = Path(path)
        if workspace != self.cwd:
            self.cwd = workspace
        return workspace

    def recent_sessions(self, limit: int = 20) -> list[Any]:
        return [
            FakeSessionInfo("session-aaaa1111", "fix tests", "D:/work"),
            FakeSessionInfo("session-bbbb2222", "tui polish", "D:/work"),
        ]

    def session_usage(self) -> dict[str, int]:
        return {"input": 12, "output": 3, "cached": 0}

    async def compact_now(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


async def _make_runtime(app: DeepPyroApp) -> FakeRuntime:
    return FakeRuntime(app)


async def test_app_boots_and_streams_a_turn() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.runtime, FakeRuntime)
        prompt = app.query_one(PromptArea)
        prompt.text = "hi"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.sink.final_text == "hello world"
        assert app.busy is False
        assert app.sub_title == "fake/fake-1"
        status = app.query_one(StatusBar)
        assert status is not None


async def test_slash_command_help_renders() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        before = len(conversation.children)
        prompt = app.query_one(PromptArea)
        prompt.text = "/help"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert len(conversation.children) > before
        assert app.busy is False


async def test_unknown_command_reports_error() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/nope"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.busy is False


async def test_new_session_command_resets_conversation() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        prompt = app.query_one(PromptArea)
        prompt.text = "/new"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.runtime, FakeRuntime)
        assert len(conversation.children) > 0


async def test_resume_without_argument_uses_picker(monkeypatch: Any) -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        calls: list[tuple[str, list[tuple[str, str, str]]]] = []

        async def fake_pick(title: str, items: Any) -> str:
            calls.append((title, list(items)))
            return next(iter(items))[0]

        monkeypatch.setattr(app, "_pick", fake_pick)
        await app.resume("")
        assert calls and calls[0][0] == "Resume session"
        assert app.runtime.opened_sessions == ["session-aaaa1111"]


async def test_model_without_argument_uses_picker(monkeypatch: Any) -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        async def fake_pick(title: str, items: Any) -> str:
            assert title == "Select model"
            return "fake-2"

        monkeypatch.setattr(app, "_pick", fake_pick)
        await app.model_command("")
        assert app.runtime.model == "fake-2"


async def test_preset_command_switches_the_agent_preset() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/preset reviewer"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.runtime.agent_preset == "reviewer"


async def test_preset_without_argument_uses_picker(monkeypatch: Any) -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        async def fake_pick(title: str, items: Any) -> str:
            assert title == "Select agent preset"
            return "reviewer"

        monkeypatch.setattr(app, "_pick", fake_pick)
        await app.preset_command("")
        assert app.runtime.agent_preset == "reviewer"


async def test_ctrl_c_clears_the_composer_when_idle() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "draft text"
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert prompt.text == ""
        assert app.is_running


async def test_plugins_command_opens_the_plugin_manager() -> None:
    from deeppyro.tui.screens.plugins import PluginScreen

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/plugins"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, PluginScreen):
                break
        assert isinstance(app.screen, PluginScreen)
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, PluginScreen)


async def test_splash_screen_shows_until_any_key() -> None:
    from deeppyro.tui.screens.splash import SplashScreen

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=True)
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause(0.15)
        assert isinstance(app.screen, SplashScreen)
        splash = app.screen
        # The runtime line is filled in once the fake runtime is booted.
        assert "fake · fake-1" in splash.full_text()
        await pilot.press("space")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, SplashScreen)
        assert app._prompt is not None
        assert app._prompt.has_focus


async def test_control_bar_reflects_runtime_state() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(ControlBar)
        assert bar.preset_select.value == "workspace-write"
        assert bar.approval_select.value == "ask"
        assert bar.cwd_select.value == str(Path("D:/work"))
        assert str(Path("D:/work")) in bar.cwd_values
        assert str(Path("D:/other")) in bar.cwd_values
        assert CUSTOM_WORKSPACE in bar.cwd_values


async def test_control_bar_switches_preset_and_approval() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(ControlBar)
        bar.preset_select.value = "read-only"
        for _ in range(40):
            await pilot.pause(0.05)
            if app.runtime.permission.preset == "read-only":
                break
        assert app.runtime.permission.preset == "read-only"
        assert bar.preset_select.value == "read-only"
        bar.approval_select.value = "never"
        for _ in range(40):
            await pilot.pause(0.05)
            if app.runtime.permission.approval == "never":
                break
        assert app.runtime.permission.approval == "never"
        assert bar.approval_select.value == "never"


async def test_control_bar_switches_workspace() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(ControlBar)
        target = str(Path("D:/other"))
        bar.cwd_select.value = target
        for _ in range(40):
            await pilot.pause(0.05)
            if str(app.runtime.cwd) == target:
                break
        assert str(app.runtime.cwd) == target
        assert bar.cwd_select.value == str(app.runtime.cwd)


async def test_control_bar_custom_workspace_uses_path_prompt(monkeypatch: Any) -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        asked: list[str] = []

        target = str(Path("D:/custom"))

        async def fake_prompt() -> str:
            asked.append("prompt")
            return target

        monkeypatch.setattr(app, "_ask_workspace_path", fake_prompt)
        bar = app.query_one(ControlBar)
        bar.cwd_select.value = CUSTOM_WORKSPACE
        for _ in range(40):
            await pilot.pause(0.05)
            if str(app.runtime.cwd) == target:
                break
        assert asked == ["prompt"]
        assert str(app.runtime.cwd) == target


async def test_control_bar_is_disabled_during_a_turn() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(ControlBar)
        assert bar.preset_select.disabled is False
        app._working = True
        app.refresh_status()
        await pilot.pause(0.1)
        assert bar.preset_select.disabled is True
        assert bar.approval_select.disabled is True
        assert bar.cwd_select.disabled is True
        app._working = False
        app.refresh_status()
        await pilot.pause(0.1)
        assert bar.preset_select.disabled is False
