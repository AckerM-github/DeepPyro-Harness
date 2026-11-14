"""Acceptance: the four built-in agent presets shape tools, prompt and context.

The presets are registered by the core bundle (presets + agent-presets + ptc).
Each one must produce the tool list, prompt composition and runtime-context
switch that the DSH presets describe, without restarting the process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from deeppyro.api import (
    CORDIS_PRESET_ID,
    DEFAULT_PRESET_ID,
    MINIMAL_PRESET_ID,
    PLUGIN_AUTHORING_TOOL_NAMES,
    PTC_PRESET_ID,
    TEAM_PRESET_ID,
)
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import boot
from deeppyro.prompt import PromptContext

SHELL_TOOL = "pwsh" if os.name == "nt" else "bash"


class DummySink:
    def emit(self, event: dict[str, Any]) -> None:
        pass


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={"local": ProviderConfig(baseURL="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])},
        agent={"provider": "local", "model": "m"},
        sessionDir=tmp_path / "sessions",
    )


def _prompt_context(tmp_path: Path) -> PromptContext:
    return PromptContext(
        cwd=tmp_path,
        platform="test",
        session_id="session-preset",
        started_at=0,
        permission_preset="workspace-write",
        approval_policy="ask",
        model="m",
    )


def _agent_for(app: Any, tmp_path: Path, preset_id: str) -> Any:
    sessions = app.service("sessions")
    agents = app.service("agents")
    session = sessions.create(str(tmp_path), agent_preset=preset_id)
    return agents.create(
        session=session,
        provider=object(),
        options=agents.options(),
        sink=DummySink(),
        cwd=str(tmp_path),
        preset=preset_id,
    )


async def test_core_bundle_registers_the_standard_ptc_minimal_and_cordis_presets(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path))
    try:
        presets = app.service("presets")
        assert presets.ids() == [
            DEFAULT_PRESET_ID,
            PTC_PRESET_ID,
            MINIMAL_PRESET_ID,
            CORDIS_PRESET_ID,
            TEAM_PRESET_ID,
        ]
        visible = [spec.id for spec in presets.visible()]
        assert visible == [DEFAULT_PRESET_ID, PTC_PRESET_ID, MINIMAL_PRESET_ID, CORDIS_PRESET_ID]
        assert TEAM_PRESET_ID not in visible

        standard = presets.resolve(DEFAULT_PRESET_ID)
        assert standard.name == "Standard"
        assert standard.presentation == "native"
        assert standard.tools is None
        assert standard.tools_exclude == PLUGIN_AUTHORING_TOOL_NAMES
        # The legacy persisted id keeps resolving to the standard preset.
        assert presets.resolve("default").id == DEFAULT_PRESET_ID
        assert presets.canonical("default") == DEFAULT_PRESET_ID

        assert presets.resolve(PTC_PRESET_ID).presentation == "ptc"
        minimal = presets.resolve(MINIMAL_PRESET_ID)
        assert minimal.tools == (SHELL_TOOL,)
        assert minimal.prompt_sections == ("minimal-persona", "tools", "rules")
        assert minimal.runtime_context is False
        cordis = presets.resolve(CORDIS_PRESET_ID)
        assert cordis.tools_exclude == ()
        assert cordis.runtime_context is True
    finally:
        await app.stop()


async def test_standard_preset_hides_plugin_authoring_and_keeps_run_code_out(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path))
    try:
        agent = _agent_for(app, tmp_path, DEFAULT_PRESET_ID)
        names = set(agent._registry.names())
        assert {"read", "write", "pwsh", "subagent"} <= names or {
            "read",
            "write",
            "bash",
            "subagent",
        } <= names
        assert names.isdisjoint(PLUGIN_AUTHORING_TOOL_NAMES)
        assert "run_code" not in names
        assert all(not name.startswith("team_") for name in names)
        assert agent.presentation == "native"
        assert agent.surface_registry is None
        assert agent._runtime_context is True

        prompt = agent._prompt_builder([], _prompt_context(tmp_path))
        assert "## Environment" in prompt
        assert "## Operating rules" in prompt
        assert "## Tools" in prompt
        assert "run_code" not in prompt
        assert "Extending DeepPyro with Cordis plugins" not in prompt
    finally:
        await app.stop()


async def test_ptc_preset_presents_only_run_code_and_the_programmatic_sdk(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path))
    try:
        agent = _agent_for(app, tmp_path, PTC_PRESET_ID)
        assert "run_code" in agent._registry.names()
        assert set(agent._registry.names()).isdisjoint(PLUGIN_AUTHORING_TOOL_NAMES)
        assert agent.presentation == "ptc"
        assert list(agent.surface_registry.names()) == ["run_code"]

        prompt = agent._prompt_builder([], _prompt_context(tmp_path))
        assert "is the only tool you can call directly" in prompt
        assert "## Writing code for run_code" in prompt
        assert "class Tools(Protocol):" in prompt
        # The native tools are reached from inside the program, not by name.
        assert "### read" not in prompt
        assert "### pwsh" not in prompt and "### bash" not in prompt
    finally:
        await app.stop()


async def test_minimal_preset_exposes_only_the_platform_shell(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path))
    try:
        agent = _agent_for(app, tmp_path, MINIMAL_PRESET_ID)
        assert agent._registry.names() == [SHELL_TOOL]
        assert agent.presentation == "native"
        assert agent.surface_registry is None
        assert agent._runtime_context is False

        prompt = agent._prompt_builder([], _prompt_context(tmp_path))
        assert prompt.startswith("You are a helpful software engineer assistant.")
        assert "## Operating rules" in prompt
        assert "## Tools" in prompt
        # The minimal composition opts out of the environment/project sections.
        assert "## Environment" not in prompt
        assert "## Project instructions" not in prompt
    finally:
        await app.stop()


async def test_cordis_preset_adds_plugin_authoring_tools_and_guidance(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path))
    try:
        agent = _agent_for(app, tmp_path, CORDIS_PRESET_ID)
        names = set(agent._registry.names())
        assert set(PLUGIN_AUTHORING_TOOL_NAMES) <= names
        assert agent.presentation == "native"
        assert agent._runtime_context is True

        prompt = agent._prompt_builder([], _prompt_context(tmp_path))
        assert "Extending DeepPyro with Cordis plugins" in prompt
        assert "## Environment" in prompt
    finally:
        await app.stop()
