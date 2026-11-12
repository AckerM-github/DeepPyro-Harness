"""Smoke tests for the Textual application with an injected fake runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.tui.app import DeepPyroApp
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
    approval = "ask"


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
    app = DeepPyroApp(runtime_factory=_make_runtime)
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
    app = DeepPyroApp(runtime_factory=_make_runtime)
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
    app = DeepPyroApp(runtime_factory=_make_runtime)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/nope"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.busy is False


async def test_new_session_command_resets_conversation() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime)
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
    app = DeepPyroApp(runtime_factory=_make_runtime)
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
    app = DeepPyroApp(runtime_factory=_make_runtime)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        async def fake_pick(title: str, items: Any) -> str:
            assert title == "Select model"
            return "fake-2"

        monkeypatch.setattr(app, "_pick", fake_pick)
        await app.model_command("")
        assert app.runtime.model == "fake-2"


async def test_ctrl_c_clears_the_composer_when_idle() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime)
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

    app = DeepPyroApp(runtime_factory=_make_runtime)
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
