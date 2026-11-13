"""Kernel plugin that exposes the Python PTC runtime and, when enabled, run_code.

The plugin is intentionally inert by default: the "ptc" service is always
provided so a host can inspect configuration, but the run_code tool is only
registered when [ptc].enabled is true.  This keeps the model-facing tool list
unchanged unless the feature is explicitly turned on.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.ptc import PtcConfig, PtcRuntime, RunCodeTool


def _ptc_config(ctx: Any, row_config: Any) -> dict[str, Any]:
    """Merge the bundle row config under the [ptc] table from the kernel config."""
    merged: dict[str, Any] = {}
    if isinstance(row_config, Mapping):
        merged.update(row_config)
    getter = getattr(ctx, "get", None)
    config_service = getter("config") if callable(getter) else None
    value = getattr(config_service, "value", None) if config_service is not None else None
    raw: Any = None
    if value is not None:
        raw = getattr(value, "ptc", None)
        if raw is None and isinstance(value, Mapping):
            raw = value.get("ptc")
    if isinstance(raw, Mapping):
        merged.update(raw)
    else:
        model_dump = getattr(raw, "model_dump", None)
        if callable(model_dump):
            merged.update(model_dump())
    return merged


class PtcPlugin(Plugin):
    """Provides the "ptc" service and conditionally registers run_code."""

    provide = "ptc"
    plugin_id = "ptc"
    version = "0.1.0"
    description = "Python programmatic tool calling (run_code, opt-in)"
    inject: ClassVar[list[str]] = ["config", "tools"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.config = PtcConfig.from_mapping(_ptc_config(ctx, config))
        self.service = PtcRuntime(self.config)

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("ptc", self.service)
        if self.config.enabled:
            ctx.register_tool(lambda _cfg, _workspace: [RunCodeTool()])


__all__ = ["PtcPlugin"]
