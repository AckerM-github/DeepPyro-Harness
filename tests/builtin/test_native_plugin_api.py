"""Tests for the native sessions/agents plugin API (the TUI-facing calls)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.native_factory import AgentBuild, PromptFacts
from deeppyro.agent.native_host import NativeAgentHost
from deeppyro.api import DEFAULT_PRESET_ID
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.ids import new_session_id
from deeppyro.kernel import boot
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.fold import fold
from deeppyro.session.native_control import NewSession
from deeppyro.session.native_query import NativeQueryError


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep user bundles and manifests out of the test run."""
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
        session_dir=tmp_path / "sessions",
    )


def _facts(tmp_path: Path) -> PromptFacts:
    return PromptFacts(
        cwd=tmp_path,
        model="m",
        permission_preset="workspace-write",
        approval_policy="ask",
    )


async def test_sessions_plugin_native_control_api(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        sessions = app.require(SessionsPlugin)
        assert sessions.native.root is not None
        session = sessions.native_create(NewSession(cwd=tmp_path))
        session_id = session.header.id
        try:
            assert sessions.native_exists(session_id) is True
            assert sessions.native_read(session_id).header.id == session_id
            assert sessions.native_look(session_id).id == session_id
            assert sessions.native_transcript(session_id).header.id == session_id
            with pytest.raises(SessionLockedError):
                sessions.native_open(session_id)
        finally:
            session.close()

        assert sessions.native_rename(session_id, "  Native  control  ") == "Native control"
        assert sessions.native_look(session_id).title == "Native control"
        assert [info.id for info in sessions.native_list()] == [session_id]
        assert sessions.native_latest() is not None
        assert sessions.native_latest().id == session_id
        assert sessions.native_archive(session_id) is True
        assert sessions.native_list() == []
        assert [info.id for info in sessions.native_list(include_archived=True)] == [session_id]
        assert sessions.native_archive(session_id, archived=False) is False

        fork = sessions.native_fork(session_id)
        try:
            assert fork.header.parent_id == session_id
            assert fork.header.origin == "fork"
        finally:
            fork.close()

        with pytest.raises(NativeQueryError):
            sessions.native_open(new_session_id())

        # The v4 surface is untouched and still callable on the same plugin.
        assert callable(sessions.create)
        assert callable(sessions.open)
        assert callable(sessions.list)
        assert callable(sessions.latest)
        assert sessions.store is not None
    finally:
        await app.stop()


async def test_agents_plugin_builds_a_native_host(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        agents = app.require(AgentsPlugin)
        sessions = app.require(SessionsPlugin)
        session = sessions.native_create(NewSession(cwd=tmp_path))
        build = AgentBuild(
            provider="local",
            model="",
            cwd=tmp_path,
            preset=DEFAULT_PRESET_ID,
            prompt_facts=_facts(tmp_path),
        )
        host = agents.host(session, build)
        try:
            assert isinstance(host, NativeAgentHost)
            assert host.session_id == session.header.id
            assert agents.is_live(host) is True
            view = fold(session.header, session.events)
            assert view.system is not None
            assert view.system.text().strip()
            names = {tool.name for tool in host._tools}
            assert names
            assert "plugin_manager" not in names
            assert host._loop._model == "m"
            assert host.turn_count() == 0
        finally:
            await host.aclose()
        assert agents.is_live(host) is False
    finally:
        await app.stop()


class _SentinelHost:
    """Factory double result: enough surface for _register/is_live."""

    closed = False
    session_id = "sentinel"

    def __init__(self) -> None:
        self.marker = "sentinel"


class _SentinelFactory:
    """NativeAgentFactory double: records the injected build."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def build(self, session: Any, build: Any) -> _SentinelHost:
        self.calls.append((session, build))
        return _SentinelHost()


async def test_agents_host_factory_can_be_injected(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        agents = app.require(AgentsPlugin)
        sessions = app.require(SessionsPlugin)
        session = sessions.native_create(NewSession(cwd=tmp_path))
        try:
            factory = _SentinelFactory()
            build = AgentBuild(provider="local", model="m", cwd=tmp_path, preset=DEFAULT_PRESET_ID)
            host = agents.host(session, build, factory=factory)  # type: ignore[arg-type]
            assert isinstance(host, _SentinelHost)
            assert factory.calls == [(session, build)]
            assert agents.is_live(host) is True
            assert agents.live_agents() == (host,)
        finally:
            session.close()
    finally:
        await app.stop()
