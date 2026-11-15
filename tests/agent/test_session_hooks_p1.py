"""P1 consumer hooks: payloads, waterfall consumption and failure phases.

These tests pin the five consumer hooks the agent loop dispatches:
system-prompt/assemble, llm/stream, tool/call, model/selection and
agent/error.  They assert the payload each listener receives (frozen, read
only), that a waterfall listener actually changes behaviour, and that
failures publish exactly one agent/error with the right phase.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.errors import LlmError, SessionError
from deeppyro.core.protocols import TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.hooks import HookService
from deeppyro.hooks.types import (
    AGENT_ASSISTANT_STREAM,
    AGENT_ERROR,
    LLM_STREAM,
    MODEL_SELECTION,
    SYSTEM_PROMPT_ASSEMBLE,
    TOOL_CALL,
)

from .fakes import (
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
    registry: FakeRegistry | None = None,
    services: dict[str, Any] | None = None,
    prompt_builder: Any = None,
) -> tuple[FakeSessionLog, ScriptedProvider, RecordingSink, AgentSession]:
    """Build a loop over in-memory fakes with optional hook services."""
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
        services=services,
        prompt_builder=prompt_builder,
    )
    return session, provider, sink, agent


async def _wait_for(predicate: Any, *, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached before timeout")


def _system_text(record: Any) -> str:
    parts = record.data["message"]["content"]
    return "\n".join(str(part.get("text", "")) for part in parts)


async def test_system_prompt_assemble_payload_is_frozen_and_applied(tmp_path: Path) -> None:
    """system-prompt/assemble receives a frozen payload and may rewrite the prompt."""
    hooks = HookService()
    seen: list[Any] = []

    def assemble(payload: Any, nxt: Any) -> str:
        seen.append(payload)
        return payload["prompt"] + "\n\n<hook-marker/>"

    hooks.on(SYSTEM_PROMPT_ASSEMBLE, assemble)
    registry = FakeRegistry([FakeTool("echo")])
    session, provider, _sink, agent = make_agent(
        tmp_path,
        [ScriptedTurn(events=[TextDelta("hi"), finish("stop")])],
        registry=registry,
        services={"hooks": hooks},
    )

    assert await agent.send("hello") == "hi"

    assert len(seen) == 1
    payload = seen[0]
    assert payload["agent"] is agent
    assert payload["turn"] == 1
    assert payload["step"] == 1
    assert payload["signal"] is None
    assert isinstance(payload["prompt"], str) and payload["prompt"]
    assert isinstance(payload, MappingProxyType)
    assert isinstance(payload["tools"], tuple)
    assert [tool["name"] for tool in payload["tools"]] == ["echo"]
    assert isinstance(payload["tools"][0], MappingProxyType)
    with pytest.raises(TypeError):
        payload["prompt"] = "mutated"
    with pytest.raises(TypeError):
        payload["tools"][0]["name"] = "mutated"

    system_records = session.records_of("system/message")
    assert system_records
    assert "<hook-marker/>" in _system_text(system_records[-1])
    request_messages = provider.requests[0].messages
    assert "<hook-marker/>" in "\n".join(
        message.text() for message in request_messages if message.role == "system"
    )


async def test_system_prompt_assemble_fallback_is_the_rendered_prompt(tmp_path: Path) -> None:
    """A listener that awaits next() and returns None leaves the prompt untouched."""
    hooks = HookService()
    seen: list[Any] = []

    async def observe(payload: Any, nxt: Any) -> None:
        seen.append(payload["prompt"])
        assert await nxt() == payload["prompt"]

    hooks.on(SYSTEM_PROMPT_ASSEMBLE, observe)
    session, _provider, _sink, agent = make_agent(
        tmp_path, [ScriptedTurn(events=[finish("stop")])], services={"hooks": hooks}
    )

    await agent.send("hello")

    assert len(seen) == 1
    system_records = session.records_of("system/message")
    assert len(system_records) == 1
    assert _system_text(system_records[0]) == seen[0]


async def test_system_prompt_assemble_rejects_a_non_string(tmp_path: Path) -> None:
    """Returning a structure violates the contract and fails the turn."""
    hooks = HookService()
    hooks.on(SYSTEM_PROMPT_ASSEMBLE, lambda payload, nxt: {"prompt": "no"})
    _session, _provider, _sink, agent = make_agent(
        tmp_path, [ScriptedTurn(events=[finish("stop")])], services={"hooks": hooks}
    )

    with pytest.raises(SessionError, match="system-prompt/assemble hook returned dict, expected str"):
        await agent.send("hello")


async def test_llm_stream_emits_frames_with_the_route(tmp_path: Path) -> None:
    """llm/stream mirrors every assistant frame and names the route that produced it."""
    hooks = HookService()
    frames: list[Any] = []
    assistant_frames: list[Any] = []
    hooks.on(LLM_STREAM, frames.append)
    hooks.on(AGENT_ASSISTANT_STREAM, assistant_frames.append)
    _session, _provider, _sink, agent = make_agent(
        tmp_path,
        [ScriptedTurn(events=[TextDelta("hi"), finish("stop")])],
        services={"hooks": hooks},
    )

    assert await agent.send("hello") == "hi"

    assert frames
    first = frames[0]
    assert isinstance(first, MappingProxyType)
    assert set(first) == {"agent", "frame", "provider", "model"}
    assert first["agent"] is agent
    assert first["provider"] == "fake-provider"
    assert first["model"] == "test-model"
    assert first["frame"]["type"] == "start"
    # The UI hook and the plugin hook are independent dispatches.
    assert assistant_frames
    assert set(assistant_frames[0]) == {"frame"}
    assert [payload["frame"]["type"] for payload in frames] == [
        payload["frame"]["type"] for payload in assistant_frames
    ]
    assert {payload["frame"]["type"] for payload in frames} >= {"start", "chunk", "end"}


async def test_tool_call_is_a_session_record_not_a_bus_event(tmp_path: Path) -> None:
    """tool/call and model/selection are records; the bus refuses the bare names.

    DSH spells both as records and has no dispatch site for either, so deeppyro
    keeps them as deprecated constants only: the tool batch still records one
    tool/call per started call, and a plugin that subscribes raises instead of
    silently listening to a name nothing dispatches.
    """
    tool = FakeTool("echo", text="ok")
    hooks = HookService()
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, _provider, _sink, agent = make_agent(
        tmp_path, script, registry=FakeRegistry([tool]), services={"hooks": hooks}
    )

    assert await agent.send("go") == "done"

    calls = session.records_of("tool/call")
    assert len(calls) == 1
    assert calls[0].data["callId"] == "call_1"
    assert len(tool.calls) == 1

    for name in (TOOL_CALL, MODEL_SELECTION):
        with pytest.raises(ValueError, match="unknown hook"):
            hooks.on(name, lambda payload: None)


async def test_agent_error_emitted_once_for_a_provider_failure(tmp_path: Path) -> None:
    """A failed request publishes one agent/error with phase request."""
    hooks = HookService()
    errors: list[Any] = []
    hooks.on(AGENT_ERROR, errors.append)
    session, _provider, _sink, agent = make_agent(
        tmp_path, [ScriptedTurn(error=LlmError("provider boom"))], services={"hooks": hooks}
    )

    with pytest.raises(LlmError, match="provider boom"):
        await agent.send("hello")

    assert len(errors) == 1
    payload = errors[0]
    assert isinstance(payload, MappingProxyType)
    assert set(payload) == {"agent", "error", "phase"}
    assert payload["agent"] is agent
    assert payload["phase"] == "request"
    assert isinstance(payload["error"], LlmError)
    turns = session.records_of("turn/end")
    assert turns[-1].data["reason"]["kind"] == "error"


async def test_agent_error_phase_tool_when_tool_execution_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected tool-boundary failure publishes phase tool."""
    hooks = HookService()
    errors: list[Any] = []
    hooks.on(AGENT_ERROR, errors.append)
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[finish("stop")]),
    ]
    _session, _provider, _sink, agent = make_agent(
        tmp_path, script, registry=FakeRegistry([FakeTool("echo")]), services={"hooks": hooks}
    )

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("tool boom")

    monkeypatch.setattr(agent, "_run_tools", boom)

    with pytest.raises(RuntimeError, match="tool boom"):
        await agent.send("go")

    assert len(errors) == 1
    assert errors[0]["phase"] == "tool"
    assert isinstance(errors[0]["error"], RuntimeError)


async def test_agent_error_phase_turn_when_prompt_assembly_fails(tmp_path: Path) -> None:
    """A failure before the request phase publishes phase turn."""
    hooks = HookService()
    errors: list[Any] = []
    hooks.on(AGENT_ERROR, errors.append)

    def broken_prompt(tools: Any, context: Any) -> str:
        raise RuntimeError("prompt boom")

    _session, _provider, _sink, agent = make_agent(
        tmp_path,
        [ScriptedTurn(events=[finish("stop")])],
        services={"hooks": hooks},
        prompt_builder=broken_prompt,
    )

    with pytest.raises(RuntimeError, match="prompt boom"):
        await agent.send("hello")

    assert len(errors) == 1
    assert errors[0]["phase"] == "turn"
