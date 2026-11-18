"""Checkpoint store and crash/resume tests for the M2 native loop."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest

from deeppyro.agent.budget import BudgetLimits, BudgetUsage
from deeppyro.agent.checkpoint import (
    CHECKPOINT_VERSION,
    CheckpointError,
    CheckpointRecord,
    CheckpointStore,
)
from deeppyro.agent.loop import AgentLoop
from deeppyro.agent.run_state import RunState, RunStatus, ToolCallCheckpoint
from deeppyro.core.ids import (
    new_call_id,
    new_message_id,
    new_run_id,
    new_session_id,
    new_step_id,
    new_trace_id,
    new_turn_id,
)
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
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeCodec, NativeSession

IDEM = "idem:" + "a" * 64


def _state(*, status: RunStatus = RunStatus.RUNNING, session_id: str | None = None) -> RunState:
    return RunState(
        run_id=new_run_id(),
        trace_id=new_trace_id(),
        session_id=session_id or new_session_id(),
        status=status,
        last_seq=7,
        usage=BudgetUsage(input_tokens=11, output_tokens=5, cost_usd=0.5, tool_calls=2, wall_ms=120),
        budget=BudgetLimits(max_steps=3, max_tokens=100),
        started_at=1234,
        turn_id=new_turn_id(),
        step_id=new_step_id(),
        last_message_id=new_message_id(),
    )


def test_store_round_trip_and_missing_read(tmp_path) -> None:
    store = CheckpointStore(tmp_path / "checkpoints")
    assert store.read("session-x") is None
    state = _state()
    record = store.make_record(
        state,
        tool_calls=[
            ToolCallCheckpoint(call_id=new_call_id(), name="echo", state="pending", idempotency_key=IDEM)
        ],
        written_at=99,
    )
    path = store.write(record)
    assert path == store.path_for(state.session_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == CHECKPOINT_VERSION
    assert payload["session_id"] == state.session_id
    assert payload["written_at"] == 99
    assert payload["checkpoint"]["tool_calls"][0]["idempotency_key"] == IDEM
    restored = store.read(state.session_id)
    assert restored is not None
    assert restored.to_dict() == record.to_dict()
    assert store.delete(state.session_id) is True
    assert store.delete(state.session_id) is False
    assert store.read(state.session_id) is None


def test_store_rejects_path_traversal() -> None:
    store = CheckpointStore()
    for session_id in ("../evil", "a/b", "a\\b", "", "."):
        with pytest.raises(ValueError):
            store.path_for(session_id)


def test_store_reports_corrupt_files(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    state = _state(session_id="session-corrupt")
    store.write(store.make_record(state))
    path = store.path_for(state.session_id)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckpointError, match="unreadable"):
        store.read(state.session_id)
    path.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(CheckpointError, match="missing field"):
        store.read(state.session_id)


def test_record_parsing_is_strict(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    record = store.make_record(_state(), written_at=1)
    raw = record.to_dict()
    assert CheckpointRecord.from_dict(raw).to_dict() == raw
    broken_cases: list[dict[str, Any]] = [
        {**raw, "extra": 1},
        {**raw, "version": 2},
        {**raw, "session_id": ""},
        {**raw, "written_at": -1},
        {**raw, "checkpoint": {**raw["checkpoint"], "last_seq": 6}},
        {**raw, "checkpoint": {**raw["checkpoint"], "status": "paused"}},
        {k: v for k, v in raw.items() if k != "checkpoint"},
    ]
    for broken in broken_cases:
        with pytest.raises(CheckpointError):
            CheckpointRecord.from_dict(broken)


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
    description = "Echo the value"
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {"value": {"type": "integer"}}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult(text="tool-ok")


def _tool_call_events(provider_id: str = "call_provider_1") -> tuple[StreamEvent, ...]:
    return (
        ToolCallStart(call_id=provider_id, name="echo"),
        ToolCallArgsDelta(call_id=provider_id, delta='{"value": 1}'),
        UsageEvent(usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5)),
        Finish(stop_reason=StopReason.TOOL_CALLS),
    )


async def test_crash_then_resume_continues_run(tmp_path) -> None:
    session_path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    session = NativeSession.create(session_path, cwd=tmp_path, session_id=session_path.stem)
    store = CheckpointStore(tmp_path / "checkpoints")
    tool = _EchoTool()
    first_provider = _ScriptedProvider(
        [
            _tool_call_events(),
            RuntimeError("simulated process crash"),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=first_provider,
        model="fake-model",
        tools=[tool],
        cwd=tmp_path,
        checkpoint_store=store,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await loop.run("please echo")
    crashed_events = list(session.events)
    assert crashed_events[-1].type == EventKind.RUN_CHECKPOINT.value
    assert crashed_events[-2].type == EventKind.STEP_STARTED.value
    crashed_run_id = next(
        event.run_id for event in crashed_events if event.type == EventKind.RUN_STARTED.value
    )
    session.close()

    running = store.read(session_path.stem)
    assert running is not None
    assert running.state.status is RunStatus.RUNNING
    assert len(tool.calls) == 1

    reopened = NativeSession.open(session_path)
    resumed_provider = _ScriptedProvider(
        [
            (
                TextDelta(text="recovered"),
                UsageEvent(usage=Usage(input_tokens=2, output_tokens=1, total_tokens=3)),
                Finish(stop_reason=StopReason.STOP),
            ),
        ]
    )
    resumed_loop = AgentLoop(
        session=reopened,
        provider=resumed_provider,
        model="fake-model",
        tools=[tool],
        cwd=tmp_path,
        checkpoint_store=store,
    )
    result = await resumed_loop.run(resume=True)
    reopened.close()

    log = NativeCodec.read(session_path)
    events = list(log.events)
    view = fold(log.header, events)
    assert view.issues == ()
    assert result.status is RunStatus.COMPLETED
    assert result.final_text == "recovered"
    assert result.run_id != crashed_run_id
    assert len(tool.calls) == 1, "resume must not replay side-effectful tool calls"
    assert [event.type for event in events].count(EventKind.USER_MESSAGE.value) == 1
    assert [event.type for event in events].count(EventKind.RUN_STARTED.value) == 2
    assert [event.type for event in events].count(EventKind.RUN_FINISHED.value) == 1
    assert [event.type for event in events].count(EventKind.TOOL_CALL.value) == 1
    turn_end_reasons = [event.data["reason"] for event in events if event.type == EventKind.TURN_ENDED.value]
    assert turn_end_reasons[0] == {"kind": "aborted", "by": "disposed"}
    assert turn_end_reasons[1] == {"kind": "completed"}
    finished = next(event for event in events if event.type == EventKind.RUN_FINISHED.value)
    assert finished.data["status"] == "completed"
    assert result.usage.input_tokens == 5
    assert result.usage.output_tokens == 3
    final_record = store.read(session_path.stem)
    assert final_record is not None
    assert final_record.state.status is RunStatus.COMPLETED
    assert final_record.state.run_id == result.run_id
    assert final_record.state.usage.input_tokens == 5


async def test_resume_without_checkpoint_store_repairs_and_continues(tmp_path) -> None:
    session_path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    session = NativeSession.create(session_path, cwd=tmp_path, session_id=session_path.stem)
    provider = _ScriptedProvider([RuntimeError("boom")])
    loop = AgentLoop(session=session, provider=provider, model="fake-model", cwd=tmp_path)
    with pytest.raises(RuntimeError):
        await loop.run("hello")
    session.close()

    reopened = NativeSession.open(session_path)
    resumed = AgentLoop(
        session=reopened,
        provider=_ScriptedProvider([(TextDelta(text="done"), Finish(stop_reason=StopReason.STOP))]),
        model="fake-model",
        cwd=tmp_path,
    )
    result = await resumed.run(resume=True)
    reopened.close()
    assert result.final_text == "done"
    log = NativeCodec.read(session_path)
    assert fold(log.header, log.events).issues == ()
    assert next(event.data["reason"] for event in log.events if event.type == EventKind.TURN_ENDED.value) == {
        "kind": "aborted",
        "by": "disposed",
    }


async def test_resume_rejects_a_prompt(tmp_path) -> None:
    session_path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    session = NativeSession.create(session_path, cwd=tmp_path, session_id=session_path.stem)
    loop = AgentLoop(session=session, provider=_ScriptedProvider([]), model="fake-model", cwd=tmp_path)
    try:
        with pytest.raises(ValueError, match="resumed run"):
            await loop.run("hi", resume=True)
        with pytest.raises(ValueError, match="non-empty prompt"):
            await loop.run("   ")
    finally:
        session.close()
