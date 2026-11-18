"""Headless one-shot mode through the M2 native loop (the default since 0.2.0)."""

from __future__ import annotations

import functools
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console

from deeppyro import headless
from deeppyro.builtin.usage import UsageService
from deeppyro.config import AgentConfig, Config, ModelConfig, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.core.ids import new_session_id
from deeppyro.core.llm import (
    Finish,
    ModelRequest,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
    UsageEvent,
)
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.llm import native_anthropic
from deeppyro.llm import registry as llm_registry
from deeppyro.llm.registry import LlmRegistry
from deeppyro.observability.protocols import TraceExportSink
from deeppyro.observability.trace import ModelCallRecord, RunRecord
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeCodec
from deeppyro.tools.registry import ToolRegistry
from deeppyro.tui.main import build_parser
from deeppyro.tui.main import main as cli_main


class FakeTool:
    """A tool with a configurable text result and recorded calls."""

    def __init__(self, name: str, *, text: str = "ok", error: BaseException | None = None) -> None:
        self.name = name
        self.description = f"{name} tool"
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}
        self.parallel_safe = True
        self.text = text
        self.error = error
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append((dict(args), ctx))
        if self.error is not None:
            raise self.error
        return ToolResult(text=self.text)


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """One scripted native provider response."""

    events: tuple[StreamEvent, ...]


class ScriptedProvider:
    """Native LlmProvider double that replays scripted turns and records requests."""

    name = "scripted"

    def __init__(self, script: list[ScriptedTurn]) -> None:
        self.script = list(script)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        turn = self.script.pop(0)
        for event in turn.events:
            yield event

    async def aclose(self) -> None:
        return None


def finish(stop_reason: StopReason = StopReason.STOP) -> Finish:
    return Finish(stop_reason=stop_reason)


def usage(input_tokens: int, output_tokens: int, *, cached: int | None = None) -> UsageEvent:
    return UsageEvent(
        Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_input_tokens=cached,
        )
    )


class FakeNativeRuntime:
    """Stand-in HeadlessRuntime exposing only what the native path uses."""

    def __init__(
        self, provider: ScriptedProvider, tools: tuple[Any, ...] = (), *, preset: str = "standard"
    ) -> None:
        self.cwd = Path.cwd()
        self.native_provider = provider
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
    return tmp_path


def test_cli_rejects_the_removed_backend_switches() -> None:
    parser = build_parser()
    assert not hasattr(parser.parse_args(["hi"]), "native_loop")
    assert not hasattr(parser.parse_args(["hi"]), "legacy_v4")
    for flag in ("--native-loop", "--legacy-v4"):
        with pytest.raises(SystemExit):
            parser.parse_args(["hi", flag])


async def test_native_headless_run_persists_native_session(native_dirs: Path) -> None:
    provider = ScriptedProvider(
        [ScriptedTurn(events=[TextDelta("hello from native"), finish("stop"), usage(7, 3, cached=2)])]
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
    assert events[-1] == {"type": "usage", "input": 7, "output": 3, "cached": 2}
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
                    usage(4, 2),
                    finish(StopReason.TOOL_CALLS),
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
                    usage(2, 1),
                    finish(StopReason.TOOL_CALLS),
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
        native_provider=provider,
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


# --------------------------------------------------------------------------
# native provider resolution at boot (KI-010)
# --------------------------------------------------------------------------


class _ServiceWithoutNativeFactory:
    """Llm service double from before the native provider existed."""


def test_resolve_native_provider_reports_a_missing_factory() -> None:
    with pytest.raises(ConfigError, match="native provider factory"):
        headless._resolve_native_provider(_ServiceWithoutNativeFactory(), "local")


def test_resolve_native_provider_returns_the_provider() -> None:
    class Service:
        def native_provider(self, name: str) -> str:
            assert name == "local"
            return "native-provider"

    assert headless._resolve_native_provider(Service(), "local") == "native-provider"


def test_resolve_native_provider_turns_a_config_error_into_a_reason() -> None:
    class Service:
        def native_provider(self, name: str) -> object:
            raise ConfigError("provider 'claude' has no native transport")

    with pytest.raises(ConfigError) as info:
        headless._resolve_native_provider(Service(), "claude")
    assert "native provider unavailable for provider 'claude'" in str(info.value)
    assert "provider 'claude' has no native transport" in str(info.value)


def test_native_provider_helper_returns_the_native_attribute() -> None:
    provider = ScriptedProvider([])
    runtime = headless.HeadlessRuntime(
        cfg=None,
        cwd=Path.cwd(),
        kernel_app=None,
        provider_name="scripted",
        model="fake-model",
        context_window=32768,
        native_provider=provider,
        registry=ToolRegistry(()),
        permission=None,
        agents=None,
        preset="standard",
    )
    assert headless._native_provider(runtime) is provider


# --------------------------------------------------------------------------
# native anthropic selection (no v4 fallback)
# --------------------------------------------------------------------------


def _anthropic_sse(*deltas: str) -> bytes:
    """One minimal Anthropic Messages SSE stream emitting text deltas."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ]
    for delta in deltas:
        events.append(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": delta}}
        )
    events.extend(
        [
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
    )
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


def test_anthropic_config_selects_the_native_provider_without_a_v4_fallback(
    native_dirs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An anthropic config resolves the native transport through the registry."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_anthropic_sse("native ", "answer"),
        )

    factory = functools.partial(
        native_anthropic.NativeAnthropicProvider, transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(llm_registry, "NativeAnthropicProvider", factory)
    cfg = Config(
        providers={
            "claude": ProviderConfig(
                api="anthropic-messages",
                base_url="https://api.anthropic.com",
                api_key="test-key",
                models=[ModelConfig(id="claude-sonnet-4-5", context_window=200_000)],
            )
        },
        agent=AgentConfig(provider="claude", model="claude-sonnet-4-5"),
    )
    registry = LlmRegistry(cfg)

    class _LlmService:
        def native_provider(self, name: str) -> object:
            return registry.native_provider(name)

    class _LegacySentinel:
        """Legacy v4 provider that must never stream when the native one exists."""

        name = "claude"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            raise AssertionError("the legacy v4 provider must not be used")
            yield  # pragma: no cover

        async def aclose(self) -> None:
            return None

    legacy = _LegacySentinel()
    runtime = FakeNativeRuntime(legacy)
    runtime.native_provider_error = None

    async def fake_boot(**kwargs: Any) -> FakeNativeRuntime:
        provider = headless._resolve_native_provider(_LlmService(), "claude")
        assert isinstance(provider, native_anthropic.NativeAnthropicProvider)
        runtime.native_provider = provider
        return runtime

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)
    console, stream = _console()
    assert headless.run_once("say hello", console=console) == headless.EXIT_OK
    output = stream.getvalue()
    assert "native answer" in output
    assert "falling back" not in capsys.readouterr().err
    assert legacy.calls == 0
    assert runtime.closed is True
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.anthropic.com/v1/messages"
    assert seen[0].headers["x-api-key"] == "test-key"


# --------------------------------------------------------------------------
# boot_runtime rejects a native-loop provider it cannot construct
# --------------------------------------------------------------------------


class _BootKernelApp:
    """Kernel app double mapping boot-time contracts by class name."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services
        self.stopped = False

    def require(self, contract: type[Any]) -> Any:
        try:
            return self._services[contract.__name__]
        except KeyError:
            raise LookupError(contract.__name__) from None

    def get(self, contract: type[Any]) -> Any:
        return self._services.get(contract.__name__)

    async def stop(self) -> None:
        self.stopped = True


async def test_boot_runtime_rejects_a_provider_without_a_native_transport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _LlmService:
        def provider(self, name: str) -> object:
            return object()

        def native_provider(self, name: str) -> object:
            raise ConfigError(
                f"provider {name!r} uses api 'anthropic-messages' but has no API key; "
                "export DEEPPYRO_MISSING_ANTHROPIC_KEY"
            )

    class _Sessions:
        store = object()

    class _Sandbox:
        def gate(self, workspace: Path, *, ask: Any) -> object:
            return object()

    class _Tools:
        def registry_for(self, workspace: Path) -> ToolRegistry:
            return ToolRegistry(())

    kernel_app = _BootKernelApp(
        {
            "LlmPlugin": _LlmService(),
            "SessionsPlugin": _Sessions(),
            "SandboxPlugin": _Sandbox(),
            "ToolsPlugin": _Tools(),
            "AgentsPlugin": object(),
        }
    )

    import deeppyro.kernel

    async def fake_boot(cfg: Config) -> _BootKernelApp:
        return kernel_app

    monkeypatch.setattr(deeppyro.kernel, "boot", fake_boot)
    cfg = Config(
        providers={
            "claude": ProviderConfig(
                api="anthropic-messages",
                base_url="https://api.anthropic.com",
                api_key_env="DEEPPYRO_MISSING_ANTHROPIC_KEY",
                models=[ModelConfig(id="claude-sonnet-4-5", context_window=200_000)],
            )
        },
        agent=AgentConfig(provider="claude", model="claude-sonnet-4-5"),
    )
    monkeypatch.delenv("DEEPPYRO_MISSING_ANTHROPIC_KEY", raising=False)

    with pytest.raises(ConfigError, match="native provider unavailable for provider 'claude'") as info:
        await headless.boot_runtime(config=cfg, cwd=tmp_path)
    assert "DEEPPYRO_MISSING_ANTHROPIC_KEY" in str(info.value)
    assert kernel_app.stopped is True
