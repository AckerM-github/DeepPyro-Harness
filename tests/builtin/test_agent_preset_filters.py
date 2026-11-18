"""Preset filtering, tool presentation and prompt allowlists (task-71).

Three layers are covered here:

* deeppyro.agent.native_factory._filter_registry applies the preset allowlist
  and denylist on top of the agent-mode partition for whatever registry the
  tools service builds.
* deeppyro.agent.native_factory._refuse_ptc and
  deeppyro.ptc.native_adapter._ptc_surface express the PTC presentation rule on
  the native runtime: the factory refuses a PTC preset (the model-facing
  run_code-only surface is installed by the PTC adapter instead).
* PromptPlugin.build(include=...) narrows the rendered sections and never falls
  back to the core prompt when an allowlist matched nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.native_factory import _filter_registry, _refuse_ptc
from deeppyro.api import (
    AGENT_MODE_TEAM,
    AGENT_PRESENTATION_PTC,
    PLUGIN_AUTHORING_TOOL_NAMES,
    PluginDependencyError,
    PresetSpec,
    PromptSection,
)
from deeppyro.builtin.prompt_service import PromptPlugin
from deeppyro.core.protocols import ToolResult
from deeppyro.prompt import PromptContext, build_system_prompt
from deeppyro.ptc.native_adapter import _ptc_surface
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.registry import RESERVED_TOOL_NAMES, ToolRegistry


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
    registry = _registry("echo", "read", "pwsh")

    filtered = _filter_registry(registry, PresetSpec(id="minimal", tools=("pwsh", "read")))

    # Allowlist order does not reorder the registry.
    assert filtered.names() == ["read", "pwsh"]


def test_allowlist_with_unknown_names_is_harmless() -> None:
    filtered = _filter_registry(
        _registry("echo", "read"), PresetSpec(id="minimal", tools=("echo", "missing"))
    )

    assert filtered.names() == ["echo"]


def test_tools_exclude_is_applied_after_the_allowlist() -> None:
    registry = _registry("pwsh", "plugin_manager", "echo")
    spec = PresetSpec(
        id="minimal",
        tools=("pwsh", "plugin_manager"),
        tools_exclude=PLUGIN_AUTHORING_TOOL_NAMES,
    )

    assert _filter_registry(registry, spec).names() == ["pwsh"]


def test_default_spec_hides_only_plugin_authoring_tools() -> None:
    registry = _registry("echo", "plugin_manager", "settings_write")

    filtered = _filter_registry(
        registry, PresetSpec(id="standard", tools_exclude=PLUGIN_AUTHORING_TOOL_NAMES)
    )

    assert filtered.names() == ["echo"]


def test_agent_mode_partition_stacks_with_the_allowlist() -> None:
    registry = _registry("subagent", "team_spawn", "echo")

    team = _filter_registry(registry, PresetSpec(id="team", mode=AGENT_MODE_TEAM))
    assert team.names() == ["team_spawn", "echo"]

    narrowed = _filter_registry(
        registry,
        PresetSpec(id="team", mode=AGENT_MODE_TEAM, tools=("team_spawn", "subagent")),
    )
    assert narrowed.names() == ["team_spawn"]


def test_filtering_a_caller_supplied_registry_keeps_it_untouched() -> None:
    registry = _registry("echo", "plugin_inspect")

    filtered = _filter_registry(
        registry, PresetSpec(id="standard", tools_exclude=PLUGIN_AUTHORING_TOOL_NAMES)
    )

    assert filtered is not registry
    assert filtered.names() == ["echo"]
    assert registry.names() == ["echo", "plugin_inspect"]


def test_none_registry_becomes_an_empty_surface() -> None:
    assert _filter_registry(None, PresetSpec(id="standard")) == ()


def test_registry_without_without_is_returned_unchanged() -> None:
    spec = PresetSpec(id="standard", tools=())
    registry = object()

    assert _filter_registry(registry, spec) is registry


# -- tool presentation -----------------------------------------------------
def test_explicit_ptc_presentation_is_refused_by_the_native_factory() -> None:
    spec = PresetSpec(id="ptc", presentation=AGENT_PRESENTATION_PTC)

    with pytest.raises(PluginDependencyError, match="PTC runtime"):
        _refuse_ptc(spec, None)


def test_legacy_ptc_mode_is_refused_by_the_native_factory() -> None:
    spec = PresetSpec(id="standard")

    with pytest.raises(PluginDependencyError, match="PTC runtime"):
        _refuse_ptc(spec, lambda: True)

    # Native mode is accepted whether or not the ptc service is mounted.
    assert _refuse_ptc(spec, lambda: False) is None
    assert _refuse_ptc(spec, None) is None


def test_ptc_surface_presents_run_code_only() -> None:
    surface = _ptc_surface(_registry("echo", "run_code", "read"))

    assert surface is not None
    assert surface.names() == ["run_code"]


def test_ptc_surface_without_run_code_is_none() -> None:
    assert _ptc_surface(_registry("echo")) is None


def test_ptc_surface_ignores_missing_or_opaque_registries() -> None:
    assert _ptc_surface(None) is None
    assert _ptc_surface(object()) is None


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
