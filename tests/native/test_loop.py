"""Native loop tests: events, tools, budget, cancellation, traces and hooks."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

from deeppyro.agent.budget import BudgetExceeded, BudgetKind, BudgetLimits
from deeppyro.agent.checkpoint import CheckpointStore
from deeppyro.agent.errors import RunCancelledError
from deeppyro.agent.events import (
    AgentHooks,
    AssistantMessageEvent,
    BudgetExceededEvent,
    CheckpointWrittenEvent,
    RunFinishedEvent,
    RunStarted,
    StepStarted,
    TextDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    TurnStarted,
)
from deeppyro.agent.loop import AgentLoop
from deeppyro.agent.run_state import RunStatus
from deeppyro.core.ids import new_session_id
from deeppyro.core.llm import (
    Finish,
    ModelRequest,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
    UsageEvent,
)
from deeppyro.core.messages import TOOL_CALL_ID_RE
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.observability.trace import (
    ModelCallRecord,
    RunRecord,
    ToolCallRecord,
    TraceStore,
    join_trace,
)
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeCodec, NativeSession

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IDEM_RE = re.compile(r"^idem:[0-9a-f]{64}$")


def _session(tmp_path) -> NativeSession:
    path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    return NativeSession.create(path, cwd=tmp_path, session_id=path.stem)


def _simple_response(text: str, *, usage: Usage | None = None) -> tuple[StreamEvent, ...]:
    return (
        TextDelta(text=text),
        UsageEvent(usage if usage is not None else Usage(input_tokens=3, output_tokens=2, total_tokens=5)),
        Finish(stop_reason=StopReason.STOP),
    )


def _tool_response(
    calls: list[tuple[str, str, str]], *, usage: Usage | None = None
) -> tuple[StreamEvent, ...]:
    events: list[StreamEvent] = []
    for index, (provider_id, name, arguments) in enumerate(calls):
        events.append(ToolCallStart(call_id=provider_id, name=name, index=index))
        events.append(ToolCallArgsDelta(call_id=provider_id, delta=arguments, index=index))
    events.append(
        UsageEvent(usage if usage is not None else Usage(input_tokens=4, output_tokens=1, total_tokens=5))
    )
    events.append(Finish(stop_reason=StopReason.TOOL_CALLS))
    return tuple(events)


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        for event in item:
            yield event

    async def aclose(self) -> None:
        return None


class _EchoTool:
    name = "echo"
    description = "Echo one value back"
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {"value": {"type": "integer"}}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult(text="tool-ok")


class _BoomTool:
    name = "boom"
    description = "Fail every call"
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise RuntimeError("tool exploded")


class _SlowTool:
    parallel_safe = True
    description = "Sleep briefly and log lifecycle order"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    def __init__(self, name: str, log: list[str], delay: float = 0.02) -> None:
        self.name = name
        self.log = log
        self.delay = delay

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.log.append(f"start:{self.name}")
        await asyncio.sleep(self.delay)
        self.log.append(f"end:{self.name}")
        return ToolResult(text=self.name)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.checkpoint_payloads: list[dict[str, Any]] = []

    def _add(self, event: Any) -> None:
        self.events.append(event)

    def run_started(self, event: RunStarted) -> None:
        self._add(event)

    def turn_started(self, event: TurnStarted) -> None:
        self._add(event)

    def step_started(self, event: StepStarted) -> None:
        self._add(event)

    def text_delta(self, event: TextDeltaEvent) -> None:
        self._add(event)

    def reasoning_delta(self, event: Any) -> None:
        self._add(event)

    def assistant_message(self, event: AssistantMessageEvent) -> None:
        self._add(event)

    def tool_call_started(self, event: ToolCallStartedEvent) -> None:
        self._add(event)

    def tool_call_finished(self, event: ToolCallFinishedEvent) -> None:
        self._add(event)

    def checkpoint_written(self, event: CheckpointWrittenEvent) -> None:
        self._add(event)
        with open(event.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.checkpoint_payloads.append(raw["checkpoint"])

    def budget_exceeded(self, event: BudgetExceededEvent) -> None:
        self._add(event)

    def run_finished(self, event: RunFinishedEvent) -> None:
        self._add(event)


def _types(session: NativeSession) -> list[str]:
    return [event.type for event in session.events]


async def test_happy_path_writes_native_session_and_result(tmp_path) -> None:
    session = _session(tmp_path)
    provider = _ScriptedProvider([_simple_response("hello")])
    loop = AgentLoop(session=session, provider=provider, model="fake-model", cwd=tmp_path)
    result = await loop.run("say hello")
    session.close()

    assert result.status is RunStatus.COMPLETED
    assert result.final_text == "hello"
    assert result.finish_reason == "stop"
    assert result.steps == 1
    assert result.tool_calls == 0
    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 2
    assert len(provider.requests) == 1
    assert result.run_id == loop.run_id
    assert result.trace_id == loop.trace_id

    assert _types(session) == [
        EventKind.RUN_STARTED.value,
        EventKind.TURN_STARTED.value,
        EventKind.USER_MESSAGE.value,
        EventKind.STEP_STARTED.value,
        EventKind.RUN_CHECKPOINT.value,
        EventKind.ASSISTANT_MESSAGE.value,
        EventKind.STEP_ENDED.value,
        EventKind.TURN_ENDED.value,
        EventKind.RUN_CHECKPOINT.value,
        EventKind.RUN_FINISHED.value,
    ]
    events = list(session.events)
    user = next(event for event in events if event.type == EventKind.USER_MESSAGE.value)
    assert user.data["message"]["role"] == "user"
    assert user.data["message"]["content"][0]["text"] == "say hello"
    assistant = next(event for event in events if event.type == EventKind.ASSISTANT_MESSAGE.value)
    assert assistant.data["finish_reason"] == "stop"
    assert assistant.data["message"]["source"] == {
        "kind": "model",
        "provider": "scripted",
        "model": "fake-model",
    }
    turn_ended = next(event for event in events if event.type == EventKind.TURN_ENDED.value)
    assert turn_ended.data["reason"] == {"kind": "completed"}
    finished = next(event for event in events if event.type == EventKind.RUN_FINISHED.value)
    assert finished.data == {"status": "completed", "reason": {"kind": "completed"}}

    log = NativeCodec.read(session.path)
    view = fold(log.header, log.events)
    assert view.issues == ()
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.last_message_id == result.last_message_id


async def test_tool_round_trip(tmp_path) -> None:
    session = _session(tmp_path)
    tool = _EchoTool()
    provider = _ScriptedProvider(
        [
            _tool_response([("call_provider_1", "echo", '{"value": 7}')]),
            _simple_response("done", usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3)),
        ]
    )
    loop = AgentLoop(session=session, provider=provider, model="fake-model", tools=[tool], cwd=tmp_path)
    result = await loop.run("echo seven")
    session.close()

    assert result.final_text == "done"
    assert result.steps == 2
    assert result.tool_calls == 1
    assert tool.calls == [{"value": 7}]
    events = list(session.events)
    calls = [event for event in events if event.type == EventKind.TOOL_CALL.value]
    assert len(calls) == 1
    call_id = calls[0].data["call_id"]
    assert TOOL_CALL_ID_RE.fullmatch(call_id)
    assert calls[0].data["name"] == "echo"
    assert json.loads(calls[0].data["arguments"]) == {"value": 7}
    results = [event for event in events if event.type == EventKind.TOOL_RESULT.value]
    assert len(results) == 1
    assert results[0].data["call_id"] == call_id
    assert results[0].data["message"]["role"] == "tool"
    assert results[0].data["message"]["tool_call_id"] == call_id
    assert results[0].data["message"]["content"][0]["text"] == "tool-ok"
    assert "error_code" not in results[0].data
    assistant = [event for event in events if event.type == EventKind.ASSISTANT_MESSAGE.value]
    assert len(assistant) == 2
    assert assistant[0].data["finish_reason"] == "tool_calls"
    assert fold(NativeCodec.read(session.path).header, session.events).issues == ()


async def test_tool_error_result_is_recorded_and_run_completes(tmp_path) -> None:
    session = _session(tmp_path)
    provider = _ScriptedProvider(
        [
            _tool_response([("call_provider_1", "boom", "{}")]),
            _simple_response("recovered"),
        ]
    )
    loop = AgentLoop(
        session=session, provider=provider, model="fake-model", tools=[_BoomTool()], cwd=tmp_path
    )
    result = await loop.run("break it")
    session.close()

    assert result.final_text == "recovered"
    results = [event for event in session.events if event.type == EventKind.TOOL_RESULT.value]
    assert len(results) == 1
    message = results[0].data["message"]
    assert message["is_error"] is True
    assert "tool exploded" in message["content"][0]["text"]
    assert fold(NativeCodec.read(session.path).header, session.events).issues == ()


async def test_parallel_safe_tools_run_in_one_batch(tmp_path) -> None:
    session = _session(tmp_path)
    log: list[str] = []
    tools: list[Any] = [_SlowTool("alpha", log), _SlowTool("beta", log)]
    provider = _ScriptedProvider(
        [
            _tool_response([("call_provider_1", "alpha", "{}"), ("call_provider_2", "beta", "{}")]),
            _simple_response("both done"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider, model="fake-model", tools=tools, cwd=tmp_path)
    result = await loop.run("run both")
    session.close()

    assert result.tool_calls == 2
    assert log[:2] == ["start:alpha", "start:beta"]
    assert set(log[2:]) == {"end:alpha", "end:beta"}
    calls = [event.data["name"] for event in session.events if event.type == EventKind.TOOL_CALL.value]
    assert calls == ["alpha", "beta"]
    results = [
        event.data["message"]["content"][0]["text"]
        for event in session.events
        if event.type == EventKind.TOOL_RESULT.value
    ]
    assert results == ["alpha", "beta"]


async def test_checkpoint_records_pending_tools_before_dispatch(tmp_path) -> None:
    session = _session(tmp_path)
    store = CheckpointStore(tmp_path / "checkpoints")
    recorder = _Recorder()
    provider = _ScriptedProvider(
        [_tool_response([("call_provider_1", "echo", '{"value": 1}')]), _simple_response("done")]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        model="fake-model",
        tools=[_EchoTool()],
        cwd=tmp_path,
        hooks=recorder,
        checkpoint_store=store,
    )
    await loop.run("work")
    session.close()

    assert len(recorder.checkpoint_payloads) >= 3
    seen_pending = False
    seen_completed = False
    for payload in recorder.checkpoint_payloads:
        states = [entry["state"] for entry in payload["tool_calls"]]
        if "pending" in states:
            seen_pending = True
        if "completed" in states:
            seen_completed = True
    assert seen_pending, "checkpoint before tool dispatch must mark calls pending"
    assert seen_completed, "checkpoint after tool execution must mark calls completed"
    final = store.read(session.header.id)
    assert final is not None
    assert final.state.status is RunStatus.COMPLETED


async def test_budget_max_steps_aborts_and_persists_reason(tmp_path) -> None:
    session = _session(tmp_path)
    store = CheckpointStore(tmp_path / "checkpoints")
    provider = _ScriptedProvider(
        [
            _tool_response([("call_provider_1", "echo", '{"value": 1}')]),
            _tool_response([("call_provider_2", "echo", '{"value": 2}')]),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        model="fake-model",
        tools=[_EchoTool()],
        cwd=tmp_path,
        budget=BudgetLimits(max_steps=1),
        checkpoint_store=store,
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        await loop.run("loop forever")
    session.close()

    assert excinfo.value.kind is BudgetKind.STEPS
    assert excinfo.value.limit == 1
    events = list(session.events)
    turn_ended = next(event for event in events if event.type == EventKind.TURN_ENDED.value)
    assert turn_ended.data["reason"] == {"kind": "max_steps", "limit": 1}
    finished = next(event for event in events if event.type == EventKind.RUN_FINISHED.value)
    assert finished.data["status"] == "failed"
    assert finished.data["reason"] == {"kind": "max_steps", "limit": 1}
    assert len(provider.requests) == 1
    record = store.read(session.header.id)
    assert record is not None
    assert record.state.status is RunStatus.FAILED


async def test_budget_max_tokens_aborts_before_assistant_message(tmp_path) -> None:
    session = _session(tmp_path)
    provider = _ScriptedProvider(
        [
            (
                TextDelta(text="long answer"),
                UsageEvent(Usage(input_tokens=1, output_tokens=50, total_tokens=51)),
                Finish(stop_reason=StopReason.STOP),
            ),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        model="fake-model",
        cwd=tmp_path,
        budget=BudgetLimits(max_tokens=10),
    )
    with pytest.raises(BudgetExceeded) as excinfo:
        await loop.run("say too much")
    session.close()

    assert excinfo.value.kind is BudgetKind.TOKENS
    types = _types(session)
    assert EventKind.ASSISTANT_MESSAGE.value not in types
    finished = next(event for event in session.events if event.type == EventKind.RUN_FINISHED.value)
    assert finished.data["status"] == "failed"
    assert finished.data["reason"] == {"kind": "max_tokens", "limit": 10}
    assert fold(NativeCodec.read(session.path).header, session.events).issues == ()


async def test_cooperative_cancel_stops_run_without_leaking_tasks(tmp_path) -> None:
    started = asyncio.Event()

    class _HangingProvider:
        name = "hanging"
        calls = 0

        async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
            _HangingProvider.calls += 1
            if _HangingProvider.calls == 1:
                yield ToolCallStart(call_id="call_provider_1", name="echo")
                yield ToolCallArgsDelta(call_id="call_provider_1", delta='{"value": 1}')
                yield Finish(stop_reason=StopReason.TOOL_CALLS)
                return
            started.set()
            await asyncio.Event().wait()
            yield TextDelta(text="never")

        async def aclose(self) -> None:
            return None

    session = _session(tmp_path)
    loop = AgentLoop(
        session=session,
        provider=_HangingProvider(),
        model="fake-model",
        tools=[_EchoTool()],
        cwd=tmp_path,
    )
    task = asyncio.create_task(loop.run("hang"))
    await asyncio.wait_for(started.wait(), timeout=5)
    loop.cancel()
    with pytest.raises(RunCancelledError):
        await asyncio.wait_for(task, timeout=5)
    session.close()
    await asyncio.sleep(0)

    events = list(session.events)
    turn_ended = next(event for event in events if event.type == EventKind.TURN_ENDED.value)
    assert turn_ended.data["reason"] == {"kind": "aborted", "by": "user"}
    finished = next(event for event in events if event.type == EventKind.RUN_FINISHED.value)
    assert finished.data["status"] == "cancelled"
    leaks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
    assert leaks == []
    assert fold(NativeCodec.read(session.path).header, session.events).issues == ()


async def test_outer_task_cancellation_propagates_after_persisting_abort(tmp_path) -> None:
    started = asyncio.Event()

    class _HangingProvider:
        name = "hanging"

        async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
            started.set()
            await asyncio.Event().wait()
            yield TextDelta(text="never")

        async def aclose(self) -> None:
            return None

    session = _session(tmp_path)
    loop = AgentLoop(session=session, provider=_HangingProvider(), model="fake-model", cwd=tmp_path)
    task = asyncio.create_task(loop.run("hang"))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session.close()
    await asyncio.sleep(0)

    finished = next(event for event in session.events if event.type == EventKind.RUN_FINISHED.value)
    assert finished.data["status"] == "cancelled"
    assert finished.data["reason"] == {"kind": "aborted", "by": "user"}
    leaks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()]
    assert leaks == []


async def test_trace_records_join_session_and_store_no_prompt_text(tmp_path) -> None:
    session = _session(tmp_path)
    store = TraceStore(tmp_path / "traces")
    provider = _ScriptedProvider(
        [
            _tool_response([("call_provider_1", "echo", '{"value": 7}')]),
            _simple_response("done"),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        model="fake-model",
        tools=[_EchoTool()],
        cwd=tmp_path,
        trace_store=store,
    )
    result = await loop.run("secret prompt text")
    session.close()

    log = store.read(result.run_id)
    assert isinstance(log.run, RunRecord)
    assert log.run.run_id == result.run_id
    assert log.run.trace_id == result.trace_id
    model_records = [record for record in log.records if isinstance(record, ModelCallRecord)]
    tool_records = [record for record in log.records if isinstance(record, ToolCallRecord)]
    assert len(model_records) == 2
    assert len(tool_records) == 1
    assert [record.finish_reason for record in model_records] == ["tool_calls", "stop"]
    for record in model_records:
        assert HASH_RE.fullmatch(record.prompt_hash)
        assert record.context_message_ids
        assert record.input_tokens > 0 or record.output_tokens > 0
    tool_record = tool_records[0]
    assert HASH_RE.fullmatch(tool_record.args_hash)
    assert tool_record.tool == "echo"
    assert tool_record.is_error is False
    assert IDEM_RE.fullmatch(tool_record.idempotency_key or "")
    assert TOOL_CALL_ID_RE.fullmatch(tool_record.call_id)
    joined = join_trace(log, session.events)
    assert len(joined.model_calls) == 2
    assert len(joined.tool_calls) == 1
    raw = store.path_for(result.run_id).read_text(encoding="utf-8")
    assert "secret prompt text" not in raw
    assert '{"value": 7}' not in raw


async def test_typed_hooks_receive_lifecycle_events(tmp_path) -> None:
    session = _session(tmp_path)
    recorder = _Recorder()
    provider = _ScriptedProvider([_simple_response("hi")])
    loop = AgentLoop(session=session, provider=provider, model="fake-model", cwd=tmp_path, hooks=recorder)
    await loop.run("hello")
    session.close()

    kinds = [type(event).__name__ for event in recorder.events]
    assert kinds[:3] == ["RunStarted", "TurnStarted", "StepStarted"]
    assert "TextDeltaEvent" in kinds
    assert kinds[-1] == "RunFinishedEvent"
    run_started = recorder.events[0]
    assert isinstance(run_started, RunStarted)
    assert run_started.session_id == session.header.id
    turn_started = recorder.events[1]
    assert isinstance(turn_started, TurnStarted)
    assert turn_started.prompt_message_id is not None
    finished = recorder.events[-1]
    assert isinstance(finished, RunFinishedEvent)
    assert finished.status == "completed"


async def test_run_guards_and_fresh_second_run(tmp_path) -> None:
    session = _session(tmp_path)
    provider = _ScriptedProvider([_simple_response("one"), _simple_response("two")])
    loop = AgentLoop(session=session, provider=provider, model="fake-model", cwd=tmp_path)
    first = await loop.run("first")
    second = await loop.run("second")
    assert first.run_id != second.run_id
    assert _types(session).count(EventKind.RUN_STARTED.value) == 2
    assert _types(session).count(EventKind.TURN_STARTED.value) == 2
    assert [
        message.role for message in fold(NativeCodec.read(session.path).header, session.events).messages
    ] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    with pytest.raises(ValueError, match="non-empty prompt"):
        await loop.run("")
    session.close()
    with pytest.raises(ValueError, match="closed"):
        await loop.run("after close")


def test_agent_hooks_protocol_is_implemented_by_recorder() -> None:
    recorder: AgentHooks = _Recorder()
    assert recorder is not None
