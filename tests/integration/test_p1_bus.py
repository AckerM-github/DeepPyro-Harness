"""Integration checks for the P1 bus (hook scope isolation and consumer vetoes).

The hook runtime already implements scoped listeners; these tests drive the real
AgentSession and tools pipeline to prove the dispatch sites actually pass a
scope, that two agents sharing one HookService cannot see each other's hooks,
and that a veto follows each consumer's documented fallback behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.protocols import Finish, TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.hooks import HookService
from deeppyro.hooks.types import (
    AGENT_PRE_STEP,
    AGENT_STATUS,
    APPROVAL_REQUEST,
    SYSTEM_PROMPT_ASSEMBLE,
    TOOL_CALL,
)
from deeppyro.sandbox.gate import ApprovalRequest, PermissionGate
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


def make_agent(
    tmp_path: Path,
    script: list[ScriptedTurn],
    *,
    hooks: HookService,
    registry: FakeRegistry | None = None,
) -> tuple[FakeSessionLog, ScriptedProvider, AgentSession]:
    """Build a real agent loop over the in-memory fakes with a shared hook service."""
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=registry if registry is not None else FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        services={"hooks": hooks},
    )
    return session, provider, agent


def _stop_turn() -> ScriptedTurn:
    return ScriptedTurn(events=[TextDelta("ok"), finish("stop")])


def _tool_turn(name: str) -> ScriptedTurn:
    return ScriptedTurn(
        events=[
            ToolCallStart("call_1", name),
            ToolCallArgsDelta("call_1", "{}"),
            Finish("tool-calls"),
        ]
    )


async def test_scoped_listeners_do_not_cross_between_sessions(tmp_path: Path) -> None:
    """A listener scoped to one session only sees dispatches carrying that session id."""
    hooks = HookService()
    by_session: list[Any] = []
    by_preset: list[Any] = []
    unscoped: list[Any] = []
    global_seen: list[Any] = []

    async def session_listener(payload: Any, next_step: Any) -> Any:
        by_session.append(payload["agent"])
        return await next_step()

    async def preset_listener(payload: Any, next_step: Any) -> Any:
        by_preset.append(payload["agent"])
        return await next_step()

    async def broadcast(payload: Any, next_step: Any) -> Any:
        unscoped.append(payload["agent"])
        return await next_step()

    async def global_listener(payload: Any, next_step: Any) -> Any:
        global_seen.append(payload["agent"])
        return await next_step()

    session_a, _provider_a, agent_a = make_agent(tmp_path, [_stop_turn()], hooks=hooks)
    _session_b, _provider_b, agent_b = make_agent(tmp_path, [_stop_turn()], hooks=hooks)
    agent_a.preset = "default"
    agent_b.preset = "team"
    hooks.on(AGENT_PRE_STEP, session_listener, scope={"session": session_a.session_id})
    hooks.on(AGENT_PRE_STEP, preset_listener, scope={"preset": "team"})
    hooks.on(AGENT_PRE_STEP, broadcast)
    hooks.on(AGENT_PRE_STEP, global_listener, global_=True)

    assert await agent_a.send("first") == "ok"
    assert await agent_b.send("second") == "ok"

    assert agent_a.session_id == session_a.session_id
    assert by_session == [agent_a]
    assert by_preset == [agent_b]
    assert unscoped == [agent_a, agent_b]
    assert global_seen == [agent_a, agent_b]


async def test_dispatch_without_a_scope_skips_scoped_listeners(tmp_path: Path) -> None:
    """Only unscoped and global listeners receive a dispatch that carries no scope."""
    hooks = HookService()
    scoped: list[Any] = []
    global_seen: list[Any] = []
    hooks.on(AGENT_STATUS, lambda payload: scoped.append(payload["status"]), scope={"session": "s-1"})
    hooks.on(AGENT_STATUS, lambda payload: global_seen.append(payload["status"]), global_=True)

    await hooks.emit(AGENT_STATUS, {"status": "running"})
    assert scoped == []
    assert global_seen == ["running"]

    await hooks.emit(AGENT_STATUS, {"status": "idle"}, scope={"session": "s-1"})
    assert scoped == ["idle"]
    assert global_seen == ["running", "idle"]


async def test_tool_call_scope_follows_the_executing_session(tmp_path: Path) -> None:
    """tool/call dispatched by the pipeline carries the owning agent's session scope."""
    hooks = HookService()
    registry = FakeRegistry([FakeTool("echo")])
    session_a, _provider_a, agent_a = make_agent(
        tmp_path, [_tool_turn("echo"), _stop_turn()], hooks=hooks, registry=registry
    )
    session_b, _provider_b, agent_b = make_agent(
        tmp_path, [_tool_turn("echo"), _stop_turn()], hooks=hooks, registry=registry
    )
    owner_a: list[Any] = []
    owner_b: list[Any] = []
    hooks.on(TOOL_CALL, owner_a.append, scope={"session": session_a.session_id})
    hooks.on(TOOL_CALL, owner_b.append, scope={"session": session_b.session_id})

    assert await agent_a.send("a") == "ok"
    assert await agent_b.send("b") == "ok"

    assert [payload["agent"] for payload in owner_a] == [agent_a]
    assert [payload["agent"] for payload in owner_b] == [agent_b]
    assert [payload["name"] for payload in owner_a] == ["echo"]


async def test_pre_step_veto_blocks_the_turn(tmp_path: Path) -> None:
    """A listener that neither continues the chain nor decides vetoes the turn."""
    hooks = HookService()
    hooks.on(AGENT_PRE_STEP, lambda payload, next_step: None)
    session, provider, agent = make_agent(tmp_path, [_stop_turn()], hooks=hooks)

    assert await agent.send("go") == ""

    assert provider.requests == []
    ends = session.records_of("turn/end")
    assert len(ends) == 1
    reason = ends[0].data["reason"]
    assert reason["kind"] == "blocked"
    assert reason["reason"]["kind"] == "pre-step"
    assert reason["reason"]["reason"] == "agent/pre-step hook vetoed the turn"


async def test_system_prompt_assemble_veto_keeps_the_rendered_prompt(tmp_path: Path) -> None:
    """A veto suppressing the assemble dispatch leaves the built-in prompt in place."""
    hooks = HookService()
    hooks.on(SYSTEM_PROMPT_ASSEMBLE, lambda payload, next_step: None)
    session, provider, agent = make_agent(tmp_path, [_stop_turn()], hooks=hooks)

    assert await agent.send("hello") == "ok"

    assert session.records_of("system/message")
    system = "\n".join(
        message.text() for message in provider.requests[0].messages if message.role == "system"
    )
    assert system


async def test_approval_request_veto_falls_back_to_the_policy(tmp_path: Path) -> None:
    """A veto suppressing approval/request lets the configured policy decide."""
    hooks = HookService()
    hooks.on(APPROVAL_REQUEST, lambda payload, next_step: None)
    always = PermissionGate("workspace-write", "always", tmp_path, hooks=hooks)
    never = PermissionGate("workspace-write", "never", tmp_path, hooks=hooks)

    allowed = await always.request(ApprovalRequest(kind="command", summary="run"))
    denied = await never.request(ApprovalRequest(kind="command", summary="run"))

    assert allowed.allowed is True
    assert allowed.reason == "approval policy is always"
    assert denied.allowed is False
    assert denied.reason == "approval policy is never"
