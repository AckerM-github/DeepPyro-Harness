"""Headless one-shot mode through the M2 native loop switch."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from deeppyro import headless
from deeppyro.core.ids import new_session_id
from deeppyro.core.protocols import Finish, TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeCodec
from deeppyro.tools.registry import ToolRegistry
from deeppyro.tui.main import build_parser
from deeppyro.tui.main import main as cli_main
from tests.agent.fakes import FakeTool, ScriptedProvider, ScriptedTurn, finish, usage


class FakeNativeRuntime:
    """Stand-in HeadlessRuntime exposing only what the native path uses."""

    def __init__(
        self, provider: ScriptedProvider, tools: tuple[Any, ...] = (), *, preset: str = "standard"
    ) -> None:
        self.cwd = Path.cwd()
        self.provider = provider
        self.model = "fake-model"
        self.provider_name = provider.name
        self.registry = ToolRegistry(tools)
        self.tools = None
        self.preset = preset
        self.preset_spec = None
        self.permission = None
        self.jobs = None
        self.kernel_app = None
        self.closed = False

    def _plugin_services(self) -> dict[str, Any]:
        return {"test": object()}

    async def aclose(self) -> None:
        self.closed = True


def _console() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    return Console(file=stream, width=100, force_terminal=False), stream


def _sink() -> tuple[headless.JsonLinesSink, io.StringIO]:
    stream = io.StringIO()
    return headless.JsonLinesSink(stream), stream


def _events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.fixture()
def native_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPPYRO_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("DEEPPYRO_CHECKPOINTS_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("DEEPPYRO_TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv(headless.NATIVE_LOOP_ENV, raising=False)
    return tmp_path


def test_native_loop_enabled_reads_flag_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(headless.NATIVE_LOOP_ENV, raising=False)
    assert headless.native_loop_enabled() is False
    assert headless.native_loop_enabled(True) is True
    for value in ("1", "true", "YES", " on "):
        monkeypatch.setenv(headless.NATIVE_LOOP_ENV, value)
        assert headless.native_loop_enabled() is True
    monkeypatch.setenv(headless.NATIVE_LOOP_ENV, "0")
    assert headless.native_loop_enabled() is False
    assert headless.native_loop_enabled(True) is True


def test_cli_exposes_and_forwards_native_loop_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_parser().parse_args(["hi"]).native_loop is False
    assert build_parser().parse_args(["hi", "--native-loop"]).native_loop is True

    captured: dict[str, Any] = {}

    def fake_run_once(prompt: str, **kwargs: Any) -> int:
        captured["prompt"] = prompt
        captured.update(kwargs)
        return headless.EXIT_OK

    import deeppyro.paths as paths_module

    monkeypatch.setattr(paths_module, "migrate_legacy_layout", lambda: {})
    monkeypatch.setattr(headless, "run_once", fake_run_once)
    assert cli_main(["hi", "--native-loop"]) == headless.EXIT_OK
    assert captured["prompt"] == "hi"
    assert captured["native_loop"] is True


async def test_native_headless_run_persists_native_session(native_dirs: Path) -> None:
    provider = ScriptedProvider(
        [ScriptedTurn(events=[TextDelta("hello from native"), finish("stop"), usage(7, 3)])]
    )
    runtime = FakeNativeRuntime(provider)
    sink, stream = _sink()
    code = await headless._run_native_async(
        "say hello",
        runtime=runtime,
        console=_console()[0],
        sink=sink,
        session_id=new_session_id(),
        json_output=True,
        max_steps=None,
        preset_id="standard",
    )
    assert code == headless.EXIT_OK
    events = _events(stream)
    assert [event["type"] for event in events] == ["session", "text", "final", "usage"]
    assert events[-2] == {"type": "final", "text": "hello from native"}
    assert events[-1] == {"type": "usage", "input": 7, "output": 3, "cached": 0}
    files = sorted((native_dirs / "sessions").glob("*.jsonl"))
    assert len(files) == 1
    log = NativeCodec.read(files[0])
    assert log.events[0].type == EventKind.RUN_STARTED.value
    assert log.events[-1].type == EventKind.RUN_FINISHED.value
    view = fold(log.header, log.events)
    assert view.issues == ()
    assert view.messages[0].text() == "say hello"
    assert view.messages[-1].text() == "hello from native"
    assert list((native_dirs / "checkpoints").glob("*.json"))
    assert list((native_dirs / "traces").glob("*.jsonl"))
    assert runtime.closed is False


async def test_native_headless_forwards_tool_hooks(native_dirs: Path) -> None:
    provider = ScriptedProvider(
        [
            ScriptedTurn(
                events=[
                    ToolCallStart(call_id="call_provider_1", name="echo", index=0),
                    ToolCallArgsDelta(call_id="call_provider_1", delta='{"value": 1}', index=0),
                    Finish(kind="tool-calls"),
                    usage(4, 2),
                ]
            ),
            ScriptedTurn(events=[TextDelta("done"), finish("stop"), usage(3, 1)]),
        ]
    )
    tool = FakeTool("echo", text="ok")
    runtime = FakeNativeRuntime(provider, tools=(tool,))
    sink, stream = _sink()
    code = await headless._run_native_async(
        "use the echo tool",
        runtime=runtime,
        console=_console()[0],
        sink=sink,
        session_id=new_session_id(),
        json_output=True,
        max_steps=None,
        preset_id="standard",
    )
    assert code == headless.EXIT_OK
    events = _events(stream)
    assert [event["type"] for event in events] == [
        "session",
        "tool_call",
        "tool_result",
        "text",
        "final",
        "usage",
    ]
    tool_call = next(event for event in events if event["type"] == "tool_call")
    assert tool_call["name"] == "echo"
    assert tool_call["arguments"] == '{"value": 1}'
    tool_result = next(event for event in events if event["type"] == "tool_result")
    assert tool_result["text"] == "ok"
    assert tool_result["isError"] is False
    assert tool.calls and tool.calls[0][0] == {"value": 1}


async def test_native_headless_reports_budget_exceeded(native_dirs: Path) -> None:
    provider = ScriptedProvider(
        [
            ScriptedTurn(
                events=[
                    ToolCallStart(call_id="call_provider_1", name="echo", index=0),
                    ToolCallArgsDelta(call_id="call_provider_1", delta="{}", index=0),
                    Finish(kind="tool-calls"),
                    usage(2, 1),
                ]
            ),
            ScriptedTurn(events=[TextDelta("never reached"), finish("stop"), usage(2, 1)]),
        ]
    )
    runtime = FakeNativeRuntime(provider, tools=(FakeTool("echo"),))
    sink, stream = _sink()
    code = await headless._run_native_async(
        "loop forever",
        runtime=runtime,
        console=_console()[0],
        sink=sink,
        session_id=new_session_id(),
        json_output=True,
        max_steps=1,
        preset_id="standard",
    )
    assert code == headless.EXIT_ERROR
    events = _events(stream)
    assert [event["type"] for event in events if event["type"] == "error"]
    assert events[-1]["type"] == "error"
    files = sorted((native_dirs / "sessions").glob("*.jsonl"))
    log = NativeCodec.read(files[0])
    finished = [event for event in log.events if event.type == EventKind.RUN_FINISHED.value]
    assert finished and finished[-1].data["status"] == "failed"


def test_run_once_routes_to_native_loop_when_enabled(
    native_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        [ScriptedTurn(events=[TextDelta("native answer"), finish("stop"), usage(2, 1)])]
    )
    runtime = FakeNativeRuntime(provider)

    async def fake_boot(**kwargs: Any) -> FakeNativeRuntime:
        return runtime

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)
    monkeypatch.setenv(headless.NATIVE_LOOP_ENV, "1")
    console, stream = _console()
    code = headless.run_once("say hello", console=console)
    assert code == headless.EXIT_OK
    assert "native answer" in stream.getvalue()
    assert runtime.closed is True
    assert len(list((native_dirs / "sessions").glob("*.jsonl"))) == 1


def test_native_tools_prefers_the_scoped_registry() -> None:
    runtime = FakeNativeRuntime(ScriptedProvider([]))
    tool = FakeTool("echo")

    class ScopedTools:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, int | None]] = []

        def registry_for(self, cwd: str, *, scope: str, revision: int | None) -> ToolRegistry:
            self.calls.append((cwd, scope, revision))
            return ToolRegistry((tool,))

    scoped = ScopedTools()
    runtime.tools = scoped
    assert headless._native_tools(runtime, "standard") == (tool,)
    assert scoped.calls == [(str(runtime.cwd), "standard", None)]
