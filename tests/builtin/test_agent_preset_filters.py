"""Preset filtering, tool presentation and prompt allowlists (task-71).

Three layers are covered here:

* AgentsPlugin._filter_registry applies the preset allowlist and denylist on top
  of the agent-mode partition, for explicit registries as well as the ones the
  tools service builds.
* AgentsPlugin._surface_registry picks the model-facing registry from the preset
  presentation while keeping the legacy host-wide [ptc].mode switch working.
* PromptPlugin.build(include=...) narrows the rendered sections and never falls
  back to the core prompt when an allowlist matched nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.api import (
    AGENT_MODE_TEAM,
    AGENT_PRESENTATION_NATIVE,
    AGENT_PRESENTATION_PTC,
    PLUGIN_AUTHORING_TOOL_NAMES,
    TEAM_PRESET_ID,
    PluginDependencyError,
    PresetSpec,
    PromptSection,
)
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.prompt_service import PromptPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.protocols import TextDelta, ToolResult
from deeppyro.kernel import boot
from deeppyro.prompt import PromptContext, build_system_prompt
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.registry import RESERVED_TOOL_NAMES, ToolRegistry
from tests.agent.fakes import (
    FakeRegistry,
    FakeSessionLog,
    FakeTool,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)


# -- helpers ---------------------------------------------------------------
class _NamedTool(BaseTool):
    """A minimal real tool so the tests exercise ToolRegistry.without."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = name + " tool"
        self.parameters = make_parameters({})
        # run_code is reserved for the PTC transport; a stand-in must claim it.
        self.reserved_tool = name in RESERVED_TOOL_NAMES

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult(text=self.name)


def _registry(*names: str) -> ToolRegistry:
    return ToolRegistry([_NamedTool(name) for name in names])


class _FakePtc:
    def __init__(self, mode: str = "native") -> None:
        self.mode = mode


class _PluginCtx:
    """Only the get() seam the agents service uses for optional services."""

    def __init__(self, **services: Any) -> None:
        self._services = services

    def get(self, name: str) -> Any | None:
        return self._services.get(name)


def _agents_plugin(*, ptc: Any = None) -> AgentsPlugin:
    plugin = object.__new__(AgentsPlugin)
    plugin._ctx = _PluginCtx() if ptc is None else _PluginCtx(ptc=ptc)
    return plugin


def _prompt_plugin(
    names: tuple[str, ...] = ("identity", "environment", "tools", "rules"),
    *,
    scope: str | None = None,
) -> PromptPlugin:
    def renderer(name: str) -> Callable[[Any, Any], str]:
        def render(tools: list[Any], ctx: Any) -> str:
            return "## " + name

        return render

    plugin = object.__new__(PromptPlugin)
    plugin._sections = [
        PromptSection(
            name=name,
            order=index * 10,
            renderer=renderer(name),
            plugin_id="test",
            scope=scope,
        )
        for index, name in enumerate(names, start=1)
    ]
    return plugin


def _context(tmp_path: Path) -> PromptContext:
    return PromptContext(
        cwd=tmp_path,
        platform="win32",
        session_id="session-preset-filters",
        started_at=1_700_000_000_000,
        permission_preset="workspace-write",
        approval_policy="ask",
        model="test-model",
    )


# -- registry filtering ----------------------------------------------------
def test_allowlist_keeps_only_listed_tools_in_registry_order() -> None:
    plugin = _agents_plugin()
    registry = _registry("echo", "read", "pwsh")

    filtered = plugin._filter_registry(registry, PresetSpec(id="minimal", tools=("pwsh", "read")))

    # Allowlist order does not reorder the registry.
    assert filtered.names() == ["read", "pwsh"]


def test_allowlist_with_unknown_names_is_harmless() -> None:
    plugin = _agents_plugin()

    filtered = plugin._filter_registry(
        _registry("echo", "read"), PresetSpec(id="minimal", tools=("echo", "missing"))
    )

    assert filtered.names() == ["echo"]


def test_tools_exclude_is_applied_after_the_allowlist() -> None:
    plugin = _agents_plugin()
    registry = _registry("pwsh", "plugin_manager", "echo")
    spec = PresetSpec(
        id="minimal",
        tools=("pwsh", "plugin_manager"),
        tools_exclude=PLUGIN_AUTHORING_TOOL_NAMES,
    )

    assert plugin._filter_registry(registry, spec).names() == ["pwsh"]


def test_default_spec_hides_only_plugin_authoring_tools() -> None:
    plugin = _agents_plugin()
    registry = _registry("echo", "plugin_manager", "settings_write")

    filtered = plugin._filter_registry(
        registry, PresetSpec(id="standard", tools_exclude=PLUGIN_AUTHORING_TOOL_NAMES)
    )

    assert filtered.names() == ["echo"]


def test_agent_mode_partition_stacks_with_the_allowlist() -> None:
    plugin = _agents_plugin()
    registry = _registry("subagent", "team_spawn", "echo")

    team = plugin._filter_registry(registry, PresetSpec(id="team", mode=AGENT_MODE_TEAM))
    assert team.names() == ["team_spawn", "echo"]

    narrowed = plugin._filter_registry(
        registry,
        PresetSpec(id="team", mode=AGENT_MODE_TEAM, tools=("team_spawn", "subagent")),
    )
    assert narrowed.names() == ["team_spawn"]


def test_filtering_a_caller_supplied_registry_keeps_it_untouched() -> None:
    plugin = _agents_plugin()
    registry = _registry("echo", "plugin_inspect")

    filtered = plugin._filter_registry(
        registry, PresetSpec(id="standard", tools_exclude=PLUGIN_AUTHORING_TOOL_NAMES)
    )

    assert filtered is not registry
    assert filtered.names() == ["echo"]
    assert registry.names() == ["echo", "plugin_inspect"]


def test_none_registry_stays_none() -> None:
    assert _agents_plugin()._filter_registry(None, PresetSpec(id="standard")) is None


# -- tool presentation -----------------------------------------------------
def test_explicit_ptc_presentation_presents_run_code_only() -> None:
    plugin = _agents_plugin()
    registry = _registry("echo", "run_code", "read")
    spec = PresetSpec(id="ptc", presentation=AGENT_PRESENTATION_PTC)

    surface = plugin._surface_registry(registry, spec)

    assert surface is not None
    assert surface.names() == ["run_code"]


def test_explicit_ptc_presentation_without_run_code_is_a_dependency_error() -> None:
    plugin = _agents_plugin()
    spec = PresetSpec(id="ptc", presentation=AGENT_PRESENTATION_PTC)

    with pytest.raises(PluginDependencyError, match="run_code"):
        plugin._surface_registry(_registry("echo"), spec)


def test_native_presentation_keeps_every_tool() -> None:
    plugin = _agents_plugin(ptc=_FakePtc("native"))
    registry = _registry("echo", "run_code")

    assert plugin._surface_registry(registry, PresetSpec(id="standard")) is None


def test_legacy_ptc_mode_still_narrows_default_presets() -> None:
    plugin = _agents_plugin(ptc=_FakePtc("ptc"))

    surface = plugin._surface_registry(_registry("echo", "run_code"), PresetSpec(id="standard"))

    assert surface is not None
    assert surface.names() == ["run_code"]


def test_legacy_ptc_mode_without_run_code_stays_native() -> None:
    plugin = _agents_plugin(ptc=_FakePtc("ptc"))

    assert plugin._surface_registry(_registry("echo"), PresetSpec(id="standard")) is None


def test_team_preset_keeps_the_native_surface() -> None:
    plugin = _agents_plugin(ptc=_FakePtc("ptc"))
    spec = PresetSpec(
        id=TEAM_PRESET_ID,
        mode=AGENT_MODE_TEAM,
        presentation=AGENT_PRESENTATION_PTC,
    )

    assert plugin._surface_registry(_registry("team_spawn", "run_code"), spec) is None


def test_missing_registry_or_ptc_service_is_native() -> None:
    plugin = _agents_plugin()

    assert plugin._surface_registry(None, PresetSpec(id="standard")) is None
    assert plugin._surface_registry(_registry("run_code"), PresetSpec(id="standard")) is None


# -- prompt allowlists -----------------------------------------------------
def test_prompt_allowlist_renders_only_the_listed_sections(tmp_path: Path) -> None:
    plugin = _prompt_plugin()

    rendered = plugin.build([], _context(tmp_path), include=("minimal-persona", "tools", "rules"))

    assert rendered == "## tools\n\n## rules"


def test_prompt_allowlist_that_matches_nothing_is_a_dependency_error(tmp_path: Path) -> None:
    plugin = _prompt_plugin()

    with pytest.raises(PluginDependencyError, match="matched no sections"):
        plugin.build([], _context(tmp_path), include=("minimal-persona",))


def test_prompt_allowlist_is_scoped_to_the_composition(tmp_path: Path) -> None:
    plugin = object.__new__(PromptPlugin)
    plugin._sections = [
        PromptSection(name="identity", order=10, renderer=lambda tools, ctx: "## identity"),
        PromptSection(
            name="minimal-persona",
            order=20,
            renderer=lambda tools, ctx: "## persona",
            scope="minimal",
        ),
    ]
    context = _context(tmp_path)

    assert plugin.build([], context, scope="minimal", include=("minimal-persona",)) == "## persona"
    with pytest.raises(PluginDependencyError, match="matched no sections"):
        plugin.build([], context, scope="other", include=("minimal-persona",))


def test_include_none_keeps_the_previous_rendering(tmp_path: Path) -> None:
    plugin = _prompt_plugin(("identity", "tools"))
    context = _context(tmp_path)

    assert plugin.build([], context) == "## identity\n\n## tools"
    assert plugin.build([], context, include=None) == plugin.build([], context)


def test_include_none_still_falls_back_to_the_core_prompt(tmp_path: Path) -> None:
    plugin = object.__new__(PromptPlugin)
    plugin._sections = []
    context = _context(tmp_path)

    assert plugin.build([], context) == build_system_prompt([], context)


# -- wiring through the agents service -------------------------------------
class _DummySink:
    def emit(self, event: dict[str, Any]) -> None:
        pass


class _RecordingPrompt:
    """Prompt-service stand-in that records the composition request."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build(
        self,
        tools: list[Any],
        prompt_context: Any,
        *,
        scope: str | None = None,
        revision: int | None = None,
        include: Any = None,
    ) -> str:
        self.calls.append({"scope": scope, "revision": revision, "include": include})
        return "SYSTEM"


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
        sessionDir=tmp_path / "sessions",
    )


async def test_create_wires_filters_presentation_and_runtime_context(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path))
    try:
        presets = app.service("presets")
        presets.add(PresetSpec(id="ptc-lite", name="PTC Lite", presentation=AGENT_PRESENTATION_PTC))
        presets.add(
            PresetSpec(
                id="minimal",
                name="Minimal",
                tools=("echo", "run_code"),
                tools_exclude=PLUGIN_AUTHORING_TOOL_NAMES,
                runtime_context=False,
            )
        )
        agents = app.service("agents")
        registry = _registry("echo", "run_code", "plugin_manager", "subagent")
        session = app.service("sessions").create(str(tmp_path))

        standard = agents.create(
            session=session,
            provider=object(),
            registry=registry,
            options=agents.options(),
            sink=_DummySink(),
            cwd=str(tmp_path),
        )
        assert standard.presentation == AGENT_PRESENTATION_NATIVE
        assert standard.surface_registry is None
        assert standard._runtime_context is True
        assert standard._registry.names() == ["echo", "run_code", "subagent"]

        ptc = agents.create(
            session=session,
            provider=object(),
            registry=registry,
            options=agents.options(),
            sink=_DummySink(),
            cwd=str(tmp_path),
            preset="ptc-lite",
        )
        assert ptc.preset == "ptc-lite"
        assert ptc.presentation == AGENT_PRESENTATION_PTC
        assert ptc.surface_registry is not None
        assert ptc.surface_registry.names() == ["run_code"]
        # The full registry still backs dispatch and the prompt.
        assert ptc._registry.names() == ["echo", "run_code", "plugin_manager", "subagent"]

        minimal = agents.create(
            session=session,
            provider=object(),
            registry=registry,
            options=agents.options(),
            sink=_DummySink(),
            cwd=str(tmp_path),
            preset="minimal",
        )
        assert minimal.presentation == AGENT_PRESENTATION_NATIVE
        assert minimal._runtime_context is False
        assert minimal._registry.names() == ["echo", "run_code"]
    finally:
        await app.stop()


async def test_build_prompt_forwards_the_preset_prompt_allowlist(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path))
    try:
        app.service("presets").add(
            PresetSpec(id="minimal", name="Minimal", prompt_sections=("identity", "tools"))
        )
        recorder = _RecordingPrompt()
        plugin = object.__new__(AgentsPlugin)
        plugin._ctx = _PluginCtx()
        plugin._presets = app.service("presets")
        plugin._prompt_service = recorder
        plugin._tools = None
        plugin._agent_config = app.service("config").value.agent
        session = app.service("sessions").create(str(tmp_path))
        provider = ScriptedProvider([ScriptedTurn(events=[TextDelta("hi"), finish("stop")])])

        agent = plugin.create(
            session=session,
            provider=provider,
            registry=_registry("echo"),
            options=plugin.options(),
            sink=_DummySink(),
            cwd=str(tmp_path),
            preset="minimal",
        )
        assert await agent.send("go") == "hi"

        assert recorder.calls, "the system prompt must be rendered on the first step"
        assert recorder.calls[0]["scope"] == "minimal"
        assert recorder.calls[0]["include"] == ("identity", "tools")
    finally:
        await app.stop()


async def test_runtime_context_flag_gates_the_projection(tmp_path: Path) -> None:
    async def runtime_records(runtime_context: bool) -> list[Any]:
        session = FakeSessionLog(cwd=tmp_path)
        provider = ScriptedProvider([ScriptedTurn(events=[TextDelta("hi"), finish("stop")])])
        agent = AgentSession(
            session=session,
            provider=provider,
            registry=FakeRegistry([FakeTool("echo")]),
            options=AgentOptions(model="test-model"),
            sink=RecordingSink(session),
            cwd=tmp_path,
            runtime_context=runtime_context,
        )
        assert await agent.send("go") == "hi"
        return [
            record
            for record in session.records
            if record.type == "user/message"
            and record.data.get("source", {}).get("kind") == "runtime-context"
        ]

    assert await runtime_records(False) == []
    assert len(await runtime_records(True)) == 1
