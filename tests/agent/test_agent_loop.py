"""End-to-end tests for the agent turn/step loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.errors import LlmError, SessionError
from deeppyro.core.protocols import Finish, ReasoningDelta, TextDelta, ToolCallArgsDelta, ToolCallStart

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


def make_session(
    tmp_path: Path,
    registry: FakeRegistry,
    script: list[ScriptedTurn],
    *,
    options: AgentOptions | None = None,
) -> tuple[FakeSessionLog, ScriptedProvider, RecordingSink, AgentSession]:
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=registry,
        options=options or AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
    )
    return session, provider, sink, agent


async def test_simple_turn_records_and_events(tmp_path: Path) -> None:
    script = [ScriptedTurn(events=[TextDelta("Hello"), TextDelta(" world"), usage(), finish("stop")])]
    session, provider, sink, agent = make_session(tmp_path, FakeRegistry(), script)

    text = await agent.send("hi")

    assert text == "Hello world"
    assert session.types() == [
        "turn/start",
        "step/start",
        "system/message",
        "user/message",
        "request/header",
        "request/context",
        "assistant/message",
        "step/end",
        "turn/end",
    ]
    assert session.records_of("turn/start")[0].data == {"turn": 1}
    assert session.records_of("step/start")[0].data == {"turn": 1, "step": 1}
    assert session.records_of("turn/end")[0].data == {"turn": 1, "reason": {"kind": "completed"}}
    assert session.records_of("request/header")[0].data["reason"] == "initial"

    assistant = session.records_of("assistant/message")[0].data["message"]
    assert assistant["role"] == "assistant"
    assert assistant["source"]["kind"] == "model"
    assert assistant["source"]["model"] == "test-model"
    assert session.records_of("assistant/message")[0].surfaceOp == "append"
    assert session.records_of("user/message")[0].surfaceOp == "append"

    assert sink.types() == ["status", "text", "text", "usage", "final", "status"]
    assert [event["phase"] for event in sink.of_type("status")] == ["thinking", "done"]
    assert sink.of_type("final")[0]["text"] == "Hello world"
    assert sink.of_type("usage")[0] == {"type": "usage", "input": 10, "output": 5, "cached": 0}

    # Durable log first: every event is emitted after its record exists.
    assistant_seq = session.records_of("assistant/message")[0].seq
    usage_index = sink.types().index("usage")
    assert sink.record_counts[usage_index] >= assistant_seq
    turn_end_seq = session.records_of("turn/end")[0].seq
    final_index = sink.types().index("final")
    assert sink.record_counts[final_index] >= turn_end_seq

    assert [m.role for m in session.derive_messages()] == ["system", "user", "assistant"]
    assert session.derive_messages()[-1].text() == "Hello world"
    assert provider.requests[0].model == "test-model"
    assert provider.requests[0].session_id == session.session_id
    assert agent.busy is False


async def test_second_turn_reuses_system_prompt_and_header_is_series(tmp_path: Path) -> None:
    script = [
        ScriptedTurn(events=[TextDelta("one"), finish("stop")]),
        ScriptedTurn(events=[TextDelta("two"), finish("stop")]),
    ]
    session, _, sink, agent = make_session(tmp_path, FakeRegistry(), script)

    assert await agent.send("first") == "one"
    assert await agent.send("second") == "two"

    assert len(session.records_of("system/message")) == 1
    assert len(session.records_of("request/header")) == 2
    assert session.records_of("request/header")[1].data["reason"] == "series"
    assert len(session.records_of("request/context")) == 1
    assert [m.role for m in session.derive_messages()] == ["system", "user", "assistant", "user", "assistant"]
    # `session` is a frontend event; AgentSession never emits it (CLI does).
    assert sink.of_type("session") == []


async def test_tool_calls_run_and_are_recorded_before_results(tmp_path: Path) -> None:
    echo = FakeTool("echo", text="echo:{text}")
    registry = FakeRegistry([echo])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", '{"text": "a"}'),
                ToolCallStart("call_2", "echo"),
                ToolCallArgsDelta("call_2", '{"text": "b"}'),
                usage(),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, _provider, sink, agent = make_session(tmp_path, registry, script)

    assert await agent.send("use tools") == "done"

    types = session.types()
    assert types == [
        "turn/start",
        "step/start",
        "system/message",
        "user/message",
        "request/header",
        "request/context",
        "assistant/message",
        "tool/call",
        "tool/call",
        "tool/result",
        "tool/result",
        "step/end",
        "step/start",
        "assistant/message",
        "step/end",
        "turn/end",
    ]
    calls = session.records_of("tool/call")
    assert [call.data["callId"] for call in calls] == ["call_1", "call_2"]
    assert [call.data["name"] for call in calls] == ["echo", "echo"]
    results = session.records_of("tool/result")
    assert [result.data["message"]["toolCallId"] for result in results] == ["call_1", "call_2"]
    assert results[0].data["message"]["content"][0]["text"] == "echo:a"
    assistant_seq = session.records_of("assistant/message")[0].seq
    assert results[0].sourceEventSeqs == [assistant_seq]
    assert results[0].surfaceOp == "append"

    # Second step sees both tool results in order.
    assert [m.role for m in session.derive_messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert len(echo.calls) == 2
    assert echo.calls[0][1].call_id == "call_1"
    assert echo.calls[0][1].cwd == tmp_path

    tool_events = sink.of_type("tool_call")
    assert [event["callId"] for event in tool_events] == ["call_1", "call_2"]
    result_events = sink.of_type("tool_result")
    assert [event["callId"] for event in result_events] == ["call_1", "call_2"]
    # Tool events are emitted only after their records.
    first_tool_event = sink.types().index("tool_call")
    assert sink.record_counts[first_tool_event] >= session.records_of("tool/call")[0].seq


async def test_barrier_tool_runs_before_the_next_call(tmp_path: Path) -> None:
    shell = FakeTool("shell", text="shell-ok", parallel_safe=False)
    echo = FakeTool("echo", text="echo-ok")
    registry = FakeRegistry([shell, echo])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_a", "shell"),
                ToolCallArgsDelta("call_a", "{}"),
                ToolCallStart("call_b", "echo"),
                ToolCallArgsDelta("call_b", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("ok"), finish("stop")]),
    ]
    session, _, _, agent = make_session(tmp_path, registry, script)

    await agent.send("go")

    calls_and_results = [
        record.data.get("callId") or record.data.get("message", {}).get("toolCallId")
        for record in session.records
        if record.type in ("tool/call", "tool/result")
    ]
    assert calls_and_results == ["call_a", "call_a", "call_b", "call_b"]


async def test_parallel_safe_calls_are_grouped(tmp_path: Path) -> None:
    echo = FakeTool("echo", text="ok")
    registry = FakeRegistry([echo])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", "{}"),
                ToolCallStart("call_2", "echo"),
                ToolCallArgsDelta("call_2", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, _, _, agent = make_session(tmp_path, registry, script)

    await agent.send("go")

    types = session.types()
    first_call = types.index("tool/call")
    first_result = types.index("tool/result")
    assert types[first_call:first_result] == ["tool/call", "tool/call"]


async def test_failing_tool_is_reported_to_the_model(tmp_path: Path) -> None:
    broken = FakeTool("broken", error=RuntimeError("kaboom"))
    registry = FakeRegistry([broken])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "broken"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("recovered"), finish("stop")]),
    ]
    session, _, sink, agent = make_session(tmp_path, registry, script)

    assert await agent.send("go") == "recovered"

    result = session.records_of("tool/result")[0].data["message"]
    assert result["isError"] is True
    assert "kaboom" in result["content"][0]["text"]
    event = sink.of_type("tool_result")[0]
    assert event["isError"] is True
    assert "kaboom" in event["text"]


async def test_unknown_tool_is_reported_to_the_model(tmp_path: Path) -> None:
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "missing"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("ok"), finish("stop")]),
    ]
    session, _, _, agent = make_session(tmp_path, FakeRegistry(), script)

    assert await agent.send("go") == "ok"
    result = session.records_of("tool/result")[0].data["message"]
    assert result["isError"] is True
    assert "missing" in result["content"][0]["text"]


async def test_interrupt_preserves_partial_output_and_records_user_abort(tmp_path: Path) -> None:
    pause = asyncio.Event()
    script = [
        ScriptedTurn(
            events=[TextDelta("par"), TextDelta("tial"), Finish(kind="stop")],
            pause=pause,
            pause_after=1,
        )
    ]
    session, provider, sink, agent = make_session(tmp_path, FakeRegistry(), script)

    task = asyncio.create_task(agent.send("long"))
    await provider.started.wait()
    for _ in range(200):
        if sink.of_type("text"):
            break
        await asyncio.sleep(0.01)
    assert agent.busy is True
    await agent.interrupt()
    text = await task

    assert text == "par"
    assert agent.busy is False
    assert session.records_of("turn/end")[0].data["reason"] == {"kind": "aborted", "reason": {"kind": "user"}}
    assistant = session.records_of("assistant/message")[0].data["message"]
    assert assistant["content"][0]["text"] == "par"
    assert session.derive_messages()[-1].role == "assistant"


async def test_send_while_busy_raises_session_error(tmp_path: Path) -> None:
    pause = asyncio.Event()
    script = [ScriptedTurn(events=[TextDelta("x"), Finish(kind="stop")], pause=pause, pause_after=0)]
    _session, provider, _, agent = make_session(tmp_path, FakeRegistry(), script)

    task = asyncio.create_task(agent.send("first"))
    await provider.started.wait()
    with pytest.raises(SessionError):
        await agent.send("second")
    await agent.interrupt()
    await task


async def test_max_steps_stops_the_turn(tmp_path: Path) -> None:
    echo = FakeTool("echo", text="ok")
    registry = FakeRegistry([echo])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        )
    ]
    session, _, _, agent = make_session(tmp_path, registry, script, options=AgentOptions(max_steps=1))

    assert await agent.send("go") == ""
    assert len(session.records_of("step/start")) == 1
    assert session.records_of("turn/end")[0].data["reason"] == {"kind": "max-steps"}


async def test_provider_error_is_recorded_and_reraised(tmp_path: Path) -> None:
    script = [ScriptedTurn(events=[finish("error", {"message": "kaboom", "status": 503})])]
    session, _, sink, agent = make_session(tmp_path, FakeRegistry(), script)

    with pytest.raises(LlmError) as excinfo:
        await agent.send("go")

    assert "kaboom" in str(excinfo.value)
    types = session.types()
    assert "assistant/message" not in types
    assert "assistant/attempt" in types
    assert session.records_of("turn/end")[0].data["reason"]["kind"] == "error"
    assert session.records_of("turn/end")[0].data["reason"]["error"]["message"] == "kaboom"
    assert sink.of_type("error")[0]["message"] == "kaboom"


async def test_reasoning_deltas_are_recorded_as_parts(tmp_path: Path) -> None:
    script = [ScriptedTurn(events=[ReasoningDelta("think"), TextDelta("answer"), finish("stop")])]
    session, _, sink, agent = make_session(tmp_path, FakeRegistry(), script)

    assert await agent.send("go") == "answer"

    parts = session.records_of("assistant/message")[0].data["message"]["content"]
    assert [part["type"] for part in parts] == ["reasoning", "text"]
    thinking = sink.of_type("thinking")
    assert [event["text"] for event in thinking] == ["think"]


async def test_length_finish_closes_turn_as_max_tokens(tmp_path: Path) -> None:
    script = [ScriptedTurn(events=[TextDelta("cut"), finish("length")])]
    session, _, sink, agent = make_session(tmp_path, FakeRegistry(), script)

    assert await agent.send("go") == "cut"
    assert session.records_of("turn/end")[0].data["reason"] == {"kind": "max-tokens"}
    assert sink.of_type("final")[0]["text"] == "cut"


async def test_auto_compaction_runs_inside_the_open_turn(tmp_path: Path) -> None:
    from deeppyro.compact import Compactor

    from .fakes import ScriptedTurn, finish

    session = FakeSessionLog(cwd=tmp_path)
    session.append(
        "system/message",
        {
            "turn": 1,
            "step": 1,
            "message": {
                "role": "system",
                "content": [{"type": "text", "text": "You are a helpful coding agent."}],
                "id": "msg_system",
                "source": {"kind": "system-prompt"},
            },
        },
        surfaceOp="append",
    )
    for index in range(4):
        session.append(
            "user/message",
            {
                "role": "user",
                "content": [{"type": "text", "text": f"old-{index} " + "u" * 200}],
                "id": f"msg_user_{index}",
                "source": {"kind": "user"},
            },
            surfaceOp="append",
        )
        session.append(
            "assistant/message",
            {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"old-reply-{index} " + "a" * 200}],
                    "id": f"msg_assistant_{index}",
                    "source": {"kind": "model", "provider": "fake-provider", "model": "fake-model"},
                },
            },
            surfaceOp="append",
        )

    provider = ScriptedProvider(
        [
            ScriptedTurn(events=[TextDelta("## Current Work\n- compacted"), finish("stop")]),
            ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
        ]
    )
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        compactor=Compactor(
            provider=provider,
            model="test-model",
            keep_recent_tokens=120,
            context_window=32768,
            threshold=0.0,
        ),
    )

    assert await agent.send("next") == "done"

    types = session.types()
    start_index = types.index("compaction/start")
    assert types[start_index : start_index + 4] == [
        "compaction/start",
        "compaction/summary",
        "user/message",
        "compaction/end",
    ]
    new_assistant_seq = session.records_of("assistant/message")[-1].seq
    assert session.records_of("compaction/start")[0].seq < new_assistant_seq
    assert session.records_of("compaction/start")[0].data["turn"] == 1
    assert agent.last_compaction_error is None
    assert "old-0" not in "".join(message.text() for message in session.derive_messages())
    assert session.derive_messages()[-1].text() == "done"


async def test_auto_compaction_failure_does_not_fail_the_turn(tmp_path: Path) -> None:
    from deeppyro.compact import Compactor

    from .fakes import ScriptedTurn, finish

    session = FakeSessionLog(cwd=tmp_path)
    session.append(
        "system/message",
        {
            "turn": 1,
            "step": 1,
            "message": {
                "role": "system",
                "content": [{"type": "text", "text": "system"}],
                "id": "msg_system",
                "source": {"kind": "system-prompt"},
            },
        },
        surfaceOp="append",
    )
    for index in range(4):
        session.append(
            "user/message",
            {
                "role": "user",
                "content": [{"type": "text", "text": f"old-{index} " + "u" * 200}],
                "id": f"msg_user_{index}",
            },
            surfaceOp="append",
        )
        session.append(
            "assistant/message",
            {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "old reply " + "a" * 200}],
                    "id": f"msg_assistant_{index}",
                },
            },
            surfaceOp="append",
        )
    provider = ScriptedProvider(
        [
            ScriptedTurn(error=LlmError("summarizer down")),
            ScriptedTurn(events=[TextDelta("worked anyway"), finish("stop")]),
        ]
    )
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        compactor=Compactor(
            provider=provider,
            model="test-model",
            keep_recent_tokens=120,
            context_window=32768,
            threshold=0.0,
        ),
    )

    assert await agent.send("next") == "worked anyway"
    assert agent.last_compaction_error is not None
    assert "summarizer down" in str(agent.last_compaction_error)
    assert session.records_of("turn/end")[-1].data["reason"] == {"kind": "completed"}
