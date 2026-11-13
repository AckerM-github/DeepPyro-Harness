"""Tests for the PtcPlugin configuration merge and opt-in registration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.builtin.ptc import PtcPlugin
from deeppyro.core.errors import ConfigError
from deeppyro.ptc import PtcConfig, PtcRuntime, RunCodeTool


class FakeCtx:
    def __init__(self, config_value: Any = None) -> None:
        self.value = config_value
        self.provided: dict[str, Any] = {}
        self.factories: list[Any] = []

    def get(self, name: str) -> Any:
        if name == "config" and self.value is not None:
            return SimpleNamespace(value=self.value)
        return None

    def provide(self, name: str, value: Any) -> None:
        self.provided[name] = value

    def register_tool(self, factory: Any) -> None:
        self.factories.append(factory)


def test_plugin_identity() -> None:
    assert PtcPlugin.provide == "ptc"
    assert PtcPlugin.plugin_id == "ptc"
    assert list(PtcPlugin.inject) == ["config", "tools"]


def test_disabled_by_default_registers_no_tool() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {})
    plugin.setup(ctx, {})
    assert plugin.config.enabled is False
    assert isinstance(ctx.provided["ptc"], PtcRuntime)
    assert ctx.factories == []


def test_enabled_registers_run_code() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"enabled": True})
    plugin.setup(ctx, {})
    assert plugin.config.enabled is True
    assert len(ctx.factories) == 1
    tools = ctx.factories[0](None, Path("."))
    assert len(tools) == 1
    assert isinstance(tools[0], RunCodeTool)


def test_bundle_row_config_is_used() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"enabled": True, "timeoutMs": 5000})
    assert plugin.config.enabled is True
    assert plugin.config.timeout_ms == 5000


def test_kernel_ptc_table_overrides_bundle_row() -> None:
    ctx = FakeCtx(SimpleNamespace(ptc={"enabled": True, "maxTimeoutMs": 9000}))
    plugin = PtcPlugin(ctx, {"enabled": False, "timeoutMs": 5000})
    assert plugin.config.enabled is True
    assert plugin.config.max_timeout_ms == 9000


def test_kernel_config_mapping_is_merged() -> None:
    ctx = FakeCtx({"ptc": {"enabled": True, "maxOutputBytes": 1234}})
    plugin = PtcPlugin(ctx, {})
    assert plugin.config.enabled is True
    assert plugin.config.max_output_bytes == 1234


def test_unknown_config_key_raises() -> None:
    ctx = FakeCtx()
    with pytest.raises(ConfigError):
        PtcPlugin(ctx, {"enabled": True, "nonsense": 1})


def test_service_uses_shared_config_object() -> None:
    ctx = FakeCtx()
    plugin = PtcPlugin(ctx, {"enabled": True})
    assert isinstance(plugin.service.config, PtcConfig)
    assert plugin.service.config.enabled is True
