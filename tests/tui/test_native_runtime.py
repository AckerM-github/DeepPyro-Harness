"""Native-only TUI runtime tests: the TUI boots against a scripted native provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.errors import ProviderStreamError
from deeppyro.agent.native_host import NativeAgentHost, NativeSessionHandle
from deeppyro.api import AGENT_MODE_TEAM, DEFAULT_PRESET_ID, TEAM_PRESET_ID
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.errors import DeeppyroError
from deeppyro.core.llm import Finish, StopReason, TextDelta, Usage, UsageEvent
from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.app_turns import TurnLayer
from deeppyro.tui.runtime import TuiRuntime
from deeppyro.tui.sink import TuiSink


class _ScriptedProvider:
    """Native provider that replays a fixed stream; never touches the network."""

    name = "local"

    def __init__(self) -> None:
        self.script: list[Any] = []
        self.failure: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.calls = 0

    async def stream(self, request: Any) -> AsyncIterator[Any]:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        for event in list(self.script):
            yield event
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        return None


class _Conversation:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def add_info(self, text: str) -> None:
        self.infos.append(str(text))

    def add_error(self, text: str) -> None:
        self.errors.append(str(text))


class _TurnHost(TurnLayer):
    """Minimal host exposing the turn-layer attributes without a Textual app."""

    def __init__(self, runtime: TuiRuntime) -> None:
        self.runtime = runtime
        self._sink = runtime.sink
        self._working = True
        self._phase = "thinking"
        self._stub_conversation = _Conversation()
        self.refreshes = 0
        self.notes: list[tuple[str, str]] = []

    @property
    def conversation(self) -> _Conversation:
        return self._stub_conversation

    def refresh_status(self) -> None:
        self.refreshes += 1

    def notify(self, message: str, *, severity: str = "information", **kwargs: Any) -> None:
        self.notes.append((str(message), str(severity)))


async def _ask(request: Any) -> bool:
    return True


async def _noop_title(self: Any) -> None:
    return None


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )


async def _boot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: _ScriptedProvider,
    *,
    resume: str | None = None,
) -> tuple[TuiRuntime, list[dict[str, Any]]]:
    monkeypatch.setattr(LlmPlugin, "native_provider", lambda self, name: provider)
    events: list[dict[str, Any]] = []
    runtime = await TuiRuntime.boot(
        sink=TuiSink(events.append),
        ask=_ask,
        config=_config(tmp_path),
        resume=resume,
    )
    return runtime, events


@pytest.fixture(autouse=True)
def _isolated_runtime_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))


async def test_boot_creates_a_native_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider()
    runtime, events = await _boot(monkeypatch, tmp_path, provider)
    try:
        assert not hasattr(runtime, "store")
        assert isinstance(runtime.session, NativeSessionHandle)
        assert isinstance(runtime.agent, NativeAgentHost)
        assert runtime.session_id == runtime.agent.session_id
        view = runtime.session.fold()
        assert view.system is not None
        assert view.system.text().strip()
        assert view.state.model is not None
        assert view.state.model.provider == "local"
        assert view.state.model.model == "m"
        assert view.state.preset is not None
        assert view.state.preset.preset == DEFAULT_PRESET_ID
        sessions = runtime.kernel_app.require(SessionsPlugin)
        assert sessions.native_exists(runtime.session_id)
        assert events[-1] == {"type": "session", "sessionId": runtime.session_id}
    finally:
        await runtime.close()


async def test_turns_run_through_the_native_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider()
    provider.script = [TextDelta("native answer"), Finish(StopReason.STOP)]
    runtime, events = await _boot(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        layer = _TurnHost(runtime)
        await layer._run_turn("hello")
        finals = [event for event in events if event.get("type") == "final"]
        assert finals and finals[-1]["text"] == "native answer"
        assert any(event.get("type") == "session" for event in events)
    finally:
        await runtime.close()


async def test_native_turn_emits_exact_tui_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider()
    provider.script = [
        TextDelta("hello "),
        TextDelta("world"),
        UsageEvent(Usage(input_tokens=12, output_tokens=4)),
        Finish(StopReason.STOP),
    ]
    runtime, events = await _boot(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        events.clear()
        layer = _TurnHost(runtime)
        await layer._run_turn("hello")
        assert events == [
            {"type": "status", "phase": "thinking"},
            {"type": "step", "turn": 1, "step": 1},
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
            {"type": "final", "text": "hello world"},
            {"type": "usage", "input": 12, "output": 4, "cached": 0},
            {"type": "status", "phase": "done"},
        ]
        assert runtime.session_usage() == {"input": 12, "output": 4, "cached": 0}
        assert runtime.context_tokens() == 12
        assert layer.refreshes >= 1
    finally:
        await runtime.close()


async def test_native_turn_reports_a_truncated_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider()
    provider.script = [TextDelta("cut"), Finish(StopReason.LENGTH)]
    runtime, events = await _boot(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        events.clear()
        layer = _TurnHost(runtime)
        await layer._run_turn("write a long answer")
        assert [event["type"] for event in events] == [
            "status",
            "step",
            "text",
            "final",
            "usage",
            "status",
            "error",
        ]
        assert events[3] == {"type": "final", "text": "cut"}
        assert events[-2] == {"type": "status", "phase": "done"}
        assert "finish_reason: length" in events[-1]["message"]
    finally:
        await runtime.close()


async def test_native_turn_reports_provider_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider()
    provider.failure = ProviderStreamError("provider exploded")
    runtime, events = await _boot(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        events.clear()
        layer = _TurnHost(runtime)
        await layer._run_turn("boom")
        kinds = [event["type"] for event in events]
        assert kinds[:2] == ["status", "step"]
        assert kinds[-1] == "error"
        assert events[-1] == {"type": "error", "message": "provider exploded"}
        assert "final" not in kinds
        assert "usage" not in kinds
        assert layer._sink.saw_error is True
    finally:
        await runtime.close()


async def test_native_turn_reports_unexpected_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider()
    provider.failure = RuntimeError("boom")
    runtime, events = await _boot(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        events.clear()
        layer = _TurnHost(runtime)
        await layer._run_turn("boom")
        assert events[-1]["type"] == "error"
        assert "RuntimeError" in events[-1]["message"]
        assert "boom" in events[-1]["message"]
    finally:
        await runtime.close()


async def test_interrupt_cancels_a_native_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider()
    provider.gate = asyncio.Event()
    runtime, events = await _boot(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        events.clear()
        layer = _TurnHost(runtime)
        task = asyncio.create_task(layer._run_turn("slow"))
        for _ in range(500):
            if provider.calls > 0:
                break
            await asyncio.sleep(0.01)
        assert provider.calls == 1
        await layer._interrupt_turn()
        assert layer.notes[-1][0] == "interrupt requested"
        await task
        assert events[-2:] == [
            {"type": "final", "text": ""},
            {"type": "status", "phase": "interrupted"},
        ]
        assert await runtime.agent.interrupt() is False
        await layer._interrupt_turn()
        assert layer.notes[-1][0] == "nothing to interrupt"
    finally:
        provider.gate.set()
        await runtime.close()


async def test_resume_reopens_a_native_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _ScriptedProvider()
    provider.script = [TextDelta("first"), Finish(StopReason.STOP)]
    runtime, _events = await _boot(monkeypatch, tmp_path, provider)
    session_id = runtime.session_id
    try:
        await runtime.agent.send("remember this")
    finally:
        await runtime.close()
    provider.script = [TextDelta("second"), Finish(StopReason.STOP)]
    resumed, resumed_events = await _boot(monkeypatch, tmp_path, provider, resume=session_id)
    try:
        assert resumed.session_id == session_id
        view = resumed.session.fold()
        assert view.state.model is not None
        assert view.state.model.provider == "local"
        assert view.state.preset is not None
        assert view.state.preset.preset == DEFAULT_PRESET_ID
        prompts = [message.text() for message in resumed.session.derive_messages() if message.role == "user"]
        assert any("remember this" in prompt for prompt in prompts)
        result = await resumed.agent.send("again")
        assert result.final_text == "second"
        assert any(event.get("type") == "session" for event in resumed_events)
    finally:
        await resumed.close()


async def test_native_title_is_written_through_the_native_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider()
    provider.script = [TextDelta("hello"), Finish(StopReason.STOP)]
    runtime, _events = await _boot(monkeypatch, tmp_path, provider)
    try:
        record = runtime.kernel_app.mount_record("session-title")
        assert record is not None
        plugin = record.instance

        async def _fake_complete(provider_name: str, model: str, prompt: str) -> str:
            return "Native title"

        monkeypatch.setattr(plugin, "_complete", _fake_complete)
        await runtime.agent.send("please name me")
        title = await runtime.name_session_title()
        assert title == "Native title"
        assert runtime.session.fold().state.title == "Native title"
    finally:
        await runtime.close()


async def test_team_mode_toggles_and_keeps_genuine_absences_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider()
    runtime, _events = await _boot(monkeypatch, tmp_path, provider)
    try:
        # team mode toggles the team preset on the native host and stops the manager
        # again; it no longer raises the old "not ported" error.
        assert await runtime.set_team_mode(True) == TEAM_PRESET_ID
        assert runtime.agent_mode == AGENT_MODE_TEAM
        assert await runtime.set_team_mode(False) == DEFAULT_PRESET_ID
        assert runtime.agent_mode != AGENT_MODE_TEAM
        # the hook recorder observes the same HookService the host dispatches.
        assert runtime.hook_recorder is not None
        # a bundle without the native compact plugin keeps an explicit error.
        with pytest.raises(DeeppyroError, match="compaction is not in this bundle"):
            await runtime.compact_now()
    finally:
        await runtime.close()


def test_cli_parser_accepts_resume() -> None:
    from deeppyro.tui.main import build_parser

    args = build_parser().parse_args(["--resume", "session-abc"])
    assert args.resume == "session-abc"


def test_app_passes_resume_to_the_runtime() -> None:
    app = DeepPyroApp(cwd=Path("."), resume="session-abc")
    assert app._resume == "session-abc"


@pytest.mark.parametrize("flag", ["--legacy-v4", "--native-loop"])
def test_removed_backend_flags_fail_to_parse(flag: str) -> None:
    from deeppyro.tui.main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args([flag])
