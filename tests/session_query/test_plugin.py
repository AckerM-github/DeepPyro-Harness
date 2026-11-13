"""The session-query plugin and its configuration surface."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.core.errors import ConfigError
from deeppyro.session import SessionStore
from deeppyro.session_query import INVALID_FILTER, SessionQueryError
from tests.session_query.support import new_session, tmp_store

BUILTIN_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "deeppyro" / "builtin" / "session_query.py"
)
MODULE_NAME = "deeppyro_builtin_session_query_under_test"


class _Reflect:
    def __init__(self) -> None:
        self.provided: dict[str, Any] = {}

    def provide(self, name: str, service: Any, check: Any = None) -> None:
        self.provided[name] = service


class _Ctx:
    """Minimal Cordis context: a registry plus a service lookup."""

    def __init__(self, services: dict[str, Any]) -> None:
        self.reflect = _Reflect()
        self._services = services

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise KeyError(name)
        return self._services[name]


def _load_plugin_module() -> Any:
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(MODULE_NAME, BUILTIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _mount(store: SessionStore, *, row_config: dict[str, Any] | None = None, **global_config: Any) -> Any:
    module = _load_plugin_module()
    services = {
        "sessions": SimpleNamespace(store=store),
        "config": SimpleNamespace(value=SimpleNamespace(**global_config)),
    }
    return module.SessionQueryPlugin(_Ctx(services), row_config or {})


def test_plugin_identity_and_default_window(tmp_path: pathlib.Path) -> None:
    module = _load_plugin_module()
    plugin = _mount(tmp_store(tmp_path))
    assert module.SessionQueryPlugin.provide == "session_query"
    assert module.SessionQueryPlugin.inject == ["sessions"]
    assert plugin.query.read_window_max == module.DEFAULT_READ_WINDOW_MAX == 50


def test_plugin_reads_global_section(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    plugin = _mount(store, session_query={"readWindowMax": 7})
    assert plugin.query.read_window_max == 7
    camel = _mount(store, sessionQuery={"readWindowMax": 9})
    assert camel.query.read_window_max == 9
    row = _mount(store, row_config={"readWindowMax": 11})
    assert row.query.read_window_max == 11


def test_plugin_rejects_invalid_window(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ConfigError):
        _mount(tmp_store(tmp_path), session_query={"readWindowMax": 0})
    module = _load_plugin_module()
    with pytest.raises(ValueError):  # pydantic ValidationError (a ValueError)
        module.SessionQueryConfig.model_validate({"readWindowMax": -1})


def test_plugin_delegates_reads(tmp_path: pathlib.Path) -> None:
    store = tmp_store(tmp_path)
    session = new_session(store, [], session_id="session-plugin")
    plugin = _mount(store)
    assert [summary.id for summary in plugin.list_summaries()] == ["session-plugin"]
    assert plugin.get("session-plugin").session_id == session.session_id
    assert plugin.read_session("session-plugin").header.id == "session-plugin"
    assert plugin.read_surface("session-plugin") == []
    assert plugin.list_events("session-plugin") == []
    with pytest.raises(SessionQueryError) as excinfo:
        plugin.read_event("session-plugin", 99)
    assert excinfo.value.code == "SESSION_QUERY_EVENT_NOT_FOUND"
    with pytest.raises(SessionQueryError) as excinfo:
        plugin.list_events("session-plugin", limit=0)
    assert excinfo.value.code == INVALID_FILTER
    assert plugin.trace_session("session-plugin").ancestors == []


async def test_kernel_mounts_service(tmp_path: pathlib.Path) -> None:
    pytest.importorskip("deeppyro.builtin")
    from deeppyro.config import Config
    from deeppyro.kernel.app import create_app
    from deeppyro.kernel.composition import Composition, PluginRow

    config = Config.model_validate(
        {
            "agent": {"provider": "local", "model": "m"},
            "providers": {
                "local": {"api": "openai", "baseURL": "http://127.0.0.1:8000/v1", "models": [{"id": "m"}]}
            },
            "sessionDir": str(tmp_path / "sessions"),
        }
    )
    rows = (
        PluginRow(id="sessions", plugin="deeppyro.builtin.sessions:SessionsPlugin"),
        PluginRow(
            id="session-query",
            plugin="deeppyro.builtin.session_query:SessionQueryPlugin",
            config={"readWindowMax": 5},
        ),
    )
    app = await create_app(config, composition=Composition(rows=rows, origin="test"))
    try:
        service = app.service("session_query")
        assert service.query.read_window_max == 5
        assert service.list_summaries() == []
    finally:
        await app.stop()
