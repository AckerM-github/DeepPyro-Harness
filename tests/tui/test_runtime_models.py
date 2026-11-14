"""Provider/model switching on the TUI runtime: validation, records, resume."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from textual.widgets import OptionList

from deeppyro.config import Config
from deeppyro.core.errors import ConfigError
from deeppyro.llm import ModelInfo
from deeppyro.tui import runtime as runtime_module
from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.runtime import TuiRuntime, _recorded_model_selection, _resolve_model
from deeppyro.tui.screens.picker import PickerScreen
from deeppyro.tui.widgets.prompt_area import PromptArea
from tests.tui.test_app import FakeRuntime


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
    def __init__(
        self,
        *,
        catalog: dict[str, list[ModelInfo]] | None = None,
        snapshot: dict[str, list[ModelInfo]] | None = None,
        list_error: Exception | None = None,
        info_error: Exception | None = None,
    ) -> None:
        self.catalog = catalog or {}
        self.snapshot = snapshot or {}
        self.list_error = list_error
        self.info_error = info_error
        self.list_calls: list[tuple[str, bool]] = []
        self.info_calls: list[tuple[str, str]] = []

    def provider(self, name: str) -> str:
        return f"provider:{name}"

    def catalog_snapshot(self, provider: str) -> list[ModelInfo]:
        return list(self.snapshot.get(provider, []))

    async def list_models(self, provider: str, *, refresh: bool = False) -> list[ModelInfo]:
        self.list_calls.append((provider, refresh))
        if self.list_error is not None:
            raise self.list_error
        return list(self.catalog.get(provider, []))

    async def model_info(self, provider: str, model_id: str) -> ModelInfo | None:
        self.info_calls.append((provider, model_id))
        if self.info_error is not None:
            raise self.info_error
        for info in self.catalog.get(provider, []):
            if info.id == model_id:
                return info
        return None


class FakeKernel:
    def __init__(self, llm: FakeLlm | None = None) -> None:
        self.services = {"llm": llm or FakeLlm()}

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


def make_runtime(
    *,
    session: FakeSession | None = None,
    llm: FakeLlm | None = None,
) -> tuple[TuiRuntime, FakeAgents, FakeStore]:
    store = FakeStore(session)
    agents = FakeAgents()
    runtime = TuiRuntime(
        cfg=make_config(),
        cwd=Path("D:/work"),
        kernel_app=FakeKernel(llm),
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
    assert configure["preset"] == "standard"
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
    assert configure["preset"] == "standard"
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


# -- model catalog (P1 A3) ----------------------------------------------


def discovered(model_id: str, window: int | None = None, provider: str = "local") -> ModelInfo:
    return ModelInfo(provider=provider, id=model_id, context_window=window)


async def test_model_choices_merges_discovered_catalog_after_config() -> None:
    llm = FakeLlm(
        catalog={
            "local": [
                discovered("local-model", 32768),
                discovered("discovered-big", 200000),
                discovered("local-small", 8192),
            ]
        }
    )
    runtime, _, _ = make_runtime(llm=llm)
    assert runtime.model_choices() == [
        ("local", "local-model"),
        ("local", "local-small"),
        ("deepseek", "deepseek-chat"),
    ]
    assert await runtime.refresh_models() == 3
    assert llm.list_calls == [("local", False), ("deepseek", False), ("keyless", False)]
    assert runtime.model_choices() == [
        ("local", "local-model"),
        ("local", "local-small"),
        ("deepseek", "deepseek-chat"),
        ("local", "discovered-big"),
    ]
    assert runtime.last_catalog_error is None


async def test_refresh_models_seeds_the_snapshot_and_survives_failure() -> None:
    llm = FakeLlm(
        snapshot={"local": [discovered("offline-known", 1000)]},
        list_error=TimeoutError("catalog offline"),
    )
    runtime, _, _ = make_runtime(llm=llm)
    assert await runtime.refresh_models() == 1
    assert runtime.last_catalog_error is not None
    assert "catalog offline" in runtime.last_catalog_error
    choices = runtime.model_choices()
    assert choices[:3] == [
        ("local", "local-model"),
        ("local", "local-small"),
        ("deepseek", "deepseek-chat"),
    ]
    assert ("local", "offline-known") in choices


async def test_refresh_models_is_a_noop_without_an_llm_service() -> None:
    runtime, _, _ = make_runtime()
    runtime.kernel_app.services.pop("llm")
    assert await runtime.refresh_models() == 0
    assert runtime.last_catalog_error is None
    assert runtime.model_choices() == [
        ("local", "local-model"),
        ("local", "local-small"),
        ("deepseek", "deepseek-chat"),
    ]


async def test_set_model_accepts_a_cached_discovered_model() -> None:
    llm = FakeLlm(catalog={"local": [discovered("discovered-big", 200000)]})
    runtime, agents, _ = make_runtime(llm=llm)
    await runtime.refresh_models(["local"])
    assert await runtime.set_model("discovered-big", persist=False) == "discovered-big"
    assert llm.info_calls == []
    assert runtime.model == "discovered-big"
    assert runtime.context_window == 200000
    assert agents.created[-1]["options"] == {"model": "discovered-big", "context_window": 200000}
    assert runtime.session.records[-1].data == {"provider": "local", "model": "discovered-big"}


async def test_set_model_queries_model_info_for_an_uncached_discovered_model() -> None:
    llm = FakeLlm(catalog={"local": [discovered("discovered-tiny", 64000)]})
    runtime, agents, _ = make_runtime(llm=llm)
    assert await runtime.set_model("discovered-tiny", persist=False) == "discovered-tiny"
    assert llm.info_calls == [("local", "discovered-tiny")]
    assert runtime.context_window == 64000
    assert runtime._cached_model("local", "discovered-tiny") is not None
    assert agents.created[-1]["options"]["context_window"] == 64000


async def test_discovered_model_without_a_window_uses_the_default() -> None:
    llm = FakeLlm(catalog={"local": [discovered("no-window")]})
    runtime, _, _ = make_runtime(llm=llm)
    assert await runtime.set_model("no-window", persist=False) == "no-window"
    assert runtime.context_window == runtime_module.DEFAULT_CONTEXT_WINDOW


async def test_set_model_rejects_a_model_the_catalog_does_not_list() -> None:
    llm = FakeLlm(catalog={"local": []})
    runtime, _, _ = make_runtime(llm=llm)
    with pytest.raises(ConfigError, match="unknown model: ghost"):
        await runtime.set_model("ghost", persist=False)
    assert runtime.model == "local-model"
    assert runtime.context_window == 32768


async def test_set_model_records_catalog_failures_and_rejects_the_model() -> None:
    llm = FakeLlm(info_error=TimeoutError("lookup timed out"))
    runtime, _, _ = make_runtime(llm=llm)
    with pytest.raises(ConfigError, match="unknown model: phantom"):
        await runtime.set_model("phantom", persist=False)
    assert runtime.last_catalog_error is not None
    assert "lookup timed out" in runtime.last_catalog_error


async def test_set_provider_accepts_a_cached_discovered_model() -> None:
    llm = FakeLlm(catalog={"local": [discovered("discovered-pro", 48000)]})
    runtime, agents, _ = make_runtime(llm=llm)
    await runtime.refresh_models(["local"])
    provider_name, model_id = await runtime.set_provider("local", model="discovered-pro", persist=False)
    assert (provider_name, model_id) == ("local", "discovered-pro")
    assert runtime.context_window == 48000
    assert agents.created[-1]["options"]["context_window"] == 48000


async def test_open_session_restores_a_discovered_model_from_the_cached_catalog() -> None:
    session = FakeSession()
    session.records.append(FakeRecord("model/selection", {"provider": "local", "model": "discovered-resume"}))
    llm = FakeLlm(catalog={"local": [discovered("discovered-resume", 96000)]})
    runtime, agents, _ = make_runtime(session=session, llm=llm)
    await runtime.refresh_models(["local"])
    await runtime.open_session("session-discovered")
    assert runtime.model == "discovered-resume"
    assert runtime.context_window == 96000
    assert agents.created[-1]["options"]["context_window"] == 96000


class CatalogRuntime(FakeRuntime):
    """FakeRuntime whose /model refresh exposes one discovered model."""

    def __init__(self, app: DeepPyroApp) -> None:
        super().__init__(app)
        self.refresh_calls = 0

    async def refresh_models(self) -> int:
        self.refresh_calls += 1
        return 1

    def model_choices(self) -> list[tuple[str, str]]:
        return [*super().model_choices(), ("fake", "fake-discovery")]


async def test_model_command_refreshes_the_catalog_before_the_picker() -> None:
    holder: dict[str, CatalogRuntime] = {}

    async def factory(app: DeepPyroApp) -> CatalogRuntime:
        runtime = CatalogRuntime(app)
        holder["runtime"] = runtime
        return runtime

    app = DeepPyroApp(runtime_factory=factory, splash=False)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/model"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, PickerScreen):
                break
        assert isinstance(app.screen, PickerScreen)
        assert holder["runtime"].refresh_calls == 1
        option_list = app.screen.query_one("#picker-list", OptionList)
        labels = [getattr(option.prompt, "plain", str(option.prompt)) for option in option_list.options]
        assert "fake/fake-discovery" in labels
        await pilot.press("escape")
        await pilot.pause(0.2)
