"""Kernel plugin that exposes the Python PTC runtime and the run_code mode.

The "ptc" service is always provided so a host can inspect configuration, but
the run_code tool and the PTC prompt sections are only registered when
[ptc].mode is "ptc".  This keeps the model-facing tool list and prompt unchanged
unless programmatic tool calling is explicitly selected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.core.protocols import ToolSchema
from deeppyro.ptc import NESTED_EXCLUDED_TOOLS, RUN_CODE_NAME, PtcConfig, PtcRuntime, RunCodeTool
from deeppyro.ptc.sdk import render_tools_sdk

PTC_ONLY_SECTION = "tools:ptc-only"
PTC_SDK_SECTION = "tools:sdk"
PTC_ONLY_ORDER = 41
PTC_SDK_ORDER = 42
PTC_ONLY_INSTRUCTION = (
    "`run_code` is the only tool you can call directly — a tool call naming any other tool "
    "fails. Reach every tool the SDK declares below from inside the program."
)


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
    """Provides the "ptc" service and, in ptc mode, run_code + SDK prompt."""

    provide = "ptc"
    plugin_id = "ptc"
    version = "0.1.0"
    description = "Python programmatic tool calling (run_code)"
    inject: ClassVar[list[str]] = ["config", "tools", "prompt"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.config = PtcConfig.from_mapping(_ptc_config(ctx, config))
        self.service = PtcRuntime(self.config)

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("ptc", self.service)
        if self.config.mode != "ptc":
            return
        ctx.register_tool(lambda _cfg, _workspace: [RunCodeTool(self.service)])
        ctx.register_prompt_section(PTC_ONLY_SECTION, PTC_ONLY_ORDER, self._render_ptc_only)
        ctx.register_prompt_section(PTC_SDK_SECTION, PTC_SDK_ORDER, self._render_sdk)

    @staticmethod
    def _render_ptc_only(tools: Any, prompt_context: Any) -> str:
        return PTC_ONLY_INSTRUCTION

    def _render_sdk(self, tools: Any, prompt_context: Any) -> str:
        try:
            visible: Sequence[ToolSchema] = [
                schema
                for schema in tools
                if schema.name != RUN_CODE_NAME and schema.name not in NESTED_EXCLUDED_TOOLS
            ]
            ordered = sorted(visible, key=lambda schema: schema.name)
            return render_tools_sdk(ordered)
        except Exception as exc:  # noqa: BLE001 - prompt assembly must never fail
            self.ctx.logger.warning(f"ptc SDK prompt section failed: {exc}")
            return ""


__all__ = [
    "PTC_ONLY_INSTRUCTION",
    "PTC_ONLY_ORDER",
    "PTC_ONLY_SECTION",
    "PTC_SDK_ORDER",
    "PTC_SDK_SECTION",
    "PtcPlugin",
]
