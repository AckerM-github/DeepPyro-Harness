"""Native-only TuiRuntime behavior through the real kernel bundle.

The old v4 runtime was deleted: the TUI boots the schema 1 control plane
(SessionsPlugin native APIs, AgentsPlugin.host and NativeAgentFactory). These
tests pin the remaining generic behavior of the runtime as the TUI sees it:
session lifecycle hooks, usage meters, provider/model switching, persistence and
resume restoration - all against real native sessions on a temp session dir.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.api import TEAM_PRESET_ID
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.hooks import SESSION_CREATED, SESSION_DISPOSED, HookService
from deeppyro.tui import runtime as runtime_module
from deeppyro.tui.runtime import TuiRuntime, _resolve_model
from deeppyro.tui.sink import TuiSink
from tests.tui.test_native_runtime import _ask, _ScriptedProvider

PROVIDERS = {
    "local": ProviderConfig(
        base_url="http://127.0.0.1:8000/v1",
        models=[ModelConfig(id="m1"), ModelConfig(id="m2")],
    ),
    "other": ProviderConfig(
        base_url="http://127.0.0.1:8001/v1",
        models=[ModelConfig(id="o1")],
    ),
}


def _multi_config(tmp_path: Path) -> Config:
    return Config(
        providers=PROVIDERS,
        agent={"provider": "local", "model": "m1"},
    )


async def _boot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: _ScriptedProvider | None = None,
    *,
    resume: str | None = None,
    config: Config | None = None,
) -> tuple[TuiRuntime, list[dict[str, Any]]]:
    scripted = provider if provider is not None else _ScriptedProvider()
    monkeypatch.setattr(LlmPlugin, "native_provider", lambda self, name: scripted)
    events: list[dict[str, Any]] = []
    runtime = await TuiRuntime.boot(
        sink=TuiSink(events.append),
        ask=_ask,
        config=config if config is not None else _multi_config(tmp_path),
        resume=resume,
    )
    return runtime, events


@pytest.fixture(autouse=True)
def _isolated_runtime_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))


async def test_session_lifecycle_hooks_fire_for_native_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        hooks = runtime.kernel_app.get(HookService)
        assert hooks is not None
        created: list[dict[str, Any]] = []
        disposed: list[dict[str, Any]] = []
        hooks.on(SESSION_CREATED, lambda payload: created.append(dict(payload)))
        hooks.on(SESSION_DISPOSED, lambda payload: disposed.append(dict(payload)))

        boot_session_id = runtime.session_id
        first = await runtime.new_session()
        scheduled = await runtime.create_schedule_session(SimpleNamespace(cwd=str(tmp_path)), "standard")
        await runtime.close()

        assert [event["sessionId"] for event in created] == [first.session_id, scheduled.session_id]
        # close() also disposes the session that boot created. Created ids are
        # tracked in a set, so disposal order is not guaranteed.
        assert sorted(event["sessionId"] for event in disposed) == sorted(
            [boot_session_id, first.session_id, scheduled.session_id]
        )
    finally:
        await runtime.close()


async def test_close_stops_the_kernel_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    await runtime.new_session()

    await runtime.close()

    assert runtime.kernel_app is None
    assert runtime._closed is True
    await runtime.close()
    assert runtime._closed is True


async def test_usage_and_context_come_from_the_native_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeppyro.core.llm import Finish, StopReason, TextDelta, Usage, UsageEvent

    provider = _ScriptedProvider()
    provider.script = [
        TextDelta("hello"),
        UsageEvent(usage=Usage(input_tokens=11, output_tokens=4, cached_input_tokens=2)),
        Finish(StopReason.STOP),
    ]
    runtime, _events = await _boot(monkeypatch, tmp_path, provider)
    try:
        await runtime.agent.send("hi")
        assert runtime.session_usage() == {"input": 11, "output": 4, "cached": 2}
        assert runtime.context_tokens() > 0
    finally:
        await runtime.close()


async def test_set_model_switches_and_records_a_native_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        assert await runtime.set_model("m2", persist=False) == "m2"
        assert runtime.model == "m2"
        assert runtime.provider_name == "local"
        assert runtime.context_window == runtime_module.DEFAULT_CONTEXT_WINDOW or runtime.context_window > 0
        state = runtime.session.fold().state
        assert state.model.model == "m2"
        assert state.model.provider == "local"
    finally:
        await runtime.close()


async def test_set_model_rejects_an_unknown_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        with pytest.raises(ConfigError, match="unknown model: ghost"):
            await runtime.set_model("ghost", persist=False)
        assert runtime.model == "m1"
    finally:
        await runtime.close()


async def test_set_provider_switches_the_native_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        provider_name, model_id = await runtime.set_provider("other", persist=False)
        assert (provider_name, model_id) == ("other", "o1")
        assert runtime.provider_name == "other"
        assert runtime.model == "o1"
    finally:
        await runtime.close()


async def test_set_provider_and_model_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        with pytest.raises(ConfigError, match="unknown provider"):
            await runtime.set_provider("nope", persist=False)
        with pytest.raises(ConfigError):
            await runtime.set_provider("other", model="m1", persist=False)
        with pytest.raises(ConfigError):
            await runtime.set_model("nope", persist=False)
    finally:
        await runtime.close()


async def test_persist_failure_does_not_break_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:

        def boom(*args: Any, **kwargs: Any) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(runtime_module, "save_agent_selection", boom)
        assert await runtime.set_model("m2", persist=True) == "m2"
        assert runtime.last_persist_error is not None
        assert "disk full" in runtime.last_persist_error
    finally:
        await runtime.close()


async def test_persist_writes_the_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        written: list[tuple[str, str]] = []

        def fake_save(provider: str, model: str, path: Any = None) -> Path:
            written.append((provider, model))
            return Path("config.toml")

        monkeypatch.setattr(runtime_module, "save_agent_selection", fake_save)
        await runtime.set_provider("other", persist=True)
        assert written == [("other", "o1")]
        assert runtime.last_persist_error is None
    finally:
        await runtime.close()


async def test_provider_and_model_introspection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        assert runtime.providers() == ["local", "other"]
        assert runtime.models() == ["m1", "m2"]
        assert runtime.models("other") == ["o1"]
        assert runtime.model_choices() == [("local", "m1"), ("local", "m2"), ("other", "o1")]
        assert runtime.has_api_key("local") is False
        assert runtime.has_api_key("nope") is False
    finally:
        await runtime.close()


def test_resolve_model_does_not_leak_the_configured_model_across_providers() -> None:
    cfg = Config.model_validate(
        {
            "agent": {"provider": "local", "model": "local-model"},
            "providers": {
                "local": {
                    "base_url": "http://127.0.0.1:8000/v1",
                    "models": [{"id": "local-model"}],
                },
                "deepseek": {
                    "base_url": "https://api.deepseek.com/v1",
                    "models": [{"id": "deepseek-chat"}],
                },
            },
        }
    )
    assert _resolve_model(cfg, "local", model=None) == "local-model"
    assert _resolve_model(cfg, "deepseek", model=None) == "deepseek-chat"
    assert _resolve_model(cfg, "deepseek", model="deepseek-chat") == "deepseek-chat"
    with pytest.raises(ConfigError):
        _resolve_model(cfg, "deepseek", model="local-model")


async def test_open_session_restores_the_native_model_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        await runtime.set_model("m2", persist=False)
        session_id = runtime.session_id
    finally:
        await runtime.close()

    resumed, _events = await _boot(monkeypatch, tmp_path, resume=session_id)
    try:
        assert resumed.session_id == session_id
        assert resumed.model == "m2"
        assert resumed.provider_name == "local"
    finally:
        await resumed.close()


async def test_team_mode_records_the_team_preset_on_the_native_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        assert await runtime.set_team_mode(True) == TEAM_PRESET_ID
        state = runtime.session.fold().state
        assert state.preset.preset == TEAM_PRESET_ID
        assert runtime.agent_mode == "team"
    finally:
        await runtime.close()


async def test_refresh_models_survives_a_broken_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def offline(self: LlmPlugin, provider: str, *, refresh: bool = False) -> list[Any]:
        raise TimeoutError("catalog offline")

    monkeypatch.setattr(LlmPlugin, "list_models", offline)
    runtime, _events = await _boot(monkeypatch, tmp_path)
    try:
        await runtime.refresh_models()
        assert runtime.last_catalog_error is not None
        assert "catalog offline" in runtime.last_catalog_error
    finally:
        await runtime.close()
