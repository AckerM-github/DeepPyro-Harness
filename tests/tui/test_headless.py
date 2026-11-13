"""Headless one-shot mode: sinks, validation and the run_once exit codes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from deeppyro import headless
from deeppyro.core.errors import DeeppyroError


class FakeSession:
    session_id = "session-11112222-3333-4444-5555-666677778888"


class FakeAgent:
    def __init__(self, events: list[dict[str, Any]], *, fail: bool = False, interrupt: bool = False) -> None:
        self._events = events
        self._fail = fail
        self._interrupt = interrupt
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str) -> str:
        self.sent.append(text)
        if self._interrupt:
            raise KeyboardInterrupt
        for event in self._events:
            self.sink.emit(event)
        if self._fail:
            raise DeeppyroError("boom")
        return "done"

    async def interrupt(self) -> None:
        self.interrupted = True

    async def aclose(self) -> None:
        self.closed = True


class FakeStore:
    def __init__(self) -> None:
        self.created: list[str] = []

    def create(self, cwd: str) -> FakeSession:
        self.created.append(cwd)
        return FakeSession()


class FakeAgents:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent

    def options(self, **overrides: Any) -> dict[str, Any]:
        return overrides

    def create(self, **kwargs: Any) -> FakeAgent:
        self.agent.sink = kwargs["sink"]
        return self.agent


class FakeJobs:
    def __init__(self) -> None:
        self.owners: list[str] = []

    async def cancel_owner(self, owner: str) -> int:
        self.owners.append(owner)
        return 0


class FakeTeam:
    def __init__(self) -> None:
        self.bound: list[Any] = []
        self.closed = False

    def bind_session(self, session: Any) -> None:
        self.bound.append(session)

    async def close(self) -> None:
        self.closed = True


class FakeRuntime:
    def __init__(self, agent: FakeAgent, *, jobs: Any = None, team: Any = None) -> None:
        self.cwd = Path("D:/work")
        self.provider_name = "fake"
        self.model = "fake-1"
        self.context_window = 8192
        self.store = FakeStore()
        self.agents = FakeAgents(agent)
        self.jobs = jobs
        self.team = team
        self.closed = False

    def create_agent(self, session: Any, sink: Any, *, max_steps: int | None = None) -> FakeAgent:
        return self.agents.create(session=session, sink=sink, max_steps=max_steps)

    async def aclose(self) -> None:
        self.closed = True


def make_console() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    return Console(file=stream, width=100, force_terminal=False), stream


def patch_runtime(monkeypatch: pytest.MonkeyPatch, runtime: Any) -> None:
    async def fake_boot(**kwargs: Any) -> Any:
        return runtime

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)


# --------------------------------------------------------------------------
# read_stdin_prompt
# --------------------------------------------------------------------------


def test_read_stdin_prompt_reads_piped_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO("hello from pipe\n")
    monkeypatch.setattr(stream, "isatty", lambda: False)
    monkeypatch.setattr(headless.sys, "stdin", stream)
    assert headless.read_stdin_prompt() == "hello from pipe\n"


def test_read_stdin_prompt_ignores_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO("ignored")
    monkeypatch.setattr(stream, "isatty", lambda: True)
    monkeypatch.setattr(headless.sys, "stdin", stream)
    assert headless.read_stdin_prompt() == ""


def test_read_stdin_prompt_tolerates_closed_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(io.StringIO):
        def isatty(self) -> bool:
            return False

        def read(self) -> str:
            raise OSError("closed")

    monkeypatch.setattr(headless.sys, "stdin", Broken())
    assert headless.read_stdin_prompt() == ""


# --------------------------------------------------------------------------
# sinks
# --------------------------------------------------------------------------


def test_json_lines_sink_writes_compact_ndjson() -> None:
    stream = io.StringIO()
    sink = headless.JsonLinesSink(stream)
    sink.begin_turn()
    sink.emit({"type": "text", "text": "hi"})
    sink.emit({"type": "error", "message": "bad"})
    sink.end_turn()
    lines = stream.getvalue().splitlines()
    assert [json.loads(line) for line in lines] == [
        {"type": "text", "text": "hi"},
        {"type": "error", "message": "bad"},
    ]
    assert sink.event_count == 2
    assert sink.saw_error is True


def test_rich_sink_streams_text_and_tracks_usage() -> None:
    console, stream = make_console()
    sink = headless.RichSink(console)
    sink.begin_turn()
    sink.emit({"type": "thinking", "text": "pondering"})
    sink.emit({"type": "tool_call", "callId": "c1", "name": "read", "arguments": '{"path": "a"}'})
    sink.emit({"type": "tool_result", "callId": "c1", "isError": False, "text": "line1"})
    sink.emit({"type": "text", "text": "hel"})
    sink.emit({"type": "text", "text": "lo"})
    sink.emit({"type": "usage", "input": 10, "output": 2, "cached": 3})
    sink.emit({"type": "usage", "input": 5, "output": 1, "cached": 0})
    sink.emit({"type": "final", "text": "hello"})
    sink.end_turn()
    out = stream.getvalue()
    assert "hello" in out
    assert "read" in out
    assert "→" in out and "✓" in out
    assert sink.usage == {"input": 15, "output": 3, "cached": 3}
    assert sink.final_text == "hello"
    assert sink.saw_text is True
    assert sink.saw_error is False


def test_rich_sink_reports_errors() -> None:
    console, stream = make_console()
    sink = headless.RichSink(console)
    sink.emit({"type": "error", "message": "kaput"})
    assert sink.saw_error is True
    assert "kaput" in stream.getvalue()


# --------------------------------------------------------------------------
# run_once
# --------------------------------------------------------------------------


def test_run_once_rejects_blank_prompt() -> None:
    console, stream = make_console()
    assert headless.run_once("   ", console=console) == headless.EXIT_USAGE
    assert "no prompt" in stream.getvalue()


def test_run_once_rejects_bad_max_steps() -> None:
    console, stream = make_console()
    assert headless.run_once("hi", max_steps=0, console=console) == headless.EXIT_USAGE
    assert "--max-steps" in stream.getvalue()


class RecordingAgents:
    def __init__(self) -> None:
        self.options_seen: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    def options(self, **overrides: Any) -> dict[str, Any]:
        self.options_seen.append(overrides)
        return overrides

    def create(self, **kwargs: Any) -> object:
        self.created.append(kwargs)
        return object()


def test_create_agent_passes_the_resolved_model() -> None:
    agents = RecordingAgents()
    runtime = headless.HeadlessRuntime(
        cfg=headless.Config(),
        cwd=Path("D:/work"),
        kernel_app=None,
        provider_name="fake",
        model="fake-1",
        context_window=8192,
        store=FakeStore(),
        provider=object(),
        registry=object(),
        permission=object(),
        agents=agents,
    )
    runtime.create_agent(FakeSession(), sink=object(), max_steps=7)
    assert agents.options_seen[-1]["model"] == "fake-1"
    assert agents.options_seen[-1]["context_window"] == 8192
    assert agents.options_seen[-1]["max_steps"] == 7
    assert agents.created[-1]["cwd"] == str(Path("D:/work"))


def test_run_once_cancels_background_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent([{"type": "final", "text": "done"}])
    jobs = FakeJobs()
    runtime = FakeRuntime(agent, jobs=jobs)
    patch_runtime(monkeypatch, runtime)
    console, _stream = make_console()
    assert headless.run_once("question", console=console) == headless.EXIT_OK
    assert jobs.owners == [FakeSession.session_id]


def test_run_once_binds_the_team_session(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent([{"type": "final", "text": "done"}])
    team = FakeTeam()
    runtime = FakeRuntime(agent, team=team)
    patch_runtime(monkeypatch, runtime)
    console, _stream = make_console()
    assert headless.run_once("question", console=console) == headless.EXIT_OK
    assert len(team.bound) == 1
    assert getattr(team.bound[0], "session_id", "") == FakeSession.session_id


def test_run_once_streams_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent(
        [
            {"type": "thinking", "text": "hmm"},
            {"type": "text", "text": "answer"},
            {"type": "usage", "input": 4, "output": 1, "cached": 0},
            {"type": "final", "text": "answer"},
        ]
    )
    runtime = FakeRuntime(agent)
    patch_runtime(monkeypatch, runtime)
    console, stream = make_console()
    code = headless.run_once("question", console=console)
    assert code == headless.EXIT_OK
    assert agent.sent == ["question"]
    assert agent.closed is True
    assert runtime.closed is True
    assert runtime.store.created == ["D:\\work"]
    out = stream.getvalue()
    assert "answer" in out
    assert "session 11112222" in out
    assert "tokens in=4 out=1 cached=0" in out


def test_run_once_json_mode_emits_session_and_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = FakeAgent(
        [
            {"type": "text", "text": "hi"},
            {"type": "final", "text": "hi"},
        ]
    )
    patch_runtime(monkeypatch, FakeRuntime(agent))
    console, _ = make_console()
    code = headless.run_once("q", json_output=True, console=console)
    assert code == headless.EXIT_OK
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    types = [line["type"] for line in lines]
    assert types == ["session", "text", "final"]
    assert lines[0]["sessionId"].startswith("session-")


def test_run_once_maps_agent_failure_to_exit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent([], fail=True)
    patch_runtime(monkeypatch, FakeRuntime(agent))
    console, stream = make_console()
    assert headless.run_once("q", console=console) == headless.EXIT_ERROR
    assert "boom" in stream.getvalue()
    assert agent.closed is True


def test_run_once_maps_keyboard_interrupt_to_130(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent([], interrupt=True)
    patch_runtime(monkeypatch, FakeRuntime(agent))
    console, stream = make_console()
    assert headless.run_once("q", console=console) == headless.EXIT_INTERRUPTED
    assert "interrupted" in stream.getvalue()


def test_run_once_maps_config_error_to_exit_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_boot(**kwargs: Any) -> Any:
        raise headless.ConfigError("unknown provider: nope")

    monkeypatch.setattr(headless, "boot_runtime", failing_boot)
    console, stream = make_console()
    assert headless.run_once("q", console=console) == headless.EXIT_USAGE
    assert "unknown provider" in stream.getvalue()


def test_resolve_workspace_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(headless.ConfigError):
        headless.resolve_workspace(tmp_path / "missing")
    assert headless.resolve_workspace(tmp_path) == tmp_path.resolve()
