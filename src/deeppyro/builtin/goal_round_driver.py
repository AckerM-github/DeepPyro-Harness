"""Builtin goal round driver: opt-in automatic goal rounds for native hosts.

A stock install keeps one round per human message: the driver is off unless
[goal] driver = true is configured. When it is on, the plugin attaches a
NativeRoundDriver to the bus and adopts every native agent host the agents
service creates, so an armed goal keeps sending goal rounds until the goal is
paused, completed, blocked or out of rounds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin, PluginConfig, PluginContext
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.goal import GoalService
from deeppyro.goal.native_driver import NativeRoundDriver, is_native_host
from deeppyro.hooks.service import HookService
from deeppyro.kernel.config_service import KernelConfig

logger = logging.getLogger(__name__)

#: Service name the native driver is published under for other plugins.
NATIVE_DRIVER_SERVICE = "goal-round-driver-native"


def _service_config(config: PluginConfig) -> Any:
    """The [goal] row or table inside a plugin config."""
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
    """Merge the bundle row config under the [goal] table of the kernel config."""
    merged: dict[str, Any] = {}
    table = _service_config(row_config)
    if isinstance(table, Mapping):
        merged.update(table)
    config_service = ctx.get(KernelConfig)
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
    """Read [goal] driver, which is off unless it says otherwise."""
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


class NativeGoalRoundDriverPlugin(Plugin):
    """Attach the native goal round driver when [goal] driver is enabled.

    The driver adopts native agent hosts (NativeAgentHost) created by the
    agents service: a native host records every turn as schema 1 facts, so the
    rounds are read back from the folded goal.changed log.
    """

    plugin_id = "goal-round-driver"
    version = "0.3.0"
    description = "opt-in automatic goal rounds for armed native sessions"
    inject: ClassVar[list[str]] = ["hooks", "agents"]

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.enabled = False
        self.driver: NativeRoundDriver | None = None

    def setup(self) -> None:
        self.enabled = _driver_enabled(_merged_config(self.ctx, self.config))
        if not self.enabled:
            # Off means off: no listeners, no bus traffic, no state at all.
            return
        driver = NativeRoundDriver(
            hooks=self.ctx.require(HookService),
            goals=self.ctx.get(GoalService),
        )
        agents = self.ctx.require(AgentsPlugin)
        self.driver = driver
        disposers: list[Callable[[], Any]] = [
            driver.attach(),
            agents.on_created(self._adopt),
        ]
        # Hosts created before this plugin loaded are adopted too; adoption
        # never arms, so this is inert.
        for agent in agents.live_agents():
            self._adopt(agent)
        self.ctx.provide(NATIVE_DRIVER_SERVICE, driver)

        def dispose() -> None:
            for disposer in disposers:
                disposer()
            driver.detach()

        self.ctx.effect(dispose, "goal-round-driver: listeners")
        logger.debug("native goal round driver: attached")

    def _adopt(self, agent: Any) -> None:
        driver = self.driver
        if driver is None or not is_native_host(agent):
            return
        driver.adopt(agent)


__all__ = ["NATIVE_DRIVER_SERVICE", "NativeGoalRoundDriverPlugin"]
