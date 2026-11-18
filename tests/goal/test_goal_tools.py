"""Goal plugin wiring: tool registration and the [goal] configuration (M2a)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.builtin.goal import (
    DEFAULT_BLOCKED_AFTER_CONSECUTIVE_ROUNDS,
    GoalPlugin,
    _positive_int,
)
from deeppyro.tools.builtin.goal import UpdateGoalTool


class StubContext:
    def __init__(self) -> None:
        self.tools: list[Any] = []
        self.sections: list[tuple[str, int, Any]] = []
        self.provided: dict[str, Any] = {}
        self.disposers: list[Any] = []

    def get(self, name: str) -> Any:
        raise KeyError(name)

    def service(self, name: str) -> Any:
        raise KeyError(name)

    def effect(self, execute: Any, label: str) -> None:
        # Production semantics: record the teardown callback; never run it now.
        self.disposers.append(execute)

    def provide(self, name: str, service: Any) -> None:
        self.provided[name] = service

    def register_tool(self, factory: Any, *, scope: Any = None) -> None:
        self.tools.append(factory)

    def register_prompt_section(self, name: str, order: int, render: Any) -> None:
        self.sections.append((name, order, render))


def build(config: Any = None) -> tuple[GoalPlugin, StubContext]:
    ctx = StubContext()
    plugin = GoalPlugin(ctx, config if config is not None else {})
    plugin.setup()
    return plugin, ctx


def tool_names(plugin: GoalPlugin, ctx: StubContext) -> list[str]:
    factory = ctx.tools[0]
    return [tool.name for tool in factory({}, None)]


def test_plugin_metadata_and_registration() -> None:
    plugin, ctx = build()
    assert plugin.version == "0.2.0"
    assert plugin.provide == "goal"
    assert "goal" in ctx.provided
    assert ctx.provided["goal"] is plugin.service
    assert ctx.sections and ctx.sections[0][:2] == ("goal", 35)
    assert tool_names(plugin, ctx) == ["get_goal", "create_goal", "update_goal", "goal"]


def test_defaults_follow_the_contract() -> None:
    plugin, ctx = build()
    assert plugin.max_goal_rounds == 256
    assert plugin.blocked_after == DEFAULT_BLOCKED_AFTER_CONSECUTIVE_ROUNDS == 3
    tools = ctx.tools[0]({}, None)
    update = next(tool for tool in tools if isinstance(tool, UpdateGoalTool))
    assert update.blocked_after == 3


def test_the_goal_table_is_read() -> None:
    plugin, ctx = build({"goal": {"maxGoalRounds": 12, "blockedAfterConsecutiveRounds": 5}})
    assert plugin.max_goal_rounds == 12
    assert plugin.blocked_after == 5
    tools = ctx.tools[0]({}, None)
    assert next(tool for tool in tools if isinstance(tool, UpdateGoalTool)).blocked_after == 5


def test_attribute_style_config_is_accepted() -> None:
    config = SimpleNamespace(goal=SimpleNamespace(maxGoalRounds=7, blockedAfterConsecutiveRounds=2))
    plugin, _ctx = build(config)
    assert plugin.max_goal_rounds == 7
    assert plugin.blocked_after == 2


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_thresholds_raise_value_error(value: int) -> None:
    with pytest.raises(ValueError):
        GoalPlugin(StubContext(), {"goal": {"blockedAfterConsecutiveRounds": value}})


@pytest.mark.parametrize("value", ["3", 2.5, True, []])
def test_non_integer_thresholds_raise_type_error(value: Any) -> None:
    with pytest.raises(TypeError):
        GoalPlugin(StubContext(), {"goal": {"blockedAfterConsecutiveRounds": value}})


@pytest.mark.parametrize("value", [0, -5])
def test_non_positive_max_rounds_raise_value_error(value: int) -> None:
    with pytest.raises(ValueError):
        GoalPlugin(StubContext(), {"goal": {"maxGoalRounds": value}})


def test_positive_int_reads_mapping_and_attributes() -> None:
    assert (
        _positive_int({"blockedAfterConsecutiveRounds": 4}, ("blockedAfterConsecutiveRounds",), 3, label="x")
        == 4
    )
    assert (
        _positive_int(
            SimpleNamespace(blockedAfterConsecutiveRounds=6), ("blockedAfterConsecutiveRounds",), 3, label="x"
        )
        == 6
    )
    assert _positive_int({}, ("blockedAfterConsecutiveRounds",), 3, label="x") == 3


def test_setup_registers_a_disposer_for_the_session_loader() -> None:
    plugin, ctx = build()
    assert len(ctx.disposers) == 1
    # The loader is wired eagerly; effect() only records the teardown callback.
    assert plugin.service._loader is not None
    assert callable(ctx.disposers[0])
    ctx.disposers[0]()
    # configure(load=None) is a late-binding no-op, so the loader keeps working.
    assert plugin._load("session-missing") == []
