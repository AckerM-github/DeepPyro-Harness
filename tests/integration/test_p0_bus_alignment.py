"""M1: one bus. A plugin subscribes to a domain event and sees the real dispatch.

DSH has a single cordis ``EventsService``: ``agent/pre-step``, ``tools/execute``
and friends are events like any other, so a third-party plugin observes them by
name. deeppyro used to hide them behind ``hooks`` plus a cordis republisher
(``hook-bridge``), which meant a plugin could subscribe to ``hook/invoked`` and
filter, but not to the event itself.

This test pins the new shape: ``ctx.on("tools/pre-execute", handler)`` on a
``PluginContext`` registers a listener on the kernel bus and receives the real
payload while a turn runs.
"""

from __future__ import annotations

from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.api.context import PluginContext
from deeppyro.core.protocols import TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.hooks import HookService
from tests.agent.fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    FakeTool,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)


class StubContext:
    """The minimum raw context ``PluginContext`` needs to reach the bus."""

    def __init__(self, hooks: HookService) -> None:
        self.hooks = hooks
        self.reflect = self

    def get(self, name: str) -> Any | None:
        return self.hooks if name == "hooks" else None

    def on(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - runtime fallback
        raise AssertionError("domain events must not fall back to the runtime dispatcher")

    def once(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - runtime fallback
        raise AssertionError("domain events must not fall back to the runtime dispatcher")

    def effect(self, execute: Any, label: str = "") -> Any:  # pragma: no cover - unused
        return execute()


def make_agent(tmp_path, script, hooks: HookService):
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry([FakeTool("echo", text="ok")]),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        services={"hooks": hooks},
    )
    return session, provider, agent


async def test_a_plugin_observes_tools_pre_execute_through_the_bus(tmp_path) -> None:
    hooks = HookService()
    plugin = PluginContext(StubContext(hooks), plugin_id="third-party")
    seen: list[dict[str, Any]] = []

    async def observe(payload: dict[str, Any], next_step: Any) -> Any:
        seen.append(payload)
        return await next_step()

    disposer = plugin.on("tools/pre-execute", observe)
    assert hooks.has("tools/pre-execute") is True

    script = [
        ScriptedTurn(
            events=[ToolCallStart("call_1", "echo"), ToolCallArgsDelta("call_1", "{}"), finish("tool-calls")]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    _session, _provider, agent = make_agent(tmp_path, script, hooks)

    assert await agent.send("go") == "done"
    assert [(payload["tool"], payload["call"]["callId"]) for payload in seen] == [("echo", "call_1")]

    disposer()
    assert hooks.has("tools/pre-execute") is False


async def test_a_plugin_may_decline_a_tool_with_one_listener(tmp_path) -> None:
    """``ctx.once`` mirrors the bus once semantics."""
    hooks = HookService()
    plugin = PluginContext(StubContext(hooks), plugin_id="third-party")
    calls: list[str] = []

    def decline(payload: dict[str, Any], next_step: Any) -> Any:
        calls.append(payload["tool"])
        return {"kind": "deny", "reason": "not today"}

    plugin.once("tools/pre-execute", decline)

    script = [
        ScriptedTurn(
            events=[ToolCallStart("call_1", "echo"), ToolCallArgsDelta("call_1", "{}"), finish("tool-calls")]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, _provider, agent = make_agent(tmp_path, script, hooks)

    await agent.send("go")

    assert calls == ["echo"]
    assert hooks.has("tools/pre-execute") is False
    assert session.records_of("tool/result")[0].data["message"]["isError"] is True
