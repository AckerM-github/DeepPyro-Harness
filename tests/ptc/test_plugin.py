"""Tests for the PtcPlugin configuration merge and mode registration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.builtin.ptc import (
    PTC_ONLY_INSTRUCTION,
    PTC_ONLY_ORDER,
    PTC_ONLY_SECTION,
    PTC_SDK_ORDER,
    PTC_SDK_SECTION,
    PtcPlugin,
)
from deeppyro.core.errors import ConfigError
from deeppyro.core.protocols import ToolSchema
from deeppyro.ptc import PtcConfig, PtcRuntime, RunCodeTool


class FakeCtx:
    def __init__(self, config_value: Any = None) -> None:
        self.value = config_value
        self.provided: dict[str, Any] = {}
        self.factories: list[Any] = []
        self.sections: list[tuple[str, int, Any]] = []
        self.logger = SimpleNamespace(warning=lambda message: None)

    def get(self, name: str) -> Any:
        if name == "config" and self.value is not None:
            return SimpleNamespace(value=self.value)
        return None

    def provide(self, name: str, value: Any) -> None:
        self.provided[name] = value

    def register_tool(self, factory: Any) -> None:
        self.factories.append(factory)

    def register_prompt_section(self, name: str, order: int, renderer: Any) -> None:
        self.sections.append((name, order, renderer))


def _tools() -> list[ToolSchema]:
    return [
        ToolSchema(name="read", description="Read.", parameters={"type": "object"}),
        ToolSchema(name="run_code", description="Run.", parameters={"type": "object"}),
        ToolSchema(name="ask_user_question", description="Ask.", parameters={"type": "object"}),
    ]


def test_plugin_identity() -> None:
    assert PtcPlugin.provide == "ptc"
    assert PtcPlugin.plugin_id == "ptc"
    assert list(PtcPlugin.inject) == ["config", "tools", "prompt"]


def test_native_by_default_registers_nothing() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {})
    plugin.setup(ctx, {})
    assert plugin.config.mode == "native"
    assert plugin.config.enabled is False
    assert isinstance(ctx.provided["ptc"], PtcRuntime)
    assert ctx.factories == []
    assert ctx.sections == []


def test_ptc_mode_registers_run_code() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"mode": "ptc"})
    plugin.setup(ctx, {})
    assert plugin.config.mode == "ptc"
    assert len(ctx.factories) == 1
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


async def test_kernel_boot_activates_ptc_after_the_prompt_service(tmp_path: Path) -> None:
    """Regression: the bundle row must mount after prompt, or ptc mode fails to load."""
    from deeppyro.config import Config, ModelConfig, ProviderConfig
    from deeppyro.kernel import boot

    config = Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
        ptc={"mode": "ptc"},
    )
    app = await boot(config)
    try:
        assert app.started["ptc"] == "ACTIVE"
        service = app.service("ptc")
        assert service.mode == "ptc"
        registry = app.service("tools").registry_for(tmp_path)
        assert "run_code" in list(registry.names())
    finally:
        await app.stop()
