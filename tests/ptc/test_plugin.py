"""Tests for the PtcPlugin configuration merge and mode registration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.api import PTC_PRESET_ID
from deeppyro.builtin.ptc import (
    PTC_ONLY_INSTRUCTION,
    PTC_ONLY_ORDER,
    PTC_ONLY_SECTION,
    PTC_SDK_ORDER,
    PTC_SDK_SECTION,
    PtcPlugin,
)
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.errors import ConfigError
from deeppyro.core.protocols import ToolSchema
from deeppyro.kernel import boot
from deeppyro.ptc import PtcConfig, PtcRuntime, RunCodeTool


class FakeCtx:
    def __init__(self, config_value: Any = None) -> None:
        self.value = config_value
        self.provided: dict[str, Any] = {}
        self.factories: list[Any] = []
        self.tool_scopes: list[str | None] = []
        self.sections: list[tuple[str, int, Any]] = []
        self.section_scopes: list[str | None] = []
        self.logger = SimpleNamespace(warning=lambda message: None)

    def get(self, name: str) -> Any:
        if name == "config" and self.value is not None:
            return SimpleNamespace(value=self.value)
        return None

    def provide(self, name: str, value: Any) -> None:
        self.provided[name] = value

    def register_tool(self, factory: Any, *, scope: str | None = None) -> None:
        self.factories.append(factory)
        self.tool_scopes.append(scope)

    def register_prompt_section(
        self,
        name: str,
        order: int,
        renderer: Any,
        *,
        scope: str | None = None,
    ) -> None:
        self.sections.append((name, order, renderer))
        self.section_scopes.append(scope)


def _tools() -> list[ToolSchema]:
    return [
        ToolSchema(name="read", description="Read.", parameters={"type": "object"}),
        ToolSchema(name="run_code", description="Run.", parameters={"type": "object"}),
        ToolSchema(name="ask_user_question", description="Ask.", parameters={"type": "object"}),
    ]


def _config(tmp_path: Path, **extra: Any) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
        sessionDir=tmp_path / "sessions",
        **extra,
    )


def _ptc_config(tmp_path: Path) -> Config:
    return _config(tmp_path, ptc={"mode": "ptc"})


def test_plugin_identity() -> None:
    assert PtcPlugin.provide == "ptc"
    assert PtcPlugin.plugin_id == "ptc"
    assert list(PtcPlugin.inject) == ["config", "tools", "prompt"]


def test_native_mode_registers_ptc_preset_scope() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {})
    plugin.setup(ctx, {})
    assert plugin.config.mode == "native"
    assert plugin.config.enabled is False
    assert isinstance(ctx.provided["ptc"], PtcRuntime)
    assert len(ctx.factories) == 1
    assert ctx.tool_scopes == [PTC_PRESET_ID]
    tools = ctx.factories[0](None, Path("."))
    assert len(tools) == 1
    assert isinstance(tools[0], RunCodeTool)
    assert [(name, order) for name, order, _ in ctx.sections] == [
        (PTC_ONLY_SECTION, PTC_ONLY_ORDER),
        (PTC_SDK_SECTION, PTC_SDK_ORDER),
    ]
    assert ctx.section_scopes == [PTC_PRESET_ID, PTC_PRESET_ID]
    renderers = {name: renderer for name, _, renderer in ctx.sections}
    assert renderers[PTC_ONLY_SECTION](_tools(), None) == PTC_ONLY_INSTRUCTION
    sdk = renderers[PTC_SDK_SECTION](_tools(), None)
    assert "class Tools(Protocol)" in sdk
    assert "async def read" in sdk
    assert "async def run_code" not in sdk
    assert "ask_user_question" not in sdk


def test_ptc_mode_registers_run_code() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"mode": "ptc"})
    plugin.setup(ctx, {})
    assert plugin.config.mode == "ptc"
    assert len(ctx.factories) == 1
    assert ctx.tool_scopes == [None]
    tools = ctx.factories[0](None, Path("."))
    assert len(tools) == 1
    assert isinstance(tools[0], RunCodeTool)


def test_legacy_enabled_true_selects_ptc_mode() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"enabled": True})
    plugin.setup(ctx, {})
    assert plugin.config.mode == "ptc"
    assert plugin.config.enabled is True
    assert len(ctx.factories) == 1
    assert ctx.tool_scopes == [None]


def test_explicit_mode_beats_legacy_enabled() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"enabled": True, "mode": "native"})
    assert plugin.config.mode == "native"
    assert plugin.config.enabled is False


def test_ptc_mode_registers_prompt_sections() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"mode": "ptc"})
    plugin.setup(ctx, {})
    assert [(name, order) for name, order, _ in ctx.sections] == [
        (PTC_ONLY_SECTION, PTC_ONLY_ORDER),
        (PTC_SDK_SECTION, PTC_SDK_ORDER),
    ]
    assert ctx.section_scopes == [None, None]
    renderers = {name: renderer for name, _, renderer in ctx.sections}
    assert renderers[PTC_ONLY_SECTION](_tools(), None) == PTC_ONLY_INSTRUCTION
    sdk = renderers[PTC_SDK_SECTION](_tools(), None)
    assert "class Tools(Protocol)" in sdk
    assert "async def read" in sdk
    assert "async def run_code" not in sdk
    assert "ask_user_question" not in sdk


def test_bundle_row_config_is_used() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"mode": "ptc", "timeoutMs": 5000})
    assert plugin.config.mode == "ptc"
    assert plugin.config.timeout_ms == 5000


def test_kernel_ptc_table_overrides_bundle_row() -> None:
    ctx = FakeCtx(SimpleNamespace(ptc={"mode": "ptc", "maxTimeoutMs": 9000}))
    plugin = PtcPlugin(ctx, {"mode": "native", "timeoutMs": 5000})
    assert plugin.config.mode == "ptc"
    assert plugin.config.max_timeout_ms == 9000


def test_kernel_config_mapping_is_merged() -> None:
    ctx = FakeCtx({"ptc": {"mode": "ptc", "maxOutputBytes": 1234}})
    plugin = PtcPlugin(ctx, {})
    assert plugin.config.mode == "ptc"
    assert plugin.config.max_output_bytes == 1234


def test_unknown_config_key_raises() -> None:
    ctx = FakeCtx()
    with pytest.raises(ConfigError):
        PtcPlugin(ctx, {"mode": "ptc", "nonsense": 1})


def test_invalid_mode_raises() -> None:
    ctx = FakeCtx()
    with pytest.raises(ConfigError):
        PtcPlugin(ctx, {"mode": "turbo"})


def test_service_uses_shared_config_object() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"mode": "ptc"})
    assert isinstance(plugin.service.config, PtcConfig)
    assert plugin.service.config.mode == "ptc"


async def test_kernel_native_mode_scopes_ptc_contributions_to_the_preset(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path), manifest_paths=[])
    try:
        assert app.started["ptc"] == "ACTIVE"
        tools = app.service("tools")
        assert "run_code" in tools.registry_for(tmp_path, scope=PTC_PRESET_ID).names()
        assert "run_code" not in tools.registry_for(tmp_path).names()

        prompt = app.service("prompt")
        scoped_names = {section.name for section in prompt.sections(scope=PTC_PRESET_ID)}
        assert {PTC_ONLY_SECTION, PTC_SDK_SECTION} <= scoped_names
        global_names = {section.name for section in prompt.sections()}
        assert PTC_ONLY_SECTION not in global_names
        assert PTC_SDK_SECTION not in global_names
    finally:
        await app.stop()


async def test_kernel_boot_activates_ptc_after_the_prompt_service(tmp_path: Path) -> None:
    """Regression: the bundle row must mount after prompt, or ptc mode fails to load."""
    app = await boot(_ptc_config(tmp_path), manifest_paths=[])
    try:
        assert app.started["ptc"] == "ACTIVE"
        service = app.service("ptc")
        assert service.mode == "ptc"
        tools = app.service("tools")
        registry = tools.registry_for(tmp_path)
        assert "run_code" in list(registry.names())
        assert "run_code" in tools.registry_for(tmp_path, scope=PTC_PRESET_ID).names()

        prompt = app.service("prompt")
        global_names = [section.name for section in prompt.sections()]
        scoped_names = [section.name for section in prompt.sections(scope=PTC_PRESET_ID)]
        assert [name for name in global_names if name in (PTC_ONLY_SECTION, PTC_SDK_SECTION)] == [
            PTC_ONLY_SECTION,
            PTC_SDK_SECTION,
        ]
        assert scoped_names.count(PTC_ONLY_SECTION) == 1
        assert scoped_names.count(PTC_SDK_SECTION) == 1
    finally:
        await app.stop()
