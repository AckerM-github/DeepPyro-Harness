"""Acceptance: the built-in agent presets shape tools and prompt per session.

The presets are registered by the core bundle (presets + agent-presets + ptc).
Each one must produce the tool list and prompt composition that the DSH presets
describe, without restarting the process. The native agent host is built from a
real NativeSession; the PTC preset goes through the PTC host adapter.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.native_factory import AgentBuild, PromptFacts
from deeppyro.api import (
    CORDIS_PRESET_ID,
    DEFAULT_PRESET_ID,
    MINIMAL_PRESET_ID,
    PLUGIN_AUTHORING_TOOL_NAMES,
    PTC_PRESET_ID,
    TEAM_PRESET_ID,
)
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.prompt_service import PromptPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import boot
from deeppyro.ptc.native_adapter import build_ptc_host
from deeppyro.session.fold import fold
from deeppyro.session.native_control import NewSession
from deeppyro.session.schema import NativeSession

SHELL_TOOL = "pwsh" if os.name == "nt" else "bash"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep user bundles, manifests and sessions out of the test run."""
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(base_url="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])
        },
        agent={"provider": "local", "model": "m"},
    )


def _facts(tmp_path: Path) -> PromptFacts:
    return PromptFacts(
        cwd=tmp_path,
        model="m",
        permission_preset="workspace-write",
        approval_policy="ask",
    )


def _build(app: Any, session: NativeSession, tmp_path: Path, preset_id: str) -> Any:
    build = AgentBuild(
        provider="local",
        model="m",
        cwd=tmp_path,
        preset=preset_id,
        prompt_facts=_facts(tmp_path),
    )
    if preset_id == PTC_PRESET_ID:
        return build_ptc_host(
            session,
            build,
            prompt=app.require(PromptPlugin),
            presets=app.require(PresetsPlugin),
            llm=app.require(LlmPlugin),
            tools=app.require(ToolsPlugin),
            ptc=app.ctx.get("ptc"),
            default_model="m",
        )
    return app.require(AgentsPlugin).host(session, build)


def _session_for(app: Any, tmp_path: Path, preset_id: str) -> NativeSession:
    return app.require(SessionsPlugin).native_create(NewSession(cwd=tmp_path, preset=preset_id))


def _system_prompt(session: NativeSession) -> str:
    view = fold(session.header, session.events)
    assert view.system is not None
    return view.system.text()


async def test_core_bundle_registers_the_standard_ptc_minimal_and_cordis_presets(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        presets = app.require(PresetsPlugin)
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
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        session = _session_for(app, tmp_path, DEFAULT_PRESET_ID)
        host = _build(app, session, tmp_path, DEFAULT_PRESET_ID)
        try:
            names = {tool.name for tool in host._tools}
            assert {"read", "write", "subagent"} <= names
            assert SHELL_TOOL in names
            assert names.isdisjoint(PLUGIN_AUTHORING_TOOL_NAMES)
            assert "run_code" not in names
            assert all(not name.startswith("team_") for name in names)
            # A native host carries no PTC presentation or narrowed surface.
            assert getattr(host, "presentation", None) is None
            assert getattr(host._loop, "_surface", None) is None

            prompt = _system_prompt(session)
            assert "## Environment" in prompt
            assert "## Operating rules" in prompt
            assert "## Tools" in prompt
            assert "run_code" not in prompt
            assert "Extending DeepPyro with Cordis plugins" not in prompt
        finally:
            await host.aclose()
            session.close()
    finally:
        await app.stop()


async def test_ptc_preset_presents_only_run_code_and_the_programmatic_sdk(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        session = _session_for(app, tmp_path, PTC_PRESET_ID)
        host = _build(app, session, tmp_path, PTC_PRESET_ID)
        try:
            # Every tool stays registered; only run_code is directly callable.
            full_names = {tool.name for tool in host._tools}
            assert "run_code" in full_names
            assert full_names.isdisjoint(PLUGIN_AUTHORING_TOOL_NAMES)
            assert host.presentation == "ptc"
            assert list(host._loop._registry.names()) == ["run_code"]
            assert list(host._loop.surface_registry.names()) == ["run_code"]

            prompt = _system_prompt(session)
            assert "is the only tool you can call directly" in prompt
            assert "## Writing code for run_code" in prompt
            assert "class Tools(Protocol):" in prompt
            # The native tools are reached from inside the program, not by name.
            assert "### read" not in prompt
            assert "### pwsh" not in prompt and "### bash" not in prompt
        finally:
            await host.aclose()
            session.close()
    finally:
        await app.stop()


async def test_minimal_preset_exposes_only_the_platform_shell(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        session = _session_for(app, tmp_path, MINIMAL_PRESET_ID)
        host = _build(app, session, tmp_path, MINIMAL_PRESET_ID)
        try:
            assert [tool.name for tool in host._tools] == [SHELL_TOOL]
            assert getattr(host, "presentation", None) is None
            assert getattr(host._loop, "_surface", None) is None

            prompt = _system_prompt(session)
            assert prompt.startswith("You are a helpful software engineer assistant.")
            assert "## Operating rules" in prompt
            assert "## Tools" in prompt
            # The minimal composition opts out of the environment/project sections.
            assert "## Environment" not in prompt
            assert "## Project instructions" not in prompt
        finally:
            await host.aclose()
            session.close()
    finally:
        await app.stop()


async def test_cordis_preset_adds_plugin_authoring_tools_and_guidance(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        session = _session_for(app, tmp_path, CORDIS_PRESET_ID)
        host = _build(app, session, tmp_path, CORDIS_PRESET_ID)
        try:
            names = {tool.name for tool in host._tools}
            assert set(PLUGIN_AUTHORING_TOOL_NAMES) <= names
            assert getattr(host, "presentation", None) is None

            prompt = _system_prompt(session)
            assert "Extending DeepPyro with Cordis plugins" in prompt
            assert "## Environment" in prompt
        finally:
            await host.aclose()
            session.close()
    finally:
        await app.stop()
