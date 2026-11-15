"""Builtin goal plugin: one goal per session, kept in the log and on the bus."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.goal import GoalService
from deeppyro.tools.builtin.goal import GoalTool


def _service_config(config: Any) -> Any:
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


class GoalPlugin(Plugin):
    """Expose the goal service, its tool and the prompt line."""

    provide = "goal"
    plugin_id = "goal"
    version = "0.1.0"
    description = "per-session goal record, tool and prompt line"
    inject: ClassVar[list[str]] = ["sessions", "tools", "prompt"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.service = GoalService(_service_config(config))

    def _bus(self, ctx: Any) -> Any | None:
        try:
            return ctx.get("bus")
        except Exception:  # noqa: BLE001 - the plugin still works without a bus
            return None

    def _load(self, session_id: str) -> list[Any]:
        """Durable records of one session, so a reopened goal survives."""
        try:
            sessions = self.ctx.service("sessions")
            store = getattr(sessions, "store", None)
            if store is None:
                return []
            session = store.open(session_id)
            return list(getattr(session, "records", ()) or ())
        except Exception:  # noqa: BLE001 - a missing store only costs the goal
            return []

    def _render(self, tools: Any, context: Any) -> str:
        session_id = getattr(context, "session_id", "")
        if not session_id:
            return ""
        goal = self.service.get(session_id)
        if not goal:
            return ""
        return f"## Goal\n\nThe user's goal for this session: {goal}"

    def setup(self, ctx: Any, config: Any) -> None:
        bus = self._bus(ctx)
        self.service.configure(emit=bus.emit if bus is not None else None, load=self._load)
        ctx.effect(lambda: self.service.configure(load=None), "goal: session loader")
        ctx.provide("goal", self.service)
        ctx.register_tool(lambda _cfg, _workspace: [GoalTool()])
        ctx.register_prompt_section("goal", 35, self._render)


__all__ = ["GoalPlugin"]
