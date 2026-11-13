"""P0 loop semantics: inbox claims, hook pipelines, prompt projections, retries.

These tests drive AgentSession through the real hook service, the real prompt
projections and the real surface fold (FakeSessionLog stores real LogRecords),
and assert the durable record order around each P0 feature.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.agent.stream import expand_stream
from deeppyro.core.errors import LlmError
from deeppyro.core.protocols import Finish, TextDelta
from deeppyro.hooks import HookService
from deeppyro.hooks.types import AGENT_PRE_STEP, AGENT_REQUEST_ERROR

from .fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    FakeTool,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
    usage,
)


def make_agent(
    tmp_path: Path,
    script: list[ScriptedTurn],
    *,
    registry: FakeRegistry | None = None,
    options: AgentOptions | None = None,
    permission: FakePermission | None = None,
    services: dict[str, Any] | None = None,
) -> tuple[FakeSessionLog, ScriptedProvider, RecordingSink, AgentSession]:
    """Build a loop over in-memory fakes (no real Session, provider or tools)."""
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=registry if registry is not None else FakeRegistry(),
        options=options or AgentOptions(model="test-model"),
        sink=sink,
        permission=permission or FakePermission(),
        cwd=tmp_path,
        services=services,
    )
    return session, provider, sink, agent


async def _wait_for(predicate: Any, *, timeout: float = 5.0) -> None:
    """Poll a predicate from a driver task (tests only; 10 ms granularity)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached before timeout")


async def test_pre_step_hook_can_reject_the_turn_before_any_step(tmp_path: Path) -> None:
    """Rejecting in agent/pre-step closes the turn without starting a step."""
    hooks = HookService()
    seen: list[dict[str, Any]] = []

    def reject(payload: dict[str, Any], nxt: Any) -> dict[str, Any]:
        seen.append(payload)
        return {"kind": "reject", "reason": "blocked by policy"}

    hooks.on(AGENT_PRE_STEP, reject)
    session, provider, sink, agent = make_agent(tmp_path, [], services={"hooks": hooks})

    assert await agent.send("hello") == ""

    assert len(seen) == 1
    payload = seen[0]
    assert payload["turn"] == 1
    assert payload["step"] == 1
    assert [message.text() for message in payload["messages"]] == ["hello"]
    assert payload["modelSelection"] == {"provider": "fake-provider", "model": "test-model"}
    assert session.types() == [
        "agent/inbox/spliced",
        "turn/start",
        "agent/inbox/spliced",
        "turn/end",
    ]
    record = session.records_of("turn/end")[0]
    assert record.data == {
        "turn": 1,
        "reason": {"kind": "blocked", "reason": {"kind": "pre-step", "reason": "blocked by policy"}},
    }
    assert provider.calls == 0
    assert sink.of_type("final") == []
    assert [event["phase"] for event in sink.of_type("status")] == ["done"]
    assert agent.busy is False


async def test_steer_adds_a_step_to_the_running_turn(tmp_path: Path) -> None:
    """steer() enqueues a next-step message that becomes the following step."""
    pause = asyncio.Event()
    script = [
        ScriptedTurn(events=[TextDelta("a"), Finish(kind="stop")], pause=pause, pause_after=1),
        ScriptedTurn(events=[TextDelta("b"), Finish(kind="stop")]),
    ]
    session, provider, sink, agent = make_agent(tmp_path, script)

    task = asyncio.create_task(agent.send("go"))
    await provider.started.wait()
    await _wait_for(lambda: bool(sink.of_type("text")))
    assert agent.busy is True

    agent.steer("more")
    pause.set()

    assert await task == "b"
    assert len(session.records_of("turn/start")) == 1
    assert len(session.records_of("step/start")) == 2
    assert len(session.records_of("step/end")) == 2
    assert len(session.records_of("assistant/message")) == 2
    assert session.records_of("turn/end")[0].data["reason"] == {"kind": "completed"}
    user_messages = [
        message
        for message in session.derive_messages()
        if message.role == "user" and (message.source or {}).get("kind") == "user"
    ]
    assert [message.text() for message in user_messages] == ["go", "more"]
    assert sink.of_type("final")[-1]["text"] == "b"


async def test_followup_drains_as_a_second_turn_without_another_send(tmp_path: Path) -> None:
    """followup() wakes the driver, which drains the next-turn message."""
    pause = asyncio.Event()
    script = [
        ScriptedTurn(events=[TextDelta("x"), Finish(kind="stop")], pause=pause, pause_after=1),
        ScriptedTurn(events=[TextDelta("y"), Finish(kind="stop")]),
    ]
    session, provider, _, agent = make_agent(tmp_path, script)

    task = asyncio.create_task(agent.send("first"))
    await provider.started.wait()
    await _wait_for(lambda: bool(session.records_of("step/start")))
    assert agent.busy is True

    agent.followup("second")
    assert len(session.records_of("turn/start")) == 1
    pause.set()

    assert await task == "y"
    await agent.wait_for_idle()
    assert len(session.records_of("turn/start")) == 2
    assert len(session.records_of("turn/end")) == 2
    assert session.records_of("turn/end")[-1].data["reason"] == {"kind": "completed"}
    started_turns = [record.data["turn"] for record in session.records_of("turn/start")]
    assert started_turns == [1, 2]


async def test_inject_queues_a_next_step_message_without_waking_an_idle_driver(
    tmp_path: Path,
) -> None:
    """inject() never starts a driver; the queued step message rides the next claim."""
    script = [
        ScriptedTurn(events=[TextDelta("one"), Finish(kind="stop")]),
        ScriptedTurn(events=[TextDelta("two"), Finish(kind="stop")]),
    ]
    session, _provider, _sink, agent = make_agent(tmp_path, script)

    assert await agent.send("first") == "one"
    assert agent.busy is False
    records_before = len(session.records)

    agent.inject("queued")
    await asyncio.sleep(0.05)

    assert agent.busy is False
    assert agent.inbox.has_pending is True
    # The only durable effect is the inbox splice: no turn or step starts.
    assert [record.type for record in session.records[records_before:]] == ["agent/inbox/spliced"]
    assert len(session.records_of("turn/start")) == 1

    # The wakeup send claims the queued next-step message together with its own.
    assert await agent.send("second") == "two"
    assert agent.inbox.has_pending is False
    first_turn_end = session.records_of("turn/end")[0].seq
    second_turn_records = session.records[first_turn_end + 1 :]
    admitted = [
        record.data["content"][0]["text"]
        for record in second_turn_records
        if record.type == "user/message" and (record.data.get("source") or {}).get("kind") == "user"
    ]
    assert admitted == ["queued", "second"]


async def test_interrupt_clears_the_pending_inbox(tmp_path: Path) -> None:
    """interrupt() cancels queued work, so no follow-up turn starts afterwards."""
    pause = asyncio.Event()
    script = [
        ScriptedTurn(
            events=[TextDelta("par"), Finish(kind="stop")],
            pause=pause,
            pause_after=1,
        ),
        ScriptedTurn(events=[TextDelta("never"), Finish(kind="stop")]),
    ]
    session, provider, sink, agent = make_agent(tmp_path, script)

    task = asyncio.create_task(agent.send("long"))
    await provider.started.wait()
    await _wait_for(lambda: bool(sink.of_type("text")))
    agent.followup("queued")
    assert agent.inbox.has_pending is True

    await agent.interrupt()

    assert await task == "par"
    await agent.wait_for_idle()
    assert agent.inbox.has_pending is False
    assert len(session.records_of("turn/start")) == 1
    assert provider.calls == 1
    assert session.records_of("turn/end")[-1].data["reason"] == {
        "kind": "aborted",
        "reason": {"kind": "user"},
    }


async def test_request_error_hook_retries_the_same_step(tmp_path: Path) -> None:
    """A retry keeps one step: assistant/attempt -> llm/retry -> retry-started."""
    hooks = HookService()
    payloads: list[dict[str, Any]] = []

    def retry(payload: dict[str, Any], nxt: Any) -> dict[str, Any]:
        payloads.append(payload)
        return {"kind": "retry"}

    hooks.on(AGENT_REQUEST_ERROR, retry)
    script = [
        ScriptedTurn(error=LlmError("boom")),
        ScriptedTurn(events=[TextDelta("ok"), Finish(kind="stop")]),
    ]
    services = {
        "hooks": hooks,
        "retryPolicy": {"mode": "always", "initialDelayMs": 0, "maxDelayMs": 1000},
    }
    session, provider, sink, agent = make_agent(tmp_path, script, services=services)

    assert await agent.send("go") == "ok"

    assert provider.calls == 2
    types = session.types()
    assert types.count("step/start") == 1
    assert types.count("assistant/attempt") == 1
    assert types.count("assistant/message") == 1
    attempt_at = types.index("assistant/attempt")
    retry_at = types.index("llm/retry")
    started_at = types.index("llm/retry-started")
    message_at = types.index("assistant/message")
    assert attempt_at < retry_at < started_at < message_at
    retry_record = session.records_of("llm/retry")[0].data
    assert retry_record["retry"] == 1
    assert retry_record["mode"] == "always"
    assert retry_record["failure"]["message"] == "boom"
    assert payloads[0]["turn"] == 1 and payloads[0]["step"] == 1
    assert payloads[0]["provider"] == "fake-provider"
    assert session.records_of("turn/end")[0].data["reason"] == {"kind": "completed"}
    assert sink.of_type("error") == []


async def test_tool_change_writes_a_header_change_and_a_developer_message(tmp_path: Path) -> None:
    """Adding a tool rewrites the header and announces the addition in history."""
    echo = FakeTool("echo", text="echo")
    registry = FakeRegistry([echo])
    script = [
        ScriptedTurn(events=[TextDelta("one"), Finish(kind="stop")]),
        ScriptedTurn(events=[TextDelta("two"), Finish(kind="stop")]),
    ]
    session, provider, _sink, agent = make_agent(tmp_path, script, registry=registry)

    assert await agent.send("first") == "one"
    registry.register(FakeTool("second", text="second", description="second tool"))
    assert await agent.send("again") == "two"

    headers = session.records_of("request/header")
    assert [record.data["reason"] for record in headers] == ["initial", "change"]
    developers = session.records_of("developer/message")
    assert len(developers) == 1
    developer = developers[0]
    assert developer.data["headerSeq"] == headers[1].seq
    assert developer.data["message"]["content"] == [{"type": "tool-addition", "toolName": "second"}]
    assert developer.surfaceOp == "append"

    messages = session.derive_messages()
    assert [message.role for message in messages] == [
        "system",
        "user",
        "user",
        "assistant",
        "user",
        "developer",
        "assistant",
    ]
    assert messages[-2].content[0].toolName == "second"
    assert [schema.name for schema in provider.requests[1].tools] == ["echo", "second"]


async def test_changed_runtime_context_appends_a_new_snapshot(tmp_path: Path) -> None:
    """A changed runtime snapshot appends; the system prompt is replaced at a series start."""
    permission = FakePermission(preset="workspace-write")
    script = [
        ScriptedTurn(events=[TextDelta("one"), Finish(kind="stop")]),
        ScriptedTurn(events=[TextDelta("two"), Finish(kind="stop")]),
    ]
    session, _provider, _sink, agent = make_agent(tmp_path, script, permission=permission)

    assert await agent.send("hello") == "one"

    def runtime_records() -> list[Any]:
        return [
            record
            for record in session.records_of("user/message")
            if (record.data.get("source") or {}).get("kind") == "runtime-context"
        ]

    first_runtime = runtime_records()
    assert len(first_runtime) == 1
    assert "- Permission preset: workspace-write" in first_runtime[0].data["content"][0]["text"]

    permission.preset = "read-only"
    assert await agent.send("again") == "two"

    runtime = runtime_records()
    assert len(runtime) == 2
    assert "- Permission preset: workspace-write" in runtime[0].data["content"][0]["text"]
    assert "- Permission preset: read-only" in runtime[1].data["content"][0]["text"]
    assert runtime[1].surfaceOp == "append"

    systems = session.records_of("system/message")
    assert len(systems) == 2
    assert getattr(systems[1].surfaceOp, "op", None) == "replace"
    assert systems[1].surfaceOp.startSeq == systems[0].seq
    assert systems[1].surfaceOp.endSeq == systems[0].seq
    assert systems[1].sourceEventSeqs == [systems[0].seq]
    assert "- Permission preset: read-only" in systems[1].data["message"]["content"][0]["text"]

    derived_systems = [message for message in session.derive_messages() if message.role == "system"]
    assert len(derived_systems) == 1
    assert "- Permission preset: read-only" in derived_systems[0].text()


async def test_assistant_message_persists_the_timed_stream(tmp_path: Path) -> None:
    """assistant/message.stream is the replayable timing record of the provider stream."""
    script = [ScriptedTurn(events=[TextDelta("Hello"), TextDelta(" world"), usage(), finish("stop")])]
    session, _provider, _sink, agent = make_agent(tmp_path, script)

    assert await agent.send("hi") == "Hello world"

    data = session.records_of("assistant/message")[0].data
    stream = data["stream"]
    assert stream[0]["type"] == "text-chunks"
    assert stream[0]["index"] == 0
    assert stream[0]["texts"] == ["Hello", " world"]
    chunk_types = [record["chunk"]["type"] for record in stream if record["type"] == "chunk"]
    assert "usage" in chunk_types
    assert "finish" in chunk_types
    finish_record = next(
        record for record in stream if record["type"] == "chunk" and record["chunk"]["type"] == "finish"
    )
    assert finish_record["chunk"]["reason"] == {"kind": "stop"}
    assert data["usage"] == {
        "inputTokens": 10,
        "outputTokens": 5,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "totalTokens": 15,
    }

    timed = expand_stream(stream)
    text = "".join(entry["chunk"]["text"] for entry in timed if entry["chunk"]["type"] == "text-delta")
    assert text == "Hello world"


async def test_interrupt_marks_the_partial_assistant_message(tmp_path: Path) -> None:
    """An interrupted stream persists the partial text with interrupted=True."""
    pause = asyncio.Event()
    script = [
        ScriptedTurn(
            events=[TextDelta("par"), Finish(kind="stop")],
            pause=pause,
            pause_after=1,
        )
    ]
    session, provider, sink, agent = make_agent(tmp_path, script)

    task = asyncio.create_task(agent.send("long"))
    await provider.started.wait()
    await _wait_for(lambda: bool(sink.of_type("text")))
    await agent.interrupt()

    assert await task == "par"
    data = session.records_of("assistant/message")[0].data
    assert data["interrupted"] is True
    assert data["message"]["content"] == [{"type": "text", "text": "par"}]
    assert session.records_of("turn/end")[0].data["reason"] == {
        "kind": "aborted",
        "reason": {"kind": "user"},
    }
    assert sink.of_type("final") == []
    assert agent.busy is False
