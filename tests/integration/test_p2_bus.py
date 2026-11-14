"""Integration checks for the P2 bus (hook journaling and the cordis bridge).

P2 turns the hook runtime observable: HookService notifies registered observers,
the hook-journal plugin writes hook/invoked + hook/result into the executing
agent's session log, and the hook-bridge plugin republishes dispatches as cordis
events. These tests drive a real AgentSession over the in-memory fakes to prove
the wiring is end-to-end and that hot streaming hooks stay off the bus.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.builtin.approval_policy import ApprovalPolicyPlugin
from deeppyro.builtin.hook_bridge import HookBridgePlugin
from deeppyro.builtin.hook_journal import HookJournalPlugin
from deeppyro.builtin.prompt_extension import PromptExtensionPlugin
from deeppyro.hooks import (
    AGENT_ASSISTANT_STREAM,
    AGENT_PRE_STEP,
    AGENT_TURN_STOPPING,
    LLM_STREAM,
    MODEL_SELECTION,
    SYSTEM_PROMPT_ASSEMBLE,
    HookService,
)
from deeppyro.kernel.hook_bridge import EVENT_INVOKED, EVENT_RESULT, HookEventScope
from deeppyro.sandbox import ApprovalRequest, PermissionGate
from tests.agent.fakes import RecordingSink
from tests.integration.test_p1_bus import _stop_turn, make_agent


class FakeCtx:
    """Minimal PluginContext with service lookup, effect bookkeeping and emit."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.effects: list[str] = []
        self.disposers: list[Any] = []
        self.emitted: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered")
        return self._services[name]

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        dispose = execute()
        self.effects.append(label)
        self.disposers.append(dispose)
        return dispose

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.emitted.append((args, kwargs))


def _journal(hooks: HookService) -> tuple[HookJournalPlugin, FakeCtx]:
    ctx = FakeCtx({"hooks": hooks})
    plugin = HookJournalPlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, ctx


def _bridge(hooks: HookService) -> tuple[HookBridgePlugin, FakeCtx]:
    ctx = FakeCtx({"hooks": hooks})
    plugin = HookBridgePlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, ctx


async def test_agent_turn_journals_hook_records_without_hot_hooks(tmp_path: Path) -> None:
    """A real turn writes paired hook/invoked and hook/result records to the session."""
    hooks = HookService()
    sink = RecordingSink()
    session, _provider, agent = make_agent(tmp_path, [_stop_turn()], hooks=hooks, sink=sink)
    sink.session = session
    journal, _ctx = _journal(hooks)

    assert await agent.send("hello") == "ok"

    invoked = session.records_of("hook/invoked")
    results = session.records_of("hook/result")
    assert invoked and results
    assert len(invoked) == len(results)
    assert journal.records_written == len(invoked) + len(results)

    names = {record.data["hook"] for record in invoked}
    assert {AGENT_PRE_STEP, MODEL_SELECTION, SYSTEM_PROMPT_ASSEMBLE, AGENT_TURN_STOPPING} <= names
    assert LLM_STREAM not in names
    assert AGENT_ASSISTANT_STREAM not in names
    assert all(record.data.get("scope", {}).get("session") == session.session_id for record in invoked)

    # Every invoked record is answered by a later result record for the same hook.
    for record in invoked:
        matching = [
            other for other in results if other.data["hook"] == record.data["hook"] and other.seq > record.seq
        ]
        assert matching, record.data["hook"]

    for record in results:
        assert isinstance(record.data["ok"], bool)
        assert record.data["mode"] in {"waterfall", "serial", "emit", "bail", "parallel"}

    # The live sink mirrors the durable log one-for-one (headless NDJSON sees this).
    assert {event["seq"] for event in sink.of_type("hook/invoked")} == {record.seq for record in invoked}
    assert {event["seq"] for event in sink.of_type("hook/result")} == {record.seq for record in results}
    assert all(event["type"] != "error" for event in sink.events)


async def test_bridge_republishes_dispatches_as_cordis_events(tmp_path: Path) -> None:
    """The bridge turns each dispatch into hook/invoked and hook/result emits."""
    hooks = HookService()
    _bridge_plugin, ctx = _bridge(hooks)

    await hooks.serial(AGENT_TURN_STOPPING, {"turn": 1, "signal": None}, scope={"session": "s-1"})

    names = [call[0][1] for call in ctx.emitted]
    assert names == [EVENT_INVOKED, EVENT_RESULT]
    this_args = [call[0][0] for call in ctx.emitted]
    assert all(isinstance(arg, HookEventScope) for arg in this_args)
    assert dict(this_args[0].scope or {}) == {"session": "s-1"}
    assert this_args[0].__filter__(None) is True

    invoked_data = ctx.emitted[0][0][2]
    result_data = ctx.emitted[1][0][2]
    assert invoked_data["hook"] == AGENT_TURN_STOPPING
    assert invoked_data["mode"] == "serial"
    assert result_data["hook"] == AGENT_TURN_STOPPING
    assert result_data["ok"] is True


class FakeConfigService:
    """Minimal config service exposing a plain mapping as .value."""

    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


def _prompt_extension(hooks: HookService, extension: str) -> tuple[Any, FakeCtx]:
    ctx = FakeCtx({"hooks": hooks, "config": FakeConfigService({"prompt": {"extension": extension}})})
    plugin = PromptExtensionPlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, ctx


def _approval_policy(hooks: HookService, **section: Any) -> tuple[Any, FakeCtx]:
    ctx = FakeCtx({"hooks": hooks, "config": FakeConfigService({"approval": section})})
    plugin = ApprovalPolicyPlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, ctx


async def test_prompt_extension_appends_text_to_the_assembled_prompt(tmp_path: Path) -> None:
    """The P2 post-order consumer appends [prompt].extension to the real prompt."""
    hooks = HookService()
    _plugin, _ctx = _prompt_extension(hooks, "EXTRA-RULES-FOR-THE-AGENT")
    _session, provider, agent = make_agent(tmp_path, [_stop_turn()], hooks=hooks)

    assert await agent.send("hello") == "ok"

    system = "\n".join(
        message.text() for message in provider.requests[0].messages if message.role == "system"
    )
    assert system.rstrip().endswith("EXTRA-RULES-FOR-THE-AGENT")


async def test_approval_policy_plugin_answers_the_gate(tmp_path: Path) -> None:
    """[approval].hookMode decides matching requests before the interactive policy."""
    hooks = HookService()
    _plugin, _ctx = _approval_policy(hooks, hookMode="deny", hookTools=["shell"])
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)

    denied = await gate.request(ApprovalRequest(kind="command", summary="run", tool="shell"))
    assert denied.allowed is False
    assert denied.reason == "denied by approval plugin"

    unmatched = await gate.request(ApprovalRequest(kind="command", summary="run", tool="read"))
    assert unmatched.allowed is False
    assert unmatched.reason == "no interactive approval handler is available"


async def test_approval_policy_can_allow_without_prompting(tmp_path: Path) -> None:
    """hookMode=allow approves matching tools through the hook, not the ask path."""
    hooks = HookService()
    _plugin, _ctx = _approval_policy(hooks, hookMode="allow")
    gate = PermissionGate("workspace-write", "ask", tmp_path, hooks=hooks)

    allowed = await gate.request(ApprovalRequest(kind="command", summary="run", tool="shell"))
    assert allowed.allowed is True
    assert allowed.reason == "approved by approval plugin"


async def test_bridge_skips_hot_streaming_hooks() -> None:
    """llm/stream and agent/assistant-stream never reach the cordis event bus."""
    hooks = HookService()
    _bridge_plugin, ctx = _bridge(hooks)

    await hooks.emit(LLM_STREAM, {"frame": {"kind": "text", "delta": "x"}}, scope={"session": "s"})
    assert ctx.emitted == []
