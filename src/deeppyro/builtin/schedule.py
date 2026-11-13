"""Kernel schedule plugin: provides the schedule service and its tool.

The plugin owns a :class:`deeppyro.schedule.ScheduleManager` and exposes it as
the schedule service. Starting and stopping the timer, plus attaching the
delivery host, is the runtime's job (the TUI/headless wiring calls
configure_host and start after boot, stop before close), so the plugin stays
stateless about sessions and can be mounted without a host.

Tools registered by the plugin read the manager from ToolContext.services and
reject child sessions on their own; the plugin does not touch agent internals.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Service
from deeppyro.schedule import ScheduleConfig, ScheduleManager
from deeppyro.tools.builtin.schedule import (
    SCHEDULE_PROMPT_ORDER,
    SCHEDULE_PROMPT_SECTION,
    authorization_section,
    schedule_tools,
)


def _schedule_config(ctx: Any, row_config: Any) -> Mapping[str, Any] | None:
    """Resolve the [schedule] table from the kernel config, falling back to the row config."""
    raw: Any = None
    config_service = ctx.get("config")
    value = getattr(config_service, "value", None) if config_service is not None else None
    if value is not None:
        raw = getattr(value, "schedule", None)
        if raw is None and isinstance(value, Mapping):
            raw = value.get("schedule")
    if raw is None and isinstance(row_config, Mapping):
        candidate = row_config.get("schedule")
        raw = candidate if candidate is not None else (row_config or None)
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    model_dump = getattr(raw, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump())
    return None


class SchedulePlugin(Service):
    """Exposes the schedule manager as the schedule service."""

    provide = "schedule"
    plugin_id = "schedule"
    version = "0.1.0"
    description = "scheduled reminders with persistent storage and a single timer"
    inject: ClassVar[list[str]] = ["config", "tools", "prompt"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        raw_config = _schedule_config(ctx, config)
        self.manager = ScheduleManager(config=ScheduleConfig.from_config(raw_config))

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.register_tool(lambda _config, _workspace: schedule_tools())
        ctx.register_prompt_section(
            SCHEDULE_PROMPT_SECTION,
            SCHEDULE_PROMPT_ORDER,
            authorization_section,
        )

    @property
    def schedule_manager(self) -> ScheduleManager:
        """Alias kept explicit for runtime wiring code."""
        return self.manager


__all__ = ["SchedulePlugin"]
