"""Tests for the builtin hook journal plugin (task-65)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from deeppyro.builtin.hook_journal import HookJournalPlugin
from deeppyro.hooks import (
    AGENT_ASSISTANT_STREAM,
    AGENT_PRE_STEP,
    LLM_STREAM,
    TOOL_CALL,
    HookService,
)
from deeppyro.hooks.records import INVOKED_RECORD_TYPE, MAX_PREVIEW, RESULT_RECORD_TYPE


class FakeCtx:
    """Minimal PluginContext: service lookup and effect bookkeeping."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.effects: list[str] = []
        self.disposers: list[Any] = []

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered")
        return self._services[name]

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        dispose = execute()
        self.effects.append(label)
        self.disposers.append(dispose)
        return dispose


class FakeAgent:
    """Duck-typed AgentSession that stores journal records."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    def record_hook_event(self, record_type: str, data: dict[str, Any]) -> int:
        if self.error is not None:
            raise self.error
        self.records.append((record_type, dict(data)))
        return len(self.records)


def make_plugin() -> tuple[HookJournalPlugin, HookService, FakeCtx]:
    hooks = HookService()
    ctx = FakeCtx({"hooks": hooks})
    plugin = HookJournalPlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, hooks, ctx


def record_types(agent: FakeAgent) -> list[str]:
    return [record_type for record_type, _data in agent.records]


def record_of(agent: FakeAgent, record_type: str) -> dict[str, Any]:
    for current_type, data in agent.records:
        if current_type == record_type:
            return data
    raise AssertionError(f"no {record_type!r} record in {agent.records!r}")


def test_plugin_metadata() -> None:
    assert HookJournalPlugin.plugin_id == "hook-journal"
    assert HookJournalPlugin.inject == ["hooks"]
    assert HookJournalPlugin.version


def test_setup_registers_one_effect() -> None:
    plugin, hooks, ctx = make_plugin()

    assert ctx.effects == ["hook-journal"]
    assert len(ctx.disposers) == 1
    assert plugin.records_written == 0
    assert hooks.has(TOOL_CALL) is False


def test_setup_requires_the_hooks_service() -> None:
    ctx = FakeCtx({})

    with pytest.raises(LookupError):
        HookJournalPlugin(ctx, None).setup(ctx, None)


async def test_emit_hook_journals_invoked_and_result() -> None:
    plugin, hooks, _ctx = make_plugin()
    agent = FakeAgent()

    await hooks.emit(
        TOOL_CALL,
        {"agent": agent, "tool": "read", "call": {"callId": "call-1"}, "turn": 2},
    )

    assert record_types(agent) == [INVOKED_RECORD_TYPE, RESULT_RECORD_TYPE]
    invoked = record_of(agent, INVOKED_RECORD_TYPE)
    assert invoked["hook"] == TOOL_CALL
    assert invoked["mode"] == "emit"
    assert invoked["tool"] == "read"
    assert invoked["callId"] == "call-1"
    assert invoked["turn"] == 2
    result = record_of(agent, RESULT_RECORD_TYPE)
    assert result["ok"] is True
    assert result["outcome"] == "none"
    assert result["durationMs"] >= 0
    assert plugin.records_written == 2


async def test_waterfall_hook_journals_its_result_value() -> None:
    plugin, hooks, _ctx = make_plugin()
    agent = FakeAgent()

    async def deny(payload: Any, next_step: Any) -> str:
        return "deny"

    hooks.on(AGENT_PRE_STEP, deny)

    assert await hooks.waterfall(AGENT_PRE_STEP, {"agent": agent, "turn": 1}, "fallback") == "deny"

    result = record_of(agent, RESULT_RECORD_TYPE)
    assert result["outcome"] == "value"
    assert result["preview"] == "deny"
    assert "valueType" not in result
    assert plugin.records_written == 2


async def test_long_result_is_truncated_in_the_journal() -> None:
    _plugin, hooks, _ctx = make_plugin()
    agent = FakeAgent()

    async def long_value(payload: Any, next_step: Any) -> str:
        return "x" * 500

    hooks.on(AGENT_PRE_STEP, long_value)
    await hooks.waterfall(AGENT_PRE_STEP, {"agent": agent}, "fallback")

    result = record_of(agent, RESULT_RECORD_TYPE)
    assert result["preview"] == "x" * MAX_PREVIEW
    assert len(result["preview"]) == MAX_PREVIEW


@pytest.mark.parametrize("hook", [LLM_STREAM, AGENT_ASSISTANT_STREAM])
async def test_hot_hooks_are_not_journaled(hook: str) -> None:
    plugin, hooks, _ctx = make_plugin()
    agent = FakeAgent()

    await hooks.emit(hook, {"agent": agent, "frame": object()})

    assert agent.records == []
    assert plugin.records_written == 0


async def test_payload_without_an_agent_is_skipped() -> None:
    plugin, hooks, _ctx = make_plugin()

    await hooks.emit(TOOL_CALL, {"tool": "read"})

    assert plugin.records_written == 0


async def test_agent_without_record_hook_event_is_skipped() -> None:
    plugin, hooks, _ctx = make_plugin()

    await hooks.emit(TOOL_CALL, {"agent": object(), "tool": "read"})

    assert plugin.records_written == 0


async def test_record_failure_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    plugin, hooks, _ctx = make_plugin()
    agent = FakeAgent(error=RuntimeError("session log is read-only"))
    caplog.set_level(logging.ERROR)

    await hooks.emit(TOOL_CALL, {"agent": agent, "tool": "read"})

    assert agent.records == []
    assert plugin.records_written == 0
    assert "hook journal failed" in caplog.text


async def test_disposing_the_effect_stops_journaling() -> None:
    plugin, hooks, ctx = make_plugin()
    agent = FakeAgent()

    await hooks.emit(TOOL_CALL, {"agent": agent})
    assert plugin.records_written == 2

    ctx.disposers[0]()
    await hooks.emit(TOOL_CALL, {"agent": agent})
    assert plugin.records_written == 2


async def test_scope_is_recorded_in_the_invoked_event() -> None:
    _plugin, hooks, _ctx = make_plugin()
    agent = FakeAgent()

    await hooks.emit(
        TOOL_CALL,
        {"agent": agent, "tool": "read"},
        scope={"session": "session-1", "preset": "default"},
    )

    assert record_of(agent, INVOKED_RECORD_TYPE)["scope"] == {
        "session": "session-1",
        "preset": "default",
    }
    assert "scope" not in record_of(agent, RESULT_RECORD_TYPE)


async def test_counter_tracks_successful_writes() -> None:
    plugin, hooks, _ctx = make_plugin()

    await hooks.emit(TOOL_CALL, {"agent": FakeAgent()})
    await hooks.emit(TOOL_CALL, {"agent": FakeAgent()})

    assert plugin.records_written == 4


async def test_no_journal_writes_for_unrelated_hooks_without_agents() -> None:
    plugin, hooks, _ctx = make_plugin()
    agent = FakeAgent()

    await hooks.emit(TOOL_CALL, {"tool": "read"})
    await hooks.emit(TOOL_CALL, {"agent": agent})
    await hooks.emit(LLM_STREAM, {"agent": agent})

    assert record_types(agent) == [INVOKED_RECORD_TYPE, RESULT_RECORD_TYPE]
    assert plugin.records_written == 2


async def test_error_records_are_not_written_after_failure() -> None:
    plugin, hooks, _ctx = make_plugin()
    agent = FakeAgent(error=RuntimeError("boom"))

    await hooks.serial(TOOL_CALL, {"agent": agent}, scope={"session": "s1"})

    assert agent.records == []
    assert plugin.records_written == 0
