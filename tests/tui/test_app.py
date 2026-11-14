"""Smoke tests for the Textual application with an injected fake runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from textual.widgets import Static

from deeppyro.tui.app import CUSTOM_WORKSPACE, DeepPyroApp
from deeppyro.tui.screens.plugin_panel import PluginPanelScreen
from deeppyro.tui.slots import SlotRegistry
from deeppyro.tui.widgets.conversation import ConversationView
from deeppyro.tui.widgets.plugin_slots import PluginStatusBar
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
            self.sink.emit({"type": "step", "turn": 1, "step": 1})
            self.sink.emit({"type": "thinking", "text": "pondering。more"})
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
    def __init__(
        self,
        preset_id: str,
        name: str,
        description: str = "",
        presentation: str = "native",
    ) -> None:
        self.id = preset_id
        self.name = name
        self.description = description
        self.presentation = presentation

    def display_name(self) -> str:
        return self.name or self.id


class FakeJob:
    def __init__(self, job_id: str, status: str = "running") -> None:
        self.id = job_id
        self.kind = "pwsh"
        self.label = "uv run pytest"
        self.status = status
        self.owner = "session-11112222"
        self.started_at = 1_700_000_000_000
        self.finished_at = None
        self.call_id = "call-1"


class FakeJobs:
    def __init__(self) -> None:
        self.rows = [FakeJob("pwsh-1111222233334444")]

    def list(self) -> list[FakeJob]:
        return list(self.rows)


class FakeTeam:
    team_id = "team-1"

    def members(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "member-1",
                "name": "scout",
                "description": "searches the workspace",
                "phase": "running",
                "model": "fake-1",
            }
        ]

    def tasks(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "task-1",
                "subject": "wire the team panel",
                "status": "pending",
                "owner": "",
                "blocked_by": [],
                "write_scopes": [],
            }
        ]


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
        self.agent_preset = "standard"
        self.agent_mode = "default"
        self.team_previous_preset = "standard"
        self.jobs = FakeJobs()
        self.team = FakeTeam()

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def busy(self) -> bool:
        return self.agent.busy

    @property
    def agent_presentation(self) -> str:
        return "ptc" if self.agent_preset == "ptc" else "native"

    @property
    def agent_preset_display(self) -> str:
        names = {
            "standard": "Standard",
            "ptc": "PTC",
            "minimal": "Minimal",
            "cordis": "Cordis",
            "team": "Team",
        }
        return names.get(self.agent_preset, self.agent_preset)

    async def new_session(self) -> Any:
        self.session = FakeSession()
        self.agent = FakeAgent(self.sink)
        return self.session

    async def open_session(self, session_id: str) -> Any:
        self.opened_sessions.append(session_id)
        return self.session

    PROVIDER_MODELS: ClassVar[dict[str, list[str]]] = {"fake": ["fake-1", "fake-2"], "fake-2": ["fake-3"]}

    async def set_model(self, model_id: str) -> None:
        self.model = model_id

    async def set_provider(self, name: str, *, model: str | None = None) -> tuple[str, str]:
        self.provider_name = name
        self.model = model or self.models(name)[0]
        return self.provider_name, self.model

    def providers(self) -> list[str]:
        return list(self.PROVIDER_MODELS)

    def models(self, provider: str | None = None) -> list[str]:
        return list(self.PROVIDER_MODELS.get(provider or self.provider_name, []))

    def model_choices(self) -> list[tuple[str, str]]:
        return [(name, model) for name, models in self.PROVIDER_MODELS.items() for model in models]

    def presets(self) -> list[Any]:
        return [
            FakePreset("standard", "Standard", "host-wide tools", presentation="native"),
            FakePreset("ptc", "PTC", "run_code-only", presentation="ptc"),
            FakePreset("minimal", "Minimal", "small surface", presentation="native"),
            FakePreset("cordis", "Cordis", "cordis prompt", presentation="native"),
        ]

    async def set_agent_preset(self, preset: str) -> str:
        if preset == "team":
            raise RuntimeError("team mode is enabled with /team on, not /preset")
        self.agent_preset = preset
        return preset

    async def set_team_mode(self, enabled: bool) -> str:
        if enabled:
            if self.agent_mode == "team":
                return "team"
            self.team_previous_preset = self.agent_preset
            self.agent_mode = "team"
            self.agent_preset = "team"
            return "team"
        if self.agent_mode != "team":
            raise RuntimeError("agent team mode is not active")
        live = [
            member
            for member in self.team.members()
            if str(member.get("phase", "")) in {"spawning", "running", "idle"}
        ]
        if live:
            raise RuntimeError(f"cannot leave team mode while {len(live)} member(s) are active")
        self.agent_mode = "default"
        self.agent_preset = self.team_previous_preset
        return self.agent_preset

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


def _info_texts(app: DeepPyroApp) -> list[str]:
    texts: list[str] = []
    for widget in app.query(".system-block"):
        content = getattr(widget, "content", "")
        texts.append(str(getattr(content, "plain", content)))
    return texts


class _FakeKernel:
    def __init__(self, registry: SlotRegistry) -> None:
        self.registry = registry

    def service(self, name: str) -> Any:
        if name == "slots":
            return self.registry
        raise LookupError(name)


def _runtime_with_slots(registry: SlotRegistry):
    async def factory(app: DeepPyroApp) -> FakeRuntime:
        runtime = FakeRuntime(app)
        runtime.kernel_app = _FakeKernel(registry)
        return runtime

    return factory


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


async def test_provider_command_switches_provider() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/provider fake-2"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.runtime.provider_name == "fake-2"
        assert app.runtime.model == "fake-3"


async def test_model_command_accepts_provider_slash_model() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.model_command("fake-2/fake-3")
        assert app.runtime.provider_name == "fake-2"
        assert app.runtime.model == "fake-3"


async def test_preset_command_switches_the_agent_preset() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/preset ptc"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert app.runtime.agent_preset == "ptc"
        texts = _info_texts(app)
        assert any("switched to PTC (ptc) · ptc presentation · default mode" in text for text in texts), texts


async def test_preset_without_argument_uses_picker(monkeypatch: Any) -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        async def fake_pick(title: str, items: Any) -> str:
            assert title == "Select agent preset"
            assert [item[0] for item in items] == ["standard", "ptc", "minimal", "cordis"]
            assert items[0][1] == "Standard  (native · default mode)"
            assert items[1][1] == "PTC  (ptc · default mode)"
            assert items[0][2].startswith("(current) ")
            return "ptc"

        monkeypatch.setattr(app, "_pick", fake_pick)
        await app.preset_command("")
        assert app.runtime.agent_preset == "ptc"


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


async def _submit(app: Any, pilot: Any, text: str) -> None:
    prompt = app.query_one(PromptArea)
    prompt.text = text
    await pilot.press("enter")
    await pilot.pause(0.05)


async def test_policy_and_approval_commands_switch_session_settings() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/policy read-only")
        for _ in range(40):
            await pilot.pause(0.05)
            if app.runtime.permission.preset == "read-only":
                break
        assert app.runtime.permission.preset == "read-only"
        await _submit(app, pilot, "/approval never")
        for _ in range(40):
            await pilot.pause(0.05)
            if app.runtime.permission.approval == "never":
                break
        assert app.runtime.permission.approval == "never"


async def test_policy_command_without_argument_uses_picker(monkeypatch: Any) -> None:
    picked: list[tuple[str, list[tuple[str, str, str]]]] = []

    async def fake_pick(title: str, items: list[tuple[str, str, str]]) -> str:
        picked.append((title, items))
        return "read-only"

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "_pick", fake_pick)
        await _submit(app, pilot, "/policy")
        for _ in range(40):
            await pilot.pause(0.05)
            if app.runtime.permission.preset == "read-only":
                break
        assert app.runtime.permission.preset == "read-only"
    assert picked
    assert picked[0][0] == "Select permission preset"
    assert [value for value, _, _ in picked[0][1]] == [
        "read-only",
        "workspace-write",
        "danger-full-access",
    ]


async def test_cwd_command_switches_workspace() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await _submit(app, pilot, "/cwd D:/other")
        for _ in range(40):
            await pilot.pause(0.05)
            if app.runtime.cwd == Path("D:/other"):
                break
        assert app.runtime.cwd == Path("D:/other")


async def test_cwd_command_without_argument_uses_picker_and_path_prompt(monkeypatch: Any) -> None:
    asked: list[str] = []
    target = str(Path("D:/custom"))

    async def fake_pick(title: str, items: list[tuple[str, str, str]]) -> str:
        asked.append(title)
        assert CUSTOM_WORKSPACE in [value for value, _, _ in items]
        assert str(Path("D:/other")) in [value for value, _, _ in items]
        return CUSTOM_WORKSPACE

    async def fake_prompt() -> str:
        asked.append("prompt")
        return target

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "_pick", fake_pick)
        monkeypatch.setattr(app, "_ask_workspace_path", fake_prompt)
        await _submit(app, pilot, "/cwd")
        for _ in range(40):
            await pilot.pause(0.05)
            if str(app.runtime.cwd) == target:
                break
        assert asked == ["Select workspace", "prompt"]
        assert str(app.runtime.cwd) == target


async def test_plugin_status_items_render() -> None:
    registry = SlotRegistry()
    registry.add_status_item(lambda context: "items: 3", id="queue", plugin_id="demo")
    app = DeepPyroApp(runtime_factory=_runtime_with_slots(registry), splash=False)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        bar = app.query_one(PluginStatusBar)
        assert bar.display is True
        assert "items: 3" in bar.plain_text


async def test_plugin_conversation_row_renders_on_an_event() -> None:
    registry = SlotRegistry()
    seen: list[Any] = []

    def factory(event: dict[str, Any]) -> Static:
        seen.append(event)
        return Static("plugin row", classes="plugin-row")

    registry.add_conversation_row(
        lambda event: event.get("type") == "final",
        factory,
        id="final-note",
        plugin_id="demo",
    )
    app = DeepPyroApp(runtime_factory=_runtime_with_slots(registry), splash=False)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "hi"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert [event["type"] for event in seen] == ["final"]
        assert len(app.query(".plugin-row")) == 1


async def test_panel_command_opens_a_plugin_panel() -> None:
    registry = SlotRegistry()
    registry.add_panel("Demo panel", lambda context: Static("panel body"), id="demo", plugin_id="demo")
    app = DeepPyroApp(runtime_factory=_runtime_with_slots(registry), splash=False)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/panel demo"
        await pilot.press("enter")
        for _ in range(40):
            if isinstance(app.screen, PluginPanelScreen):
                break
            await pilot.pause(0.05)
        assert isinstance(app.screen, PluginPanelScreen)


async def test_step_and_final_events_drive_one_process_block() -> None:
    from deeppyro.tui.widgets.agent_process import AgentProcess

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.handle_event({"type": "step", "turn": 1, "step": 1})
        app.handle_event({"type": "thinking", "text": "first thought。second thought。"})
        app.handle_event({"type": "text", "text": "answer"})
        await pilot.pause()
        process = app.query_one(AgentProcess)
        assert process.finished is False
        assert "second thought" in process.header_text

        app.handle_event({"type": "final", "text": "answer"})
        await pilot.pause()
        assert process.finished is True
        assert "(click to view)" in process.header_text


async def test_clicking_a_process_step_opens_the_thinking_viewer() -> None:
    from deeppyro.tui.screens.thinking import ThinkingScreen
    from deeppyro.tui.widgets.agent_process import AgentProcess

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        app.handle_event({"type": "step", "turn": 1, "step": 1})
        app.handle_event({"type": "thinking", "text": "first thought。second thought。"})
        await pilot.pause()
        process = app.query_one(AgentProcess)
        process.toggle()
        await pilot.pause()
        step = process.query_one(".process-step", Static)
        await pilot.click(step)
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, ThinkingScreen):
                break
        assert isinstance(app.screen, ThinkingScreen)
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_thinking_command_opens_the_viewer(monkeypatch: Any) -> None:
    from deeppyro.tui.history import ReasoningStep, TurnReasoning
    from deeppyro.tui.screens.thinking import ThinkingScreen

    reasoning = TurnReasoning(turn=1, steps=[ReasoningStep(step=1, text="deep thought")], finished=True)
    monkeypatch.setattr(
        "deeppyro.tui.app.reasoning_from_session",
        lambda session, last_turns=1: [reasoning],
    )
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/thinking"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, ThinkingScreen):
                break
        assert isinstance(app.screen, ThinkingScreen)
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_jobs_command_opens_the_jobs_panel() -> None:
    from deeppyro.tui.screens.jobs import JobsPanelScreen

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/jobs"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, JobsPanelScreen):
                break
        assert isinstance(app.screen, JobsPanelScreen)
        assert app.screen.jobs[0].id == "pwsh-1111222233334444"


async def test_team_command_opens_the_team_panel() -> None:
    from deeppyro.tui.screens.team import TeamPanelScreen

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/team"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, TeamPanelScreen):
                break
        assert isinstance(app.screen, TeamPanelScreen)
        assert app.screen.members[0].name == "scout"
        assert app.screen.tasks[0].id == "task-1"
        assert app.screen.team_id == "team-1"
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_team_command_board_opens_the_task_board() -> None:
    from deeppyro.tui.screens.team import TaskBoardScreen

    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/team board"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, TaskBoardScreen):
                break
        assert isinstance(app.screen, TaskBoardScreen)
        assert app.screen.ordered_tasks[0].subject == "wire the team panel"
        await pilot.press("escape")
        await pilot.pause(0.2)


async def test_preset_command_rejects_the_hidden_team_preset() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        await app.preset_command("team")
        assert app.runtime.agent_preset == "standard"
        assert app.runtime.agent_mode == "default"


async def test_team_on_and_off_toggle_the_current_session_mode() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/team on"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if app.runtime.agent_mode == "team":
                break
        assert app.runtime.agent_mode == "team"
        assert app.runtime.agent_preset == "team"

        # The fake team keeps one running member, so leaving is refused.
        await app.team_command("off")
        assert app.runtime.agent_mode == "team"

        app.runtime.team.members = list  # type: ignore[method-assign]
        await app.team_command("off")
        assert app.runtime.agent_mode == "default"
        assert app.runtime.agent_preset == "standard"


async def test_scheduled_message_renders_and_clears_busy_state() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        agent = app.runtime.agent
        waits: list[int] = []

        async def wait_for_idle() -> None:
            waits.append(1)

        agent.wait_for_idle = wait_for_idle  # type: ignore[attr-defined]
        conversation = app.query_one(ConversationView)
        before = len(conversation.children)
        app._on_schedule_message("Scheduled task: standup")
        for _ in range(20):
            await pilot.pause(0.05)
            if not app._working:
                break
        assert len(conversation.children) > before
        assert waits == [1]
        assert app._working is False
        assert app._phase == "idle"


async def test_preset_listing_shows_english_names_and_presentation() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        app._report_agent_presets()
        await pilot.pause(0.1)
        texts = _info_texts(app)
        assert any("standard  Standard  native tools" in text for text in texts), texts
        assert any("ptc  PTC  ptc tools" in text for text in texts), texts
        assert any("(current)" in text for text in texts), texts
        assert any("use /preset <id>" in text for text in texts), texts


async def test_status_and_welcome_show_the_agent_preset_presentation() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.pause()
        app.show_status()
        app._render_welcome()
        await pilot.pause(0.1)
        texts = _info_texts(app)
        assert any("preset   Standard  (native · default mode)" in text for text in texts), texts
        assert any(
            "agent preset Standard (standard · native presentation · default mode)" in text for text in texts
        ), texts
