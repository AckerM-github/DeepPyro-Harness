"""Kernel-backed assembly: build_app, service resolution and lifecycle."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

app_module = importlib.import_module("deeppyro.cli.app")
from deeppyro.cli.app import (
    CliApp,
    _context_window,
    build_app,
    list_session_infos,
    make_terminal_ask,
    resolve_awaitable,
    resolve_workspace,
)
from deeppyro.cli.frontend import TerminalFrontend
from deeppyro.config import ModelConfig, ProviderConfig
from deeppyro.core.errors import ConfigError
from tests.cli.fakes import FakeStore, make_config, make_terminal_frontend


class FakeServiceApp:
    """Kernel App stand-in: named services, tracked stop()."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services
        self.stopped = 0

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered or not active")
        return self._services[name]

    async def stop(self) -> None:
        self.stopped += 1


class FakeAgents:
    def __init__(self) -> None:
        self.options_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def options(self, **overrides: Any) -> SimpleNamespace:
        self.options_calls.append(overrides)
        return SimpleNamespace(**overrides)

    def create(self, **kwargs: Any) -> str:
        self.create_calls.append(kwargs)
        return "agent"


class FakeTools:
    def registry_for(self, workspace: Path) -> str:
        self.workspace = workspace
        return f"registry:{workspace}"


class FakeSandbox:
    def gate(self, workspace: Path, ask: Any = None) -> SimpleNamespace:
        self.workspace = workspace
        self.ask = ask
        return SimpleNamespace(workspace=workspace, ask=ask)


class FakeLlm:
    def provider(self, name: str) -> str:
        self.name = name
        return f"provider:{name}"


def make_services(*, frontend: TerminalFrontend | None = None) -> dict[str, Any]:
    return {
        "frontend": frontend or TerminalFrontend(),
        "llm": FakeLlm(),
        "sessions": SimpleNamespace(store=FakeStore()),
        "sandbox": FakeSandbox(),
        "tools": FakeTools(),
        "agents": FakeAgents(),
    }


def test_resolve_awaitable_handles_sync_and_async() -> None:
    assert resolve_awaitable(7) == 7

    async def value() -> str:
        return "async"

    assert resolve_awaitable(value()) == "async"


def test_context_window_matches_model_then_falls_back() -> None:
    provider = ProviderConfig(
        api="openai-completions",
        baseURL="http://x/v1",
        models=[
            ModelConfig(id="a", name="A", contextWindow=1000),
            ModelConfig(id="b", name="B", contextWindow=2000),
        ],
    )
    assert _context_window(provider, "b") == 2000
    assert _context_window(provider, "missing") == 1000
    empty = ProviderConfig(api="openai-completions", baseURL="http://x/v1")
    assert _context_window(empty, "any") == 32768


def test_build_app_resolves_kernel_services(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = make_config()
    services = make_services()
    kernel_app = FakeServiceApp(services)

    async def fake_boot(config: Any = None) -> FakeServiceApp:
        assert config is cfg
        return kernel_app

    monkeypatch.setattr(app_module, "boot_services", fake_boot)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    built = asyncio.run(build_app(cwd=workspace, config=cfg, frontend=services["frontend"]))

    assert built.cfg is cfg
    assert built.cwd == workspace.resolve()
    assert built.provider_name == "local"
    assert built.model == "local-model"
    assert built.context_window == 8192
    assert built.provider == "provider:local"
    assert built.registry == f"registry:{workspace.resolve()}"
    assert built.permission.workspace == workspace.resolve()
    assert built.agents is services["agents"]
    assert built.kernel_app is kernel_app
    assert built.frontend is services["frontend"]

    asyncio.run(built.aclose())
    assert kernel_app.stopped == 1
    asyncio.run(built.aclose())
    assert kernel_app.stopped == 1


def test_build_app_uses_service_frontend_when_not_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = make_config()
    frontend = TerminalFrontend()
    kernel_app = FakeServiceApp(make_services(frontend=frontend))

    async def fake_boot(config: Any = None) -> FakeServiceApp:
        return kernel_app

    monkeypatch.setattr(app_module, "boot_services", fake_boot)
    built = asyncio.run(build_app(cwd=tmp_path, config=cfg))
    assert built.frontend is frontend


def test_build_app_unwraps_frontend_plugin_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = make_config()
    frontend = TerminalFrontend()
    services = make_services()
    services["frontend"] = SimpleNamespace(frontend=frontend)
    kernel_app = FakeServiceApp(services)

    async def fake_boot(config: Any = None) -> FakeServiceApp:
        return kernel_app

    monkeypatch.setattr(app_module, "boot_services", fake_boot)
    built = asyncio.run(build_app(cwd=tmp_path, config=cfg))
    assert built.frontend is frontend
    assert built.permission.ask is not None


def test_build_app_provider_and_model_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = make_config()
    kernel_app = FakeServiceApp(make_services())

    async def fake_boot(config: Any = None) -> FakeServiceApp:
        return kernel_app

    monkeypatch.setattr(app_module, "boot_services", fake_boot)
    built = asyncio.run(
        build_app(
            cwd=tmp_path,
            config=cfg,
            provider="deepseek",
            model="deepseek-chat",
        )
    )
    assert built.provider_name == "deepseek"
    assert built.model == "deepseek-chat"
    assert built.provider == "provider:deepseek"
    assert built.context_window == 65536


def test_build_app_unknown_provider_does_not_boot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def exploding_boot(config: Any = None) -> Any:  # pragma: no cover - must not run
        raise AssertionError("boot must not run for an invalid provider")

    monkeypatch.setattr(app_module, "boot_services", exploding_boot)
    with pytest.raises(ConfigError, match="unknown provider"):
        asyncio.run(build_app(cwd=tmp_path, config=make_config(), provider="nope"))


def test_build_app_missing_workspace_is_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def exploding_boot(config: Any = None) -> Any:  # pragma: no cover - must not run
        raise AssertionError("boot must not run for a missing workspace")

    monkeypatch.setattr(app_module, "boot_services", exploding_boot)
    with pytest.raises(ConfigError, match="workspace directory does not exist"):
        asyncio.run(build_app(cwd=tmp_path / "missing", config=make_config()))


def test_build_app_disposes_kernel_when_service_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = make_config()
    services = make_services()
    del services["agents"]
    kernel_app = FakeServiceApp(services)

    async def fake_boot(config: Any = None) -> FakeServiceApp:
        return kernel_app

    monkeypatch.setattr(app_module, "boot_services", fake_boot)
    with pytest.raises(LookupError):
        asyncio.run(build_app(cwd=tmp_path, config=cfg))
    assert kernel_app.stopped == 1


def test_cli_app_create_agent_delegates_to_agents_service() -> None:
    agents = FakeAgents()
    built = CliApp(
        cfg=make_config(),
        cwd=Path("D:/w"),
        provider_name="local",
        model="local-model",
        context_window=8192,
        store=FakeStore(),
        provider="provider",
        registry="registry",
        permission="permission",
        frontend=TerminalFrontend(),
        agents=agents,
    )
    built.create_agent("session", "sink", max_steps=3)
    assert agents.options_calls == [{"context_window": 8192, "max_steps": 3}]
    call = agents.create_calls[0]
    assert call["session"] == "session"
    assert call["provider"] == "provider"
    assert call["registry"] == "registry"
    assert call["sink"] == "sink"
    assert call["permission"] == "permission"
    assert call["cwd"] == str(Path("D:/w"))


def test_cli_app_create_agent_defaults_to_config_max_steps() -> None:
    agents = FakeAgents()
    built = CliApp(
        cfg=make_config(),
        cwd=Path("D:/w"),
        provider_name="local",
        model="local-model",
        context_window=8192,
        store=FakeStore(),
        provider="p",
        registry="r",
        permission="perm",
        frontend=TerminalFrontend(),
        agents=agents,
    )
    built.create_agent("session", "sink")
    assert agents.options_calls == [{"context_window": 8192}]


def test_cli_app_session_helpers() -> None:
    store = FakeStore()
    built = CliApp(
        cfg=make_config(),
        cwd=Path("D:/w"),
        provider_name="local",
        model="local-model",
        context_window=8192,
        store=store,
        provider="p",
        registry="r",
        permission="perm",
        frontend=TerminalFrontend(),
        agents=FakeAgents(),
    )
    session = built.new_session()
    assert store.created == ["D:\\w"]
    assert built.open_session(session.session_id) is session
    assert built.list_sessions(limit=1) == store.list()[:1]


def test_make_terminal_ask_denies_when_non_interactive() -> None:
    frontend, _, err = make_terminal_frontend()
    ask = make_terminal_ask(frontend)
    allowed = asyncio.run(ask(SimpleNamespace(summary="rm -rf /")))
    assert allowed is False
    assert "approval denied (non-interactive)" in err.getvalue()


def test_make_terminal_ask_prompts_on_interactive_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = TerminalFrontend()
    monkeypatch.setattr(type(frontend.console), "is_terminal", property(lambda self: True), raising=False)
    answers = iter(["y"])

    def fake_ask(*args: Any, **kwargs: Any) -> str:
        return next(answers)

    monkeypatch.setattr(app_module, "Prompt", SimpleNamespace(ask=fake_ask))
    ask = make_terminal_ask(frontend)
    request = SimpleNamespace(kind="shell", summary="run tests", detail="pytest -q")
    assert asyncio.run(ask(request)) is True


def test_list_session_infos_boots_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    infos = ["a", "b"]
    kernel_app = FakeServiceApp({"sessions": SimpleNamespace(list=lambda: infos)})

    async def fake_boot(config: Any = None) -> FakeServiceApp:
        return kernel_app

    monkeypatch.setattr(app_module, "boot_services", fake_boot)
    assert list_session_infos(make_config()) == infos
    assert kernel_app.stopped == 1


def test_list_session_infos_stops_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def list(self) -> list[Any]:
            raise RuntimeError("boom")

    kernel_app = FakeServiceApp({"sessions": Broken()})

    async def fake_boot(config: Any = None) -> FakeServiceApp:
        return kernel_app

    monkeypatch.setattr(app_module, "boot_services", fake_boot)
    with pytest.raises(RuntimeError, match="boom"):
        list_session_infos(make_config())
    assert kernel_app.stopped == 1


def test_resolve_workspace_accepts_existing_directory(tmp_path: Path) -> None:
    assert resolve_workspace(tmp_path) == tmp_path.resolve()
