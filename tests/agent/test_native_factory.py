"""Tests for the native agent factory build recipe."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.native_factory import (
    AgentBuild,
    NativeAgentFactory,
    PresetPromptSource,
    PromptFacts,
    build_native_host,
)
from deeppyro.agent.turn_messages import tool_specs
from deeppyro.api import PluginDependencyError
from deeppyro.api.types import (
    AGENT_MODE_TEAM,
    AGENT_PRESENTATION_PTC,
    PresetPluginRow,
    PresetSpec,
)
from deeppyro.core.ids import new_session_id
from deeppyro.core.protocols import ToolResult
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeSession
from deeppyro.tools.registry import ToolRegistry


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name
        self.parameters: dict[str, Any] = {"type": "object"}
        self.parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult(text="ok")


class _FakeProvider:
    name = "fake"

    async def stream(self, request: Any):
        if False:  # pragma: no cover - never streamed in factory tests
            yield request

    async def aclose(self) -> None:
        return None


class _FakeLlm:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def native_provider(self, name: str) -> _FakeProvider:
        self.requested.append(name)
        return _FakeProvider()


class _FakePresets:
    def __init__(self, spec: PresetSpec) -> None:
        self.spec = spec
        self.requests: list[tuple[str | None, int | None]] = []
        self.materialized = True

    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> PresetSpec:
        self.requests.append((preset_id, revision))
        return self.spec

    def is_materialized(self, preset_id: str, revision: int | None = None) -> bool:
        return self.materialized


class _FakeTools:
    def __init__(self, names: list[str]) -> None:
        self.registry = ToolRegistry([_FakeTool(name) for name in names])
        self.calls: list[tuple[str, str | None, int | None]] = []

    def registry_for(
        self, workspace: str | Path, *, scope: str | None = None, revision: int | None = None
    ) -> ToolRegistry:
        self.calls.append((str(workspace), scope, revision))
        return self.registry


class _FakePrompt:
    def __init__(self, text: str = "SYSTEM PROMPT") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def build(
        self,
        tools: list[Any],
        prompt_context: Any,
        *,
        scope: str | None = None,
        revision: int | None = None,
        include: tuple[str, ...] | None = None,
    ) -> str:
        self.calls.append(
            {
                "tools": [tool.name for tool in tools],
                "context": prompt_context,
                "scope": scope,
                "revision": revision,
                "include": include,
            }
        )
        return self.text


def _session(tmp_path: Path) -> NativeSession:
    path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    return NativeSession.create(path, cwd=tmp_path, session_id=path.stem)


def _facts(tmp_path: Path) -> PromptFacts:
    return PromptFacts(
        cwd=tmp_path,
        model="m",
        permission_preset="workspace-write",
        approval_policy="ask",
    )


def _spec(**overrides: Any) -> PresetSpec:
    base: dict[str, Any] = {"id": "standard", "revision": 0, "model": "", "prompt_sections": None}
    base.update(overrides)
    return PresetSpec(**base)


def _build(tmp_path: Path, **overrides: Any) -> AgentBuild:
    base: dict[str, Any] = {
        "provider": "fake",
        "model": "",
        "cwd": tmp_path,
        "preset": "standard",
    }
    base.update(overrides)
    return AgentBuild(**base)


def _system_events(session: NativeSession) -> list[Any]:
    return [event for event in session.events if event.type == EventKind.SYSTEM_MESSAGE.value]


async def test_builds_host_with_prompt_and_scoped_tools(tmp_path: Path) -> None:
    session = _session(tmp_path)
    prompt = _FakePrompt()
    llm = _FakeLlm()
    tools = _FakeTools(["read", "write", "team_spawn", "subagent", "plugin_manager"])
    presets = _FakePresets(_spec(tools=("read", "write"), tools_exclude=("write",)))
    host = build_native_host(
        session,
        _build(tmp_path, prompt_facts=_facts(tmp_path)),
        prompt=prompt,
        presets=presets,
        llm=llm,
        tools=tools,
        default_model="default-model",
    )
    try:
        assert presets.requests == [("standard", None)]
        assert llm.requested == ["fake"]
        assert tools.calls == [(str(tmp_path), "standard", 0)]
        assert [tool.name for tool in host._tools] == ["read"]
        assert host._loop._model == "default-model"
        view = fold(session.header, session.events)
        assert view.system is not None
        assert view.system.text() == "SYSTEM PROMPT"
        assert len(_system_events(session)) == 1
        call = prompt.calls[0]
        assert call["scope"] == "standard"
        assert call["revision"] == 0
        assert call["tools"] == ["read"]
        assert call["context"].model == "m"
        assert call["context"].cwd == tmp_path
        assert call["context"].direct_tool_names == ("read",)
    finally:
        await host.aclose()


async def test_team_mode_keeps_team_tools_and_drops_subagents(tmp_path: Path) -> None:
    session = _session(tmp_path)
    tools = _FakeTools(["team_spawn", "subagent", "read"])
    presets = _FakePresets(_spec(id="team", mode=AGENT_MODE_TEAM))
    host = build_native_host(
        session,
        _build(tmp_path, preset="team"),
        prompt=_FakePrompt(),
        presets=presets,
        llm=_FakeLlm(),
        tools=tools,
        default_model="m",
    )
    try:
        assert [tool.name for tool in host._tools] == ["team_spawn", "read"]
    finally:
        await host.aclose()


async def test_prompt_refresh_is_last_wins_and_short_circuits(tmp_path: Path) -> None:
    session = _session(tmp_path)
    prompt = _FakePrompt("V1")
    host = build_native_host(
        session,
        _build(tmp_path, prompt_facts=_facts(tmp_path)),
        prompt=prompt,
        presets=_FakePresets(_spec()),
        llm=_FakeLlm(),
        tools=None,
        default_model="m",
    )
    try:
        assert len(_system_events(session)) == 1
        assert host.refresh_prompt() is False
        assert len(_system_events(session)) == 1
        prompt.text = "V2"
        assert host.refresh_prompt() is True
        assert len(_system_events(session)) == 2
        view = fold(session.header, session.events)
        assert view.system is not None
        assert view.system.text() == "V2"
        prompt.text = ""
        assert host.refresh_prompt() is False
        assert len(_system_events(session)) == 2
    finally:
        await host.aclose()


async def test_no_prompt_facts_means_no_system_message(tmp_path: Path) -> None:
    session = _session(tmp_path)
    host = build_native_host(
        session,
        _build(tmp_path),
        prompt=_FakePrompt(),
        presets=_FakePresets(_spec()),
        llm=_FakeLlm(),
        tools=None,
        default_model="m",
    )
    try:
        assert _system_events(session) == []
        assert fold(session.header, session.events).system is None
        assert host.refresh_prompt() is False
    finally:
        await host.aclose()


async def test_ptc_presentation_is_refused_honestly(tmp_path: Path) -> None:
    session = _session(tmp_path)
    presets = _FakePresets(_spec(id="ptc", presentation=AGENT_PRESENTATION_PTC))
    with pytest.raises(PluginDependencyError, match="requires the PTC runtime"):
        build_native_host(
            session,
            _build(tmp_path, preset="ptc"),
            prompt=_FakePrompt(),
            presets=presets,
            llm=_FakeLlm(),
            tools=None,
            default_model="m",
        )
    session.close()

    session = _session(tmp_path)
    try:
        with pytest.raises(PluginDependencyError, match="requires the PTC runtime"):
            build_native_host(
                session,
                _build(tmp_path),
                prompt=_FakePrompt(),
                presets=_FakePresets(_spec()),
                llm=_FakeLlm(),
                tools=None,
                default_model="m",
                ptc_mode=lambda: True,
            )
    finally:
        session.close()


async def test_unmaterialized_preset_is_refused(tmp_path: Path) -> None:
    session = _session(tmp_path)
    spec = _spec(plugins=(PresetPluginRow(id="demo", plugin="demo"),))
    presets = _FakePresets(spec)
    presets.materialized = False
    with pytest.raises(PluginDependencyError, match="not materialized yet"):
        build_native_host(
            session,
            _build(tmp_path),
            prompt=_FakePrompt(),
            presets=presets,
            llm=_FakeLlm(),
            tools=None,
            default_model="m",
        )
    session.close()


async def test_missing_model_is_refused(tmp_path: Path) -> None:
    session = _session(tmp_path)
    with pytest.raises(PluginDependencyError, match="no model configured"):
        build_native_host(
            session,
            _build(tmp_path),
            prompt=_FakePrompt(),
            presets=_FakePresets(_spec(model="")),
            llm=_FakeLlm(),
            tools=None,
        )
    session.close()


async def test_factory_object_can_be_injected(tmp_path: Path) -> None:
    session = _session(tmp_path)
    factory = NativeAgentFactory(
        prompt=_FakePrompt("FACTORY"),
        presets=_FakePresets(_spec()),
        llm=_FakeLlm(),
        tools=None,
        default_model="m",
    )
    host = factory.build(session, _build(tmp_path, prompt_facts=_facts(tmp_path)))
    try:
        assert host.session_id == session.header.id
        view = fold(session.header, session.events)
        assert view.system is not None
        assert view.system.text() == "FACTORY"
    finally:
        await host.aclose()


async def test_closed_session_is_refused(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.close()
    with pytest.raises(ValueError, match="session is closed"):
        build_native_host(
            session,
            _build(tmp_path),
            prompt=_FakePrompt(),
            presets=_FakePresets(_spec()),
            llm=_FakeLlm(),
            tools=None,
            default_model="m",
        )


async def test_preset_prompt_source_builds_context(tmp_path: Path) -> None:
    session = _session(tmp_path)
    spec = _spec(instructions=("stay calm",), prompt_sections=("identity",))
    prompt = _FakePrompt("rendered")
    source = PresetPromptSource(prompt=prompt, spec=spec, facts=lambda: _facts(tmp_path))
    tools = [_FakeTool("read")]
    context = source.context(session.header, tool_specs(tools))
    assert context.session_id == session.header.id
    assert context.permission_preset == "workspace-write"
    assert context.approval_policy == "ask"
    assert context.instructions[-1] == "stay calm"
    assert context.direct_tool_names == ("read",)
    text = source.render(session.header, tool_specs(tools))
    assert text == "rendered"
    assert prompt.calls[-1]["include"] == ("identity",)
    assert prompt.calls[-1]["revision"] == 0
    session.close()


def test_agent_build_validates_its_fields(tmp_path: Path) -> None:
    build = AgentBuild(provider="fake", model="m", cwd=str(tmp_path), preset="standard")
    assert isinstance(build.cwd, Path)
    with pytest.raises(ValueError):
        AgentBuild(provider="", model="m", cwd=tmp_path, preset="standard")
    with pytest.raises(ValueError):
        AgentBuild(provider="fake", model="m", cwd=tmp_path, preset="standard", context_window=0)
    with pytest.raises(ValueError):
        AgentBuild(provider="fake", model="m", cwd=tmp_path, preset="standard", max_steps=0)
    facts = _facts(tmp_path)
    with pytest.raises(ValueError):
        PromptFacts(cwd=tmp_path, model="", permission_preset="", approval_policy="", env=(("a", 1),))  # type: ignore[arg-type]
    assert facts.approval_policy == "ask"
