"""Tests for the built-in PTC, Minimal and Cordis agent presets."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any

import pytest

from deeppyro.api import (
    AGENT_MODE_DEFAULT,
    AGENT_PRESENTATION_NATIVE,
    AGENT_PRESENTATION_PTC,
    CORDIS_PRESET_ID,
    MINIMAL_PRESET_ID,
    PLUGIN_AUTHORING_TOOL_NAMES,
    PTC_PRESET_ID,
    TEAM_PRESET_ID,
)
from deeppyro.builtin.agent_presets import (
    CORDIS_GUIDE,
    CORDIS_GUIDE_ORDER,
    CORDIS_GUIDE_SECTION,
    MINIMAL_PERSONA,
    MINIMAL_PERSONA_ORDER,
    MINIMAL_PERSONA_SECTION,
    AgentPresetsPlugin,
    builtin_presets,
)
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import boot
from deeppyro.prompt import PromptContext


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep user bundles and manifests out of the test run."""
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))


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


def _prompt_context(tmp_path: Path) -> PromptContext:
    return PromptContext(
        cwd=tmp_path,
        platform="test",
        session_id="session-test",
        started_at=0,
        permission_preset="workspace-write",
        approval_policy="ask",
        model="m",
    )


def _section_names(prompt: Any, scope: str) -> list[str]:
    return [section.name for section in prompt.sections(scope=scope)]


def test_builtin_presets_are_declared_in_order() -> None:
    presets = builtin_presets()
    assert [preset.id for preset in presets] == [PTC_PRESET_ID, MINIMAL_PRESET_ID, CORDIS_PRESET_ID]
    assert [preset.name for preset in presets] == ["PTC", "Minimal", "Cordis"]
    assert [preset.order for preset in presets] == [10, 20, 30]
    assert all(preset.mode == AGENT_MODE_DEFAULT for preset in presets)


async def test_agent_presets_plugin_registers_the_dsh_preset_set(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        assert app.started["agent-presets"] == "ACTIVE"
        presets = app.service("presets")
        assert {"standard", PTC_PRESET_ID, MINIMAL_PRESET_ID, CORDIS_PRESET_ID, TEAM_PRESET_ID} <= set(
            presets.ids()
        )
        visible_ids = [spec.id for spec in presets.visible()]
        assert visible_ids[:4] == ["standard", PTC_PRESET_ID, MINIMAL_PRESET_ID, CORDIS_PRESET_ID]
        assert TEAM_PRESET_ID not in visible_ids
    finally:
        await app.stop()


async def test_ptc_preset_fields(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        preset = app.service("presets").resolve(PTC_PRESET_ID)
        assert preset.id == PTC_PRESET_ID
        assert preset.name == "PTC"
        assert preset.description == (
            "standard tools through one run_code program (programmatic tool calling)"
        )
        assert preset.order == 10
        assert preset.mode == AGENT_MODE_DEFAULT
        assert preset.presentation == AGENT_PRESENTATION_PTC
        assert preset.tools is None
        assert preset.tools_exclude == PLUGIN_AUTHORING_TOOL_NAMES
        assert preset.prompt_sections is None
        assert preset.runtime_context is True
        assert preset.hidden is False
    finally:
        await app.stop()


async def test_minimal_preset_fields(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        preset = app.service("presets").resolve(MINIMAL_PRESET_ID)
        expected_shell = "pwsh" if os.name == "nt" else "bash"
        assert preset.id == MINIMAL_PRESET_ID
        assert preset.name == "Minimal"
        assert preset.description == "a single platform shell and a minimal prompt"
        assert preset.order == 20
        assert preset.mode == AGENT_MODE_DEFAULT
        assert preset.presentation == AGENT_PRESENTATION_NATIVE
        assert preset.tools == (expected_shell,)
        assert preset.tools_exclude == ()
        assert preset.prompt_sections == (MINIMAL_PERSONA_SECTION, "tools", "rules")
        assert preset.runtime_context is False
    finally:
        await app.stop()


async def test_cordis_preset_fields(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        preset = app.service("presets").resolve(CORDIS_PRESET_ID)
        assert preset.id == CORDIS_PRESET_ID
        assert preset.name == "Cordis"
        assert preset.description == "standard tools plus plugin authoring and cordis guidance"
        assert preset.order == 30
        assert preset.mode == AGENT_MODE_DEFAULT
        assert preset.presentation == AGENT_PRESENTATION_NATIVE
        assert preset.tools is None
        assert preset.tools_exclude == ()
        assert preset.runtime_context is True
    finally:
        await app.stop()


async def test_minimal_persona_section_is_scoped_to_the_minimal_preset(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        prompt = app.service("prompt")
        sections = prompt.sections(scope=MINIMAL_PRESET_ID)
        persona = next(section for section in sections if section.name == MINIMAL_PERSONA_SECTION)
        assert persona.order == MINIMAL_PERSONA_ORDER
        assert persona.renderer([], _prompt_context(tmp_path)) == MINIMAL_PERSONA
        assert MINIMAL_PERSONA_SECTION not in _section_names(prompt, CORDIS_PRESET_ID)

        text = prompt.build(
            [],
            _prompt_context(tmp_path),
            scope=MINIMAL_PRESET_ID,
            include=(MINIMAL_PERSONA_SECTION, "tools", "rules"),
        )
        assert MINIMAL_PERSONA in text
        assert "## Operating rules" in text
    finally:
        await app.stop()


async def test_cordis_guide_section_is_scoped_to_the_cordis_preset(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        prompt = app.service("prompt")
        sections = prompt.sections(scope=CORDIS_PRESET_ID)
        guide = next(section for section in sections if section.name == CORDIS_GUIDE_SECTION)
        assert guide.order == CORDIS_GUIDE_ORDER
        assert guide.renderer([], _prompt_context(tmp_path)) == CORDIS_GUIDE
        assert len(CORDIS_GUIDE.splitlines()) <= 25
        assert "bundle.toml" in CORDIS_GUIDE
        assert "plugin_manager" in CORDIS_GUIDE
        assert "plugin_inspect" in CORDIS_GUIDE
        assert CORDIS_GUIDE_SECTION not in _section_names(prompt, MINIMAL_PRESET_ID)
    finally:
        await app.stop()


async def test_plugin_dispose_removes_presets_and_scoped_sections(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        presets = app.service("presets")
        prompt = app.service("prompt")
        assert PTC_PRESET_ID in presets.ids()
        assert MINIMAL_PERSONA_SECTION in _section_names(prompt, MINIMAL_PRESET_ID)

        record = app.mount_record(AgentPresetsPlugin.plugin_id)
        assert record is not None
        assert record.fiber is not None
        result = record.fiber.dispose()
        if inspect.isawaitable(result):
            await result

        assert {"standard", TEAM_PRESET_ID} <= set(presets.ids())
        assert PTC_PRESET_ID not in presets.ids()
        assert MINIMAL_PRESET_ID not in presets.ids()
        assert CORDIS_PRESET_ID not in presets.ids()
        assert MINIMAL_PERSONA_SECTION not in _section_names(prompt, MINIMAL_PRESET_ID)
        assert CORDIS_GUIDE_SECTION not in _section_names(prompt, CORDIS_PRESET_ID)
    finally:
        await app.stop()
