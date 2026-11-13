"""Provider/model switching on the TUI runtime: validation, records, resume."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.config import Config
from deeppyro.core.errors import ConfigError
from deeppyro.tui import runtime as runtime_module
from deeppyro.tui.runtime import TuiRuntime, _recorded_model_selection, _resolve_model


def make_config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "local-model", "maxTokens": 4096},
            "providers": {
                "local": {
                    "baseURL": "http://127.0.0.1:8000/v1",
                    "models": [
                        {"id": "local-model", "contextWindow": 32768},
                        {"id": "local-small", "contextWindow": 8192},
                    ],
                },
                "deepseek": {
                    "baseURL": "https://api.deepseek.com/v1",
                    "apiKey": "sk-test",
                    "models": [{"id": "deepseek-chat", "contextWindow": 65536}],
                },
                "keyless": {"baseURL": "https://example.invalid/v1", "models": []},
            },
        }
    )


@dataclass
class FakeRecord:
    type: str
    data: dict[str, Any]


class FakeSession:
    session_id = "session-11112222-3333-4444-5555-666677778888"
    header = SimpleNamespace(agentPreset="")

    def __init__(self) -> None:
        self.records: list[FakeRecord] = []

    def append(self, record_type: str, data: dict[str, Any], **_: Any) -> int:
        self.records.append(FakeRecord(record_type, data))
        return len(self.records)


class FakeAgent:
    closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeAgents:
    def __init__(self) -> None:
        self.options_seen: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []

    def options(self, **overrides: Any) -> dict[str, Any]:
        self.options_seen.append(overrides)
        return overrides

    def create(self, **kwargs: Any) -> FakeAgent:
        self.created.append(kwargs)
        return FakeAgent()


class FakeLlm:
    def provider(self, name: str) -> str:
        return f"provider:{name}"


class FakeKernel:
    def __init__(self) -> None:
        self.services = {"llm": FakeLlm()}

    def service(self, name: str) -> Any:
        if name not in self.services:
            raise LookupError(name)
        return self.services[name]


class FakeStore:
    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession()
        self.opened: list[str] = []

    def create(self, cwd: str, *, agent_preset: str = "") -> FakeSession:
        self.session = FakeSession()
        return self.session

    def open(self, session_id: str) -> FakeSession:
        self.opened.append(session_id)
        return self.session


class FakePermission:
    preset = "workspace-write"
    approval = "ask"


def make_runtime(*, session: FakeSession | None = None) -> tuple[TuiRuntime, FakeAgents, FakeStore]:
    store = FakeStore(session)
    agents = FakeAgents()
    runtime = TuiRuntime(
        cfg=make_config(),
        cwd=Path("D:/work"),
        kernel_app=FakeKernel(),
        provider_name="local",
        model="local-model",
        context_window=32768,
        store=store,
        provider="provider:local",
        registry=object(),
        permission=FakePermission(),
        agents=agents,
        sink=None,
        ask=lambda request: None,
    )
    runtime.session = store.session
    return runtime, agents, store


async def test_set_provider_switches_client_model_and_context() -> None:
    runtime, agents, _ = make_runtime()
    provider_name, model_id = await runtime.set_provider("deepseek", persist=False)
    assert (provider_name, model_id) == ("deepseek", "deepseek-chat")
    assert runtime.provider == "provider:deepseek"
    assert runtime.provider_name == "deepseek"
    assert runtime.model == "deepseek-chat"
    assert runtime.context_window == 65536
    assert agents.created[-1]["provider"] == "provider:deepseek"
    record = runtime.session.records[-1]
    assert record.type == "model/selection"
    assert record.data == {"provider": "deepseek", "model": "deepseek-chat"}


async def test_set_provider_validates_names_and_models() -> None:
    runtime, _, _ = make_runtime()
    with pytest.raises(ConfigError):
        await runtime.set_provider("nope", persist=False)
    with pytest.raises(ConfigError):
        await runtime.set_provider("deepseek", model="nope", persist=False)
    with pytest.raises(ConfigError):
        await runtime.set_model("nope", persist=False)


async def test_set_model_switches_within_provider() -> None:
    runtime, _, _ = make_runtime()
    model_id = await runtime.set_model("local-small", persist=False)
    assert model_id == "local-small"
    assert runtime.context_window == 8192
    assert runtime.session.records[-1].type == "model/selection"


async def test_open_session_restores_recorded_selection() -> None:
    session = FakeSession()
    session.records.append(FakeRecord("model/selection", {"provider": "deepseek", "model": "deepseek-chat"}))
    runtime, agents, store = make_runtime(session=session)
    await runtime.open_session("session-abc")
    assert store.opened == ["session-abc"]
    assert runtime.provider_name == "deepseek"
    assert runtime.provider == "provider:deepseek"
    assert runtime.model == "deepseek-chat"
    assert runtime.context_window == 65536
    assert agents.created[-1]["provider"] == "provider:deepseek"


async def test_persist_failure_does_not_break_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _, _ = make_runtime()

    def boom(*args: Any, **kwargs: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(runtime_module, "save_agent_selection", boom)
    assert await runtime.set_model("local-small", persist=True) == "local-small"
    assert runtime.last_persist_error is not None
    assert "disk full" in runtime.last_persist_error


async def test_persist_writes_the_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _, _ = make_runtime()
    written: list[tuple[str, str]] = []

    def fake_save(provider: str, model: str, path: Any = None) -> Path:
        written.append((provider, model))
        return Path("config.toml")

    monkeypatch.setattr(runtime_module, "save_agent_selection", fake_save)
    await runtime.set_provider("deepseek", persist=True)
    assert written == [("deepseek", "deepseek-chat")]
    assert runtime.last_persist_error is None


def test_provider_and_model_introspection() -> None:
    runtime, _, _ = make_runtime()
    assert runtime.providers() == ["local", "deepseek", "keyless"]
    assert runtime.models() == ["local-model", "local-small"]
    assert runtime.models("deepseek") == ["deepseek-chat"]
    assert runtime.model_choices() == [
        ("local", "local-model"),
        ("local", "local-small"),
        ("deepseek", "deepseek-chat"),
    ]
    assert runtime.has_api_key("deepseek") is True
    assert runtime.has_api_key("keyless") is False
    assert runtime.has_api_key("nope") is False


def test_resolve_model_does_not_leak_the_configured_model_across_providers() -> None:
    cfg = make_config()
    assert _resolve_model(cfg, "local", model=None) == "local-model"
    assert _resolve_model(cfg, "deepseek", model=None) == "deepseek-chat"
    assert _resolve_model(cfg, "deepseek", model="deepseek-chat") == "deepseek-chat"
    with pytest.raises(ConfigError):
        _resolve_model(cfg, "deepseek", model="local-model")


class FakeTeam:
    def __init__(self) -> None:
        self.configures: list[dict[str, Any]] = []
        self.bound: list[Any] = []
        self.closed = False

    def configure(self, **kwargs: Any) -> None:
        self.configures.append(kwargs)

    def bind_session(self, session: Any) -> None:
        self.bound.append(session)

    async def close(self) -> None:
        self.closed = True


async def test_attach_configures_and_binds_the_team_service() -> None:
    runtime, _, _ = make_runtime()
    team = FakeTeam()
    runtime.team = team
    session = FakeSession()
    await runtime._attach(session)
    assert len(team.configures) == 1
    configure = team.configures[0]
    assert configure["cwd"] == Path("D:/work")
    assert configure["model"] == "local-model"
    assert configure["context_window"] == 32768
    assert configure["preset"] == "default"
    assert team.bound == [session]


async def test_close_cancels_jobs_and_closes_the_team() -> None:
    runtime, _, _ = make_runtime()
    team = FakeTeam()
    runtime.team = team
    await runtime.close()
    assert team.closed is True


def test_recorded_model_selection_reads_the_last_record() -> None:
    session = FakeSession()
    session.records.extend(
        [
            FakeRecord("model/selection", {"provider": "local", "model": "local-model"}),
            FakeRecord("turn/start", {"turn": 1}),
            FakeRecord("model/selection", {"provider": "deepseek", "model": "deepseek-chat"}),
        ]
    )
    assert _recorded_model_selection(session) == ("deepseek", "deepseek-chat")
    assert _recorded_model_selection(FakeSession()) is None


class FakeSubagent:
    def __init__(self) -> None:
        self.configures: list[dict[str, Any]] = []
        self.bound: list[Any] = []
        self.closed = False

    def configure(self, **kwargs: Any) -> None:
        self.configures.append(kwargs)

    def bind_session(self, session: Any) -> None:
        self.bound.append(session)

    async def close(self) -> None:
        self.closed = True


async def test_attach_configures_and_binds_the_subagent_service() -> None:
    runtime, _, _ = make_runtime()
    subagent = FakeSubagent()
    runtime.subagent = subagent
    session = FakeSession()
    await runtime._attach(session)
    assert len(subagent.configures) == 1
    configure = subagent.configures[0]
    assert configure["cwd"] == Path("D:/work")
    assert configure["model"] == "local-model"
    assert configure["preset"] == "default"
    assert configure["services"]["llm"].provider("local") == "provider:local"
    assert subagent.bound == [session]


async def test_close_closes_the_subagent_service() -> None:
    runtime, _, _ = make_runtime()
    subagent = FakeSubagent()
    runtime.subagent = subagent
    await runtime.close()
    assert subagent.closed is True


class FakePresets:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def resolve(self, preset_id: str, revision: int | None = None) -> SimpleNamespace:
        return SimpleNamespace(id=preset_id, mode=self.mode)


def test_agent_mode_reads_the_active_preset() -> None:
    runtime, _, _ = make_runtime()
    runtime.kernel_app.services["presets"] = FakePresets("team")
    runtime.agent_preset = "team"
    assert runtime.agent_mode == "team"


def test_agent_mode_defaults_when_presets_are_missing() -> None:
    runtime, _, _ = make_runtime()
    assert runtime.agent_mode == "default"
