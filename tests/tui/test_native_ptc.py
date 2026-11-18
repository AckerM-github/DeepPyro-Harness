"""PTC presets on the native TUI runtime: the run_code surface, not the v4 agent."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from deeppyro.agent.native_factory import AgentBuild
from deeppyro.api.errors import PluginDependencyError
from deeppyro.core.llm import Finish, StopReason, TextDelta
from deeppyro.ptc.native_adapter import build_ptc_host
from deeppyro.tui.runtime import TuiRuntime
from tests.tui.test_native_runtime import (
    _boot,
    _noop_title,
    _ScriptedProvider,
    _TurnHost,
)

PTC_ONLY_TEXT = "`run_code` is the only tool you can call directly"


async def test_native_ptc_preset_builds_the_run_code_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider()
    provider.script = [TextDelta("hi from ptc"), Finish(StopReason.STOP)]
    runtime, events = await _boot(monkeypatch, tmp_path, provider, native=True)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        assert runtime.agent_presentation == "native"
        selected = await runtime.set_agent_preset("ptc")
        assert selected == "ptc"
        assert runtime.agent_preset == "ptc"
        assert runtime.agent_presentation == "ptc"
        agent = runtime.agent
        assert agent.presentation == "ptc"
        # The loop surface only admits run_code; the full registry stays in the
        # tool context so the nested dispatcher and the SDK prompt still see it.
        assert list(agent._loop._registry.names()) == ["run_code"]
        assert list(agent._loop._surface.names()) == ["run_code"]
        full = agent._loop._services["tool_registry"]
        assert "run_code" in full.names()
        assert len(full.names()) > 1
        system = runtime.session.fold().system
        assert system is not None and PTC_ONLY_TEXT in system.text()
        events.clear()
        layer = _TurnHost(runtime)
        await layer._run_native_turn("hello")
        finals = [event for event in events if event.get("type") == "final"]
        assert finals and finals[-1]["text"] == "hi from ptc"

        # Switching back restores the ordinary native surface.
        await runtime.set_agent_preset("default")
        assert runtime.agent_presentation == "native"
        assert not getattr(runtime.agent, "presentation", "")
        assert "run_code" not in runtime.agent._loop._registry.names()
    finally:
        await runtime.close()


def _fake_build() -> AgentBuild:
    return AgentBuild(provider="local", model="m", cwd=Path("."), preset="ptc")


def test_build_ptc_host_refuses_without_presets_or_the_ptc_runtime() -> None:
    with pytest.raises(PluginDependencyError, match="agent presets are not available"):
        build_ptc_host(None, _fake_build(), prompt=None, presets=None, llm=None)

    spec = SimpleNamespace(id="ptc", revision=None, mode="default", presentation="ptc")
    presets = SimpleNamespace(resolve=lambda preset_id=None, revision=None: spec)
    with pytest.raises(PluginDependencyError, match="needs the ptc runtime") as raised:
        build_ptc_host(None, _fake_build(), prompt=None, presets=presets, llm=None)
    assert "which this bundle does not provide" in str(raised.value)
