"""Headless one-shot mode: sinks, validation and the run_once exit codes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from deeppyro import headless
from deeppyro.api import PluginDependencyError
from deeppyro.config import AgentConfig, Config, ModelConfig, ProviderConfig
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.hooks import SESSION_CREATED, SESSION_DISPOSED, HookService
from deeppyro.llm import LlmRequestError, ModelInfo
from deeppyro.llm.errors import CODE_TRANSPORT


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
        self.agent_presets: list[str | None] = []

    def create(self, cwd: str, *, agent_preset: str | None = None) -> FakeSession:
        self.created.append(cwd)
        self.agent_presets.append(agent_preset)
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
    def __init__(
        self,
        agent: FakeAgent,
        *,
        jobs: Any = None,
        team: Any = None,
        preset: str | None = "standard",
    ) -> None:
        self.cwd = Path("D:/work")
        self.provider_name = "fake"
        self.model = "fake-1"
        self.context_window = 8192
        self.preset = preset
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


# --------------------------------------------------------------------------
# boot_runtime: model catalog discovery (P1 A3a)
# --------------------------------------------------------------------------


class FakeCatalogRegistry:
    def __init__(self, error: Exception | None = None) -> None:
        self.last_catalog_error = error


class FakeLlmService:
    """Minimal stand-in for builtin.llm.LlmPlugin."""

    def __init__(
        self,
        *,
        infos: list[ModelInfo] | None = None,
        error: Exception | None = None,
        catalog_error: Exception | None = None,
    ) -> None:
        self.infos = {info.id: info for info in infos or []}
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.registry = FakeCatalogRegistry(catalog_error)

    def provider(self, name: str) -> object:
        self.provider_name = name
        return object()

    async def model_info(self, provider: str, model_id: str) -> ModelInfo | None:
        self.calls.append((provider, model_id))
        if self.error is not None:
            raise self.error
        return self.infos.get(model_id)


class NoDiscoveryLlmService(FakeLlmService):
    model_info = None  # type: ignore[assignment] - providers without list_models


class FakeSandbox:
    def gate(self, workspace: Path, *, ask: Any) -> object:
        self.ask = ask
        return object()


class FakeToolService:
    def __init__(self) -> None:
        self.registry_calls: list[tuple[Path, dict[str, Any]]] = []

    def registry_for(self, workspace: Path, **kwargs: Any) -> object:
        self.registry_calls.append((workspace, kwargs))
        return object()


class FakePresets:
    """Preset registry double: standard + ptc with the legacy 'default' alias."""

    def __init__(self) -> None:
        self.ensured: list[tuple[str, int | None]] = []
        self._specs = {
            "standard": SimpleNamespace(id="standard", revision=0, plugins=()),
            "ptc": SimpleNamespace(id="ptc", revision=2, plugins=()),
        }

    def canonical(self, preset_id: str | None) -> str | None:
        if not preset_id or not preset_id.strip():
            return None
        wanted = preset_id.strip()
        return "standard" if wanted == "default" else wanted

    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> Any:
        wanted = self.canonical(preset_id) or "standard"
        try:
            return self._specs[wanted]
        except KeyError:
            raise PluginDependencyError(f"unknown agent preset: {wanted!r}") from None

    async def ensure(self, preset_id: str | None = None, *, revision: int | None = None) -> Any:
        spec = self.resolve(preset_id, revision=revision)
        self.ensured.append((str(spec.id), revision))
        return spec


class FakeKernelApp:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services
        self.stopped = False

    def service(self, name: str) -> Any:
        try:
            return self._services[name]
        except KeyError:
            raise LookupError(name) from None

    async def stop(self) -> None:
        self.stopped = True


def make_config(*, models: list[ModelConfig] | None = None, agent_model: str = "declared-1") -> Config:
    entries = models or [ModelConfig(id="declared-1", contextWindow=32768)]
    return Config(
        providers={
            "local": ProviderConfig(baseURL="http://127.0.0.1:8000/v1", models=entries),
        },
        agent=AgentConfig(provider="local", model=agent_model),
    )


def make_kernel_app(
    llm: FakeLlmService,
    *,
    tools: FakeToolService | None = None,
    presets: FakePresets | None = None,
) -> FakeKernelApp:
    return FakeKernelApp(
        {
            "llm": llm,
            "sessions": type("Sessions", (), {"store": object()})(),
            "sandbox": FakeSandbox(),
            "tools": tools or FakeToolService(),
            "agents": RecordingAgents(),
            "presets": presets or FakePresets(),
        }
    )


def patch_kernel_boot(monkeypatch: pytest.MonkeyPatch, kernel_app: FakeKernelApp) -> None:
    import deeppyro.kernel

    async def fake_boot(cfg: Config) -> Any:
        return kernel_app

    monkeypatch.setattr(deeppyro.kernel, "boot", fake_boot)


async def test_boot_runtime_uses_discovered_model_context_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeLlmService(infos=[ModelInfo(provider="local", id="discovered-1", context_window=200_000)])
    kernel_app = make_kernel_app(llm)
    patch_kernel_boot(monkeypatch, kernel_app)
    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path, model="discovered-1")
    try:
        assert runtime.model == "discovered-1"
        assert runtime.context_window == 200_000
        assert llm.calls == [("local", "discovered-1")]
    finally:
        await runtime.aclose()
    assert kernel_app.stopped is True


async def test_boot_runtime_falls_back_when_discovery_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeLlmService(error=TimeoutError("catalog endpoint timed out"))
    kernel_app = make_kernel_app(llm)
    patch_kernel_boot(monkeypatch, kernel_app)
    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path, model="catalog-only")
    try:
        assert runtime.model == "catalog-only"
        assert runtime.context_window == headless.DEFAULT_CONTEXT_WINDOW
    finally:
        await runtime.aclose()


async def test_boot_runtime_falls_back_when_discovery_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failure = LlmRequestError("local: offline", provider="local", code=CODE_TRANSPORT, retryable=True)
    llm = FakeLlmService(catalog_error=failure)
    kernel_app = make_kernel_app(llm)
    patch_kernel_boot(monkeypatch, kernel_app)
    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path, model="unlisted")
    try:
        assert runtime.model == "unlisted"
        assert runtime.context_window == headless.DEFAULT_CONTEXT_WINDOW
    finally:
        await runtime.aclose()


async def test_boot_runtime_rejects_model_missing_from_a_definitive_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeLlmService(infos=[ModelInfo(provider="local", id="declared-1", context_window=32768)])
    kernel_app = make_kernel_app(llm)
    patch_kernel_boot(monkeypatch, kernel_app)
    with pytest.raises(ConfigError, match="unknown model: ghost"):
        await headless.boot_runtime(config=make_config(), cwd=tmp_path, model="ghost")
    assert kernel_app.stopped is True


async def test_boot_runtime_rejects_unknown_model_without_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kernel_app = make_kernel_app(NoDiscoveryLlmService())
    patch_kernel_boot(monkeypatch, kernel_app)
    with pytest.raises(ConfigError, match="unknown model: ghost"):
        await headless.boot_runtime(config=make_config(), cwd=tmp_path, model="ghost")
    assert kernel_app.stopped is True


async def test_boot_runtime_defaults_window_when_catalog_entry_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeLlmService(infos=[ModelInfo(provider="local", id="windowless-1")])
    kernel_app = make_kernel_app(llm)
    patch_kernel_boot(monkeypatch, kernel_app)
    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path, model="windowless-1")
    try:
        assert runtime.context_window == headless.DEFAULT_CONTEXT_WINDOW
    finally:
        await runtime.aclose()


async def test_boot_runtime_uses_configured_window_without_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeLlmService()
    kernel_app = make_kernel_app(llm)
    patch_kernel_boot(monkeypatch, kernel_app)
    config = make_config(models=[ModelConfig(id="declared-1", contextWindow=65536)])
    runtime = await headless.boot_runtime(config=config, cwd=tmp_path, model="declared-1")
    try:
        assert runtime.context_window == 65536
        assert llm.calls == []
    finally:
        await runtime.aclose()


async def test_boot_runtime_default_model_never_queries_the_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeLlmService()
    kernel_app = make_kernel_app(llm)
    patch_kernel_boot(monkeypatch, kernel_app)
    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path)
    try:
        assert runtime.model == "declared-1"
        assert runtime.context_window == 32768
        assert llm.calls == []
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------
# session lifecycle hooks (task-63)
# --------------------------------------------------------------------------


class FakeHookedRuntime(FakeRuntime):
    """FakeRuntime plus a kernel app exposing the hooks service."""

    def __init__(self, agent: FakeAgent, hooks: HookService) -> None:
        super().__init__(agent)
        self.kernel_app = FakeKernelApp({"hooks": hooks})
        self.order: list[str] = []

    async def aclose(self) -> None:
        self.order.append("aclose")
        await super().aclose()


def record_hooks(hooks: HookService, order: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []
    disposed: list[dict[str, Any]] = []

    def on_created(payload: dict[str, Any]) -> None:
        created.append(dict(payload))
        order.append("created")

    def on_disposed(payload: dict[str, Any]) -> None:
        disposed.append(dict(payload))
        order.append("disposed")

    hooks.on(SESSION_CREATED, on_created)
    hooks.on(SESSION_DISPOSED, on_disposed)
    return created, disposed


def test_run_once_emits_session_created_then_disposed(monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = HookService()
    runtime = FakeHookedRuntime(FakeAgent([{"type": "final", "text": "done"}]), hooks)
    created, disposed = record_hooks(hooks, runtime.order)
    patch_runtime(monkeypatch, runtime)
    console, _stream = make_console()

    assert headless.run_once("q", console=console) == headless.EXIT_OK

    assert created == [{"sessionId": FakeSession.session_id, "cwd": str(runtime.cwd), "preset": "standard"}]
    assert disposed == [{"sessionId": FakeSession.session_id}]
    assert runtime.order == ["created", "disposed", "aclose"]


def test_run_once_emits_disposed_on_agent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = HookService()
    runtime = FakeHookedRuntime(FakeAgent([], fail=True), hooks)
    created, disposed = record_hooks(hooks, runtime.order)
    patch_runtime(monkeypatch, runtime)
    console, _stream = make_console()

    assert headless.run_once("q", console=console) == headless.EXIT_ERROR

    assert len(created) == 1
    assert disposed == [{"sessionId": FakeSession.session_id}]


def test_run_once_survives_failing_session_hook_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = HookService()

    def explode(payload: dict[str, Any]) -> None:
        raise RuntimeError("listener broke")

    hooks.on(SESSION_CREATED, explode)
    hooks.on(SESSION_DISPOSED, explode)
    runtime = FakeHookedRuntime(FakeAgent([{"type": "final", "text": "done"}]), hooks)
    patch_runtime(monkeypatch, runtime)
    console, _stream = make_console()

    assert headless.run_once("q", console=console) == headless.EXIT_OK
    assert runtime.closed is True


# --------------------------------------------------------------------------
# agent preset selection (task-73)
# --------------------------------------------------------------------------


def test_run_once_defaults_to_the_standard_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    hooks = HookService()
    runtime = FakeHookedRuntime(FakeAgent([{"type": "final", "text": "done"}]), hooks)
    created, _disposed = record_hooks(hooks, [])
    patch_runtime(monkeypatch, runtime)
    console, _stream = make_console()

    assert headless.run_once("q", console=console) == headless.EXIT_OK

    assert runtime.store.agent_presets == ["standard"]
    assert created[0]["preset"] == "standard"


def test_run_once_forwards_an_explicit_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = FakeAgent([{"type": "final", "text": "done"}])
    runtime = FakeRuntime(agent, preset="ptc")
    seen: dict[str, Any] = {}

    async def fake_boot(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return runtime

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)
    console, _stream = make_console()

    assert headless.run_once("q", preset="ptc", console=console) == headless.EXIT_OK

    assert seen["preset"] == "ptc"
    assert runtime.store.agent_presets == ["ptc"]


def test_run_once_reports_an_unknown_preset_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_boot(**kwargs: Any) -> Any:
        raise headless.UnknownPresetError("unknown agent preset: 'ghost'")

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)
    console, stream = make_console()

    assert headless.run_once("q", preset="ghost", console=console) == headless.EXIT_USAGE

    captured = capsys.readouterr()
    assert "unknown agent preset" in captured.err
    assert captured.out == ""
    assert stream.getvalue() == ""


def test_run_once_json_unknown_preset_writes_no_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_boot(**kwargs: Any) -> Any:
        raise headless.UnknownPresetError("unknown agent preset: 'ghost'")

    monkeypatch.setattr(headless, "boot_runtime", fake_boot)
    console, _stream = make_console()

    assert headless.run_once("q", preset="ghost", json_output=True, console=console) == headless.EXIT_USAGE

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown agent preset" in captured.err


def test_create_agent_uses_a_preset_scoped_registry() -> None:
    agents = RecordingAgents()
    tools = FakeToolService()
    spec = SimpleNamespace(id="ptc", revision=2)
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
        tools=tools,
        preset="ptc",
        preset_spec=spec,
    )

    runtime.create_agent(FakeSession(), sink=object())

    assert Path(tools.registry_calls[0][0]) == Path("D:/work")
    assert tools.registry_calls[0][1] == {"scope": "ptc", "revision": 2}
    assert agents.created[-1]["preset"] == "ptc"
    assert agents.created[-1]["preset_revision"] == 2


async def test_boot_runtime_resolves_and_ensures_the_requested_preset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools = FakeToolService()
    presets = FakePresets()
    kernel_app = make_kernel_app(FakeLlmService(), tools=tools, presets=presets)
    patch_kernel_boot(monkeypatch, kernel_app)

    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path, preset="ptc")
    try:
        assert runtime.preset == "ptc"
        assert runtime.preset_spec.revision == 2
        assert presets.ensured == [("ptc", 2)]
        runtime.create_agent(FakeSession(), sink=object())
        assert tools.registry_calls[-1][1] == {"scope": "ptc", "revision": 2}
    finally:
        await runtime.aclose()


async def test_boot_runtime_defaults_to_the_standard_preset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools = FakeToolService()
    presets = FakePresets()
    kernel_app = make_kernel_app(FakeLlmService(), tools=tools, presets=presets)
    patch_kernel_boot(monkeypatch, kernel_app)

    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path)
    try:
        assert runtime.preset == "standard"
        assert runtime.preset_spec.revision == 0
        runtime.create_agent(FakeSession(), sink=object())
        assert tools.registry_calls[-1][1] == {"scope": "standard", "revision": 0}
    finally:
        await runtime.aclose()


async def test_boot_runtime_maps_the_legacy_default_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kernel_app = make_kernel_app(FakeLlmService())
    patch_kernel_boot(monkeypatch, kernel_app)

    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path, preset="default")
    try:
        assert runtime.preset == "standard"
    finally:
        await runtime.aclose()


async def test_boot_runtime_rejects_an_unknown_preset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kernel_app = make_kernel_app(FakeLlmService())
    patch_kernel_boot(monkeypatch, kernel_app)

    with pytest.raises(headless.UnknownPresetError, match="unknown agent preset"):
        await headless.boot_runtime(config=make_config(), cwd=tmp_path, preset="ghost")
    assert kernel_app.stopped is True


def test_build_parser_accepts_preset() -> None:
    from deeppyro.tui.main import build_parser

    parser = build_parser()
    assert parser.parse_args(["--headless", "hi"]).preset is None
    assert parser.parse_args(["--headless", "hi", "--preset", "ptc"]).preset == "ptc"


def test_cli_forwards_the_preset_to_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import deeppyro.paths
    import deeppyro.tui.main as cli

    seen: list[dict[str, Any]] = []

    def fake_run_once(prompt: str, **kwargs: Any) -> int:
        seen.append({"prompt": prompt, **kwargs})
        return 0

    monkeypatch.setattr(deeppyro.paths, "migrate_legacy_layout", lambda: None)
    monkeypatch.setattr(headless, "run_once", fake_run_once)

    assert cli.main(["--headless", "hi", "--preset", "ptc"]) == 0

    assert seen[0]["preset"] == "ptc"
