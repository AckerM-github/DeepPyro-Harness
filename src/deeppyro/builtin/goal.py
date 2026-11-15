"""Builtin goal plugin: one goal per session, kept in the log and on the bus."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.goal import DEFAULT_MAX_GOAL_ROUNDS, GoalService
from deeppyro.tools.builtin.goal import (
    DEFAULT_BLOCKED_AFTER_CONSECUTIVE_ROUNDS,
    CreateGoalTool,
    GetGoalTool,
    GoalTool,
    UpdateGoalTool,
)

GOAL_POLICY = (
    "create_goal may infer goal intent from a direct human request in any language. "
    "After session resume or fork, an active goal is disarmed: when a human asks to "
    "continue or resume in any wording or language, use update_goal action resume to "
    "rearm it. Mark complete only when the objective is actually achieved. Mark blocked "
    "only after the same blocking condition persists for at least {rounds} consecutive "
    "goal rounds, and report that concrete condition in blocked_reason; difficulty, "
    "uncertainty, or useful remaining work is not blocked."
)


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


def _positive_int(config: Any, keys: tuple[str, ...], fallback: int, *, label: str) -> int:
    """Read one ``[goal]`` positive integer, refusing anything else at boot."""
    value: Any = None
    for key in keys:
        if isinstance(config, Mapping) and key in config:
            value = config[key]
            break
        if config is not None and hasattr(config, key):
            value = getattr(config, key)
            break
    if value is None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"[goal] {label} must be a positive integer, got {value!r}")
    if value <= 0:
        raise ValueError(f"[goal] {label} must be a positive integer, got {value!r}")
    return value


class GoalPlugin(Plugin):
    """Expose the goal service, its tools and the prompt line."""

    provide = "goal"
    plugin_id = "goal"
    version = "0.2.0"
    description = "per-session goal record, goal tools and prompt line"
    inject: ClassVar[list[str]] = ["sessions", "tools", "prompt"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        table = _service_config(config)
        self.service = GoalService(table)
        self.max_goal_rounds = _positive_int(
            table,
            ("maxGoalRounds", "max_goal_rounds"),
            DEFAULT_MAX_GOAL_ROUNDS,
            label="maxGoalRounds",
        )
        self.blocked_after = _positive_int(
            table,
            ("blockedAfterConsecutiveRounds", "blocked_after_consecutive_rounds"),
            DEFAULT_BLOCKED_AFTER_CONSECUTIVE_ROUNDS,
            label="blockedAfterConsecutiveRounds",
        )

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

    def _policy(self, tools: Any, context: Any) -> str:
        """The model guidance DSH keeps in the prompt (spec §5.4)."""
        if not getattr(context, "session_id", ""):
            return ""
        return "## Goal tool\n\n" + GOAL_POLICY.format(rounds=self.blocked_after)

    def goal_tools(self) -> list[Any]:
        """The tools this plugin registers (the shell stays for one minor)."""
        return [
            GetGoalTool(),
            CreateGoalTool(),
            UpdateGoalTool(blocked_after=self.blocked_after),
            GoalTool(),
        ]

    def setup(self, ctx: Any, config: Any) -> None:
        bus = self._bus(ctx)
        self.service.configure(emit=bus.emit if bus is not None else None, load=self._load)
        ctx.effect(lambda: self.service.configure(load=None), "goal: session loader")
        ctx.provide("goal", self.service)
        ctx.register_tool(lambda _cfg, _workspace: self.goal_tools())
        ctx.register_prompt_section("goal", 35, self._render)
        ctx.register_prompt_section("tool:goal", 36, self._policy)


__all__ = ["GOAL_POLICY", "GoalPlugin"]
