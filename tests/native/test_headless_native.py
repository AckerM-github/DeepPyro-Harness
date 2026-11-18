"""Headless one-shot mode through the M2 native loop (the default since 0.2.0)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from deeppyro import headless
from deeppyro.builtin.usage import UsageService
from deeppyro.core.ids import new_session_id
from deeppyro.core.protocols import Finish, TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.observability.protocols import TraceExportSink
from deeppyro.observability.trace import ModelCallRecord, RunRecord
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


class RecordingUsageSink:
    """UsageSink double: collects one TurnUsage per finished native turn."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def record_turn(self, usage: Any, /) -> object:
        self.records.append(usage)
        return usage


class RecordingTraceExport:
    """TraceExportSink double: collects the records the loop already persisted."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def export_trace_record(self, record: Any, /) -> None:
        self.records.append(record)


class ObservabilityKernelApp:
    """Kernel app double resolving only the two observability contracts."""

    def __init__(self, usage: RecordingUsageSink, trace_export: RecordingTraceExport) -> None:
        self._usage = usage
        self._trace_export = trace_export

    def get(self, contract: type[Any]) -> Any:
        if contract is UsageService:
            return self._usage
        if contract is TraceExportSink:
            return self._trace_export
        return None


@pytest.fixture()
def native_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPPYRO_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("DEEPPYRO_CHECKPOINTS_DIR", str(tmp_path / "checkpoints"))
    monkeypatch.setenv("DEEPPYRO_TRACES_DIR", str(tmp_path / "traces"))
    monkeypatch.delenv(headless.NATIVE_LOOP_ENV, raising=False)
    return tmp_path


def test_native_loop_enabled_defaults_to_native(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(headless.NATIVE_LOOP_ENV, raising=False)
    assert headless.native_loop_enabled() is True
    assert headless.native_loop_enabled(True) is True
    assert headless.native_loop_enabled(False) is False
    for value in ("1", "true", "YES", " on "):
        monkeypatch.setenv(headless.NATIVE_LOOP_ENV, value)
        assert headless.native_loop_enabled() is True
    for value in ("0", "false", "NO", " off "):
        monkeypatch.setenv(headless.NATIVE_LOOP_ENV, value)
        assert headless.native_loop_enabled() is False
        assert headless.native_loop_enabled(True) is True
    monkeypatch.setenv(headless.NATIVE_LOOP_ENV, "bogus")
    assert headless.native_loop_enabled() is True


def test_cli_exposes_the_backend_switches() -> None:
    parser = build_parser()
    default = parser.parse_args(["hi"])
    assert default.native_loop is False
    assert default.legacy_v4 is False
    assert parser.parse_args(["hi", "--native-loop"]).native_loop is True
    assert parser.parse_args(["hi", "--legacy-v4"]).legacy_v4 is True
    with pytest.raises(SystemExit):
        parser.parse_args(["hi", "--native-loop", "--legacy-v4"])


def test_cli_forwards_the_backend_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    def fake_run_once(prompt: str, **kwargs: Any) -> int:
        captured.append({"prompt": prompt, **kwargs})
        return headless.EXIT_OK

    import deeppyro.paths as paths_module

    monkeypatch.setattr(paths_module, "migrate_legacy_layout", lambda: {})
    monkeypatch.setattr(headless, "run_once", fake_run_once)
    assert cli_main(["hi"]) == headless.EXIT_OK
    with pytest.warns(DeprecationWarning):
        assert cli_main(["hi", "--native-loop"]) == headless.EXIT_OK
    assert cli_main(["hi", "--legacy-v4"]) == headless.EXIT_OK
    assert [entry["native_loop"] for entry in captured] == [None, True, False]


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


def test_run_once_defaults_to_native_loop(native_dirs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No switch and no env var: the native loop is the headless default."""
    provider = ScriptedProvider(
        [ScriptedTurn(events=[TextDelta("native answer"), finish("stop"), usage(2, 1)])]
    )
    runtime = FakeNativeRuntime(provider)

    async def fake_boot(**kwargs: Any) -> FakeNativeRuntime:
        return runtime

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)
    console, stream = _console()
    code = headless.run_once("say hello", console=console)
    assert code == headless.EXIT_OK
    assert "native answer" in stream.getvalue()
    assert runtime.closed is True
    assert len(list((native_dirs / "sessions").glob("*.jsonl"))) == 1


def test_run_once_native_reports_usage_and_trace_export(
    native_dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The native run hands the loop the usage/trace_export services by protocol name.

    Regression: the service mapping used to register the telemetry plugin under
    the plugin id only, so AgentLoop resolved no TraceExportSink and export was
    silently empty.
    """
    provider = ScriptedProvider(
        [ScriptedTurn(events=[TextDelta("native answer"), finish("stop"), usage(5, 2)])]
    )
    usage_sink = RecordingUsageSink()
    trace_export = RecordingTraceExport()
    runtime = headless.HeadlessRuntime(
        # cfg is not read by the native path; the kernel app double owns services
        cfg=None,
        cwd=native_dirs,
        kernel_app=ObservabilityKernelApp(usage_sink, trace_export),
        provider_name=provider.name,
        model="fake-model",
        context_window=32768,
        store=None,
        provider=provider,
        registry=ToolRegistry(()),
        permission=None,
        agents=None,
        preset="standard",
    )

    async def fake_boot(**kwargs: Any) -> headless.HeadlessRuntime:
        return runtime

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)
    console, stream = _console()
    assert headless.run_once("say hello", console=console) == headless.EXIT_OK
    assert "native answer" in stream.getvalue()

    # usage: one TurnUsage for the single native turn, with live counters
    assert len(usage_sink.records) == 1
    turn = usage_sink.records[0]
    assert turn.source == "native"
    assert turn.provider == provider.name
    assert turn.model == "fake-model"
    assert (turn.input_tokens, turn.output_tokens) == (5, 2)
    assert turn.stop_reason == "stop"
    files = sorted((native_dirs / "sessions").glob("*.jsonl"))
    assert len(files) == 1
    assert turn.session_id == NativeCodec.read(files[0]).header.id
    # trace export: the run record and the model-call record reach the sink
    assert [type(record) for record in trace_export.records] == [RunRecord, ModelCallRecord]


def test_run_once_native_loop_false_forces_legacy_v4(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit False wins over the env var (--legacy-v4 pinning)."""
    captured: dict[str, Any] = {}

    async def fake_run_async(prompt: str, **kwargs: Any) -> int:
        captured.update({"prompt": prompt, **kwargs})
        return headless.EXIT_OK

    monkeypatch.setattr(headless, "_run_async", fake_run_async)
    monkeypatch.setenv(headless.NATIVE_LOOP_ENV, "1")
    console, _stream = _console()
    code = headless.run_once("legacy please", native_loop=False, console=console)
    assert code == headless.EXIT_OK
    assert captured["native_loop"] is False


def test_cli_default_runs_the_native_loop(
    native_dirs: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """dpyro --headless PROMPT with no switch lands on the native loop."""
    provider = ScriptedProvider(
        [ScriptedTurn(events=[TextDelta("native answer"), finish("stop"), usage(2, 1)])]
    )
    runtime = FakeNativeRuntime(provider)

    async def fake_boot(**kwargs: Any) -> FakeNativeRuntime:
        return runtime

    import deeppyro.paths as paths_module

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)
    monkeypatch.setattr(paths_module, "migrate_legacy_layout", lambda: {})
    assert cli_main(["--headless", "say hello"]) == headless.EXIT_OK
    assert "native answer" in capsys.readouterr().out
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
