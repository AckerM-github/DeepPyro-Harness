"""Headless one-shot mode: sinks, validation, boot discovery and the CLI contract."""

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
from deeppyro.core.errors import ConfigError
from deeppyro.hooks import HookService
from deeppyro.llm import LlmRequestError, ModelInfo
from deeppyro.llm.errors import CODE_TRANSPORT
from tests.service_doubles import service_lookup, service_require


def make_console() -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    return Console(file=stream, width=100, force_terminal=False), stream


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
# run_once validation
# --------------------------------------------------------------------------


def test_run_once_rejects_blank_prompt() -> None:
    console, stream = make_console()
    assert headless.run_once("   ", console=console) == headless.EXIT_USAGE
    assert "no prompt" in stream.getvalue()


def test_run_once_rejects_bad_max_steps() -> None:
    console, stream = make_console()
    assert headless.run_once("hi", max_steps=0, console=console) == headless.EXIT_USAGE
    assert "--max-steps" in stream.getvalue()


def test_resolve_workspace_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(headless.ConfigError):
        headless.resolve_workspace(tmp_path / "missing")
    assert headless.resolve_workspace(tmp_path) == tmp_path.resolve()


# --------------------------------------------------------------------------
# boot_runtime: model catalog discovery
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

    def native_provider(self, name: str) -> object:
        self.native_provider_name = name
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

    def get(self, service: str | type[Any]) -> Any | None:
        return service_lookup(self._services, service)

    def require(self, service: type[Any]) -> Any:
        return service_require(self._services, service)

    async def stop(self) -> None:
        self.stopped = True


def make_config(*, models: list[ModelConfig] | None = None, agent_model: str = "declared-1") -> Config:
    entries = models or [ModelConfig(id="declared-1", context_window=32768)]
    return Config(
        providers={
            "local": ProviderConfig(base_url="http://127.0.0.1:8000/v1", models=entries),
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
            "sandbox": FakeSandbox(),
            "tools": tools or FakeToolService(),
            "agents": object(),
            "presets": presets or FakePresets(),
            "hooks": HookService(),
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
    config = make_config(models=[ModelConfig(id="declared-1", context_window=65536)])
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
# preset selection at boot
# --------------------------------------------------------------------------


async def test_boot_runtime_resolves_and_ensures_the_requested_preset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    presets = FakePresets()
    kernel_app = make_kernel_app(FakeLlmService(), presets=presets)
    patch_kernel_boot(monkeypatch, kernel_app)

    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path, preset="ptc")
    try:
        assert runtime.preset == "ptc"
        assert runtime.preset_spec.revision == 2
        assert presets.ensured == [("ptc", 2)]
    finally:
        await runtime.aclose()


async def test_boot_runtime_defaults_to_the_standard_preset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    presets = FakePresets()
    kernel_app = make_kernel_app(FakeLlmService(), presets=presets)
    patch_kernel_boot(monkeypatch, kernel_app)

    runtime = await headless.boot_runtime(config=make_config(), cwd=tmp_path)
    try:
        assert runtime.preset == "standard"
        assert runtime.preset_spec.revision == 0
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


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------


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


def test_build_parser_accepts_permission_flags() -> None:
    from deeppyro.tui.main import build_parser

    parser = build_parser()
    args = parser.parse_args(
        ["--headless", "hi", "--permission-mode", "read-only", "--approval-policy", "never"]
    )
    assert args.permission_mode == "read-only"
    assert args.approval_policy == "never"

    with pytest.raises(SystemExit):
        parser.parse_args(["--headless", "hi", "--permission-mode", "bogus"])


def test_cli_forwards_the_permission_flags_to_run_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import deeppyro.paths
    import deeppyro.tui.main as cli

    seen: list[dict[str, Any]] = []

    def fake_run_once(prompt: str, **kwargs: Any) -> int:
        seen.append({"prompt": prompt, **kwargs})
        return 0

    monkeypatch.setattr(deeppyro.paths, "migrate_legacy_layout", lambda: None)
    monkeypatch.setattr(headless, "run_once", fake_run_once)

    code = cli.main(
        [
            "--headless",
            "hi",
            "--permission-mode",
            "danger-full-access",
            "--approval-policy",
            "ask",
        ]
    )

    assert code == 0
    assert seen[0]["permission_mode"] == "danger-full-access"
    assert seen[0]["approval_policy"] == "ask"


def test_build_parser_rejects_the_removed_backend_switches() -> None:
    from deeppyro.tui.main import build_parser

    parser = build_parser()
    default = parser.parse_args(["--headless", "hi"])
    assert not hasattr(default, "native_loop")
    assert not hasattr(default, "legacy_v4")
    for flag in ("--legacy-v4", "--native-loop"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--headless", "hi", flag])
