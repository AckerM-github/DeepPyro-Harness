"""Builtin goal round driver: opt-in automatic goal rounds.

A stock install keeps one round per human message: the driver is off unless
``[goal] driver = true`` is configured.  When it is on, the plugin attaches a
:class:`~deeppyro.goal.round_driver.RoundDriver` to the bus and to every live
root agent, so an armed goal keeps queueing ``<goal_round>`` prompts on idle
sessions until the goal is paused, completed, blocked or out of rounds.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin, PluginConfig, PluginContext
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.goal import GoalService
from deeppyro.goal.round_driver import RoundDriver
from deeppyro.hooks.service import HookService

logger = logging.getLogger(__name__)


def _service_config(config: PluginConfig) -> Any:
    """The ``[goal]`` row or table inside a plugin config."""
    if isinstance(config, Mapping):
        for key in ("goal", "goals"):
            value = config.get(key)
            if value is not None:
                return value
        return config
    for key in ("goal", "goals"):
        value = getattr(config, key, None)
        if value is not None:
            return value
    return config


def _merged_config(ctx: PluginContext, row_config: PluginConfig) -> dict[str, Any]:
    """Merge the bundle row config under the ``[goal]`` table of the kernel config."""
    merged: dict[str, Any] = {}
    table = _service_config(row_config)
    if isinstance(table, Mapping):
        merged.update(table)
    getter = getattr(ctx, "get", None)
    config_service = getter("config") if callable(getter) else None
    value = getattr(config_service, "value", None) if config_service is not None else None
    raw: Any = None
    if value is not None:
        raw = getattr(value, "goal", None)
        if raw is None and isinstance(value, Mapping):
            raw = value.get("goal")
    if isinstance(raw, Mapping):
        merged.update(raw)
    else:
        model_dump = getattr(raw, "model_dump", None)
        if callable(model_dump):
            merged.update(model_dump())
    return merged


def _driver_enabled(table: Mapping[str, Any]) -> bool:
    """Read ``[goal] driver``, which is off unless it says otherwise."""
    value = table.get("driver")
    if value is None:
        return False
    if isinstance(value, Mapping):
        value = value.get("enabled")
        if value is None:
            return False
    if not isinstance(value, bool):
        raise TypeError(f"[goal] driver must be a boolean, got {value!r}")
    return value


class GoalRoundDriverPlugin(Plugin):
    """Attach the goal round driver when ``[goal] driver`` is enabled."""

    plugin_id = "goal-round-driver"
    version = "0.1.0"
    description = "opt-in automatic goal rounds for armed goals"
    inject: ClassVar[list[str]] = ["hooks", "agents"]

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.enabled = False
        self.driver: RoundDriver | None = None

    def setup(self) -> None:
        self.enabled = _driver_enabled(_merged_config(self.ctx, self.config))
        if not self.enabled:
            # Off means off: no listeners, no bus traffic, no state at all.
            return
        driver = RoundDriver(
            hooks=self.ctx.require(HookService),
            agents=self.ctx.require(AgentsPlugin),
            goals=self.ctx.get(GoalService),
        )
        self.driver = driver
        driver.attach()
        self.ctx.effect(driver.detach, "goal-round-driver: listeners")
        logger.debug("goal round driver: attached")


__all__ = ["GoalRoundDriverPlugin"]
