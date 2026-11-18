"""Native loop tests: per-turn usage reporting and trace export injection."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import pytest

from deeppyro.agent.errors import ProviderStreamError
from deeppyro.agent.loop import AgentLoop
from deeppyro.agent.run_state import RunStatus
from deeppyro.core.ids import new_session_id
from deeppyro.core.protocols import Finish, LlmRequest, StreamEvent, TextDelta, Usage
from deeppyro.observability.protocols import TurnUsage
from deeppyro.observability.trace import ModelCallRecord, RunRecord, TraceStore
from deeppyro.session.schema import NativeSession

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _session(tmp_path: Any) -> NativeSession:
    path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    return NativeSession.create(path, cwd=tmp_path, session_id=path.stem)


def _response(text: str, *, usage: Usage | None = None) -> tuple[StreamEvent, ...]:
    return (
        TextDelta(text=text),
        Finish(kind="stop"),
        usage if usage is not None else Usage(input_tokens=3, output_tokens=2, total_tokens=5),
    )


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[LlmRequest] = []

    async def stream(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        for event in item:
            yield event

    async def aclose(self) -> None:
        return None


class RecordingUsageSink:
    def __init__(self) -> None:
        self.records: list[TurnUsage] = []

    def record_turn(self, usage: TurnUsage, /) -> None:
        self.records.append(usage)


class RecordingTraceExport:
    def __init__(self) -> None:
        self.records: list[RunRecord | ModelCallRecord] = []

    def export_trace_record(self, record: RunRecord | ModelCallRecord, /) -> None:
        self.records.append(record)


async def test_native_turn_reports_usage_and_forwards_trace_records(tmp_path: Any) -> None:
    session = _session(tmp_path)
    usage_sink = RecordingUsageSink()
    export = RecordingTraceExport()
    provider = _ScriptedProvider(
        [
            _response(
                "hello", usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5, cached_input_tokens=1)
            )
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        model="fake-model",
        cwd=tmp_path,
        services={"usage": usage_sink, "trace_export": export},
    )
    try:
        result = await loop.run("hi")

        assert result.status is RunStatus.COMPLETED
        assert result.cached_input_tokens == 1
        assert len(usage_sink.records) == 1
        usage = usage_sink.records[0]
        assert usage.provider == "scripted"
        assert usage.model == "fake-model"
        assert usage.session_id == session.header.id
        assert usage.turn == 1
        assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (3, 2, 1)
        assert usage.stop_reason == "stop"
        assert usage.retries == 0
        assert usage.latency_ms >= 0
        assert usage.ts > 0
        assert usage.source == "native"
        assert usage.run_id == result.run_id
        assert usage.trace_id == result.trace_id

        assert [type(record) for record in export.records] == [RunRecord, ModelCallRecord]
        run, call = export.records
        assert run.run_id == result.run_id
        assert run.session_id == session.header.id
        assert call.trace_id == result.trace_id
        assert (call.input_tokens, call.output_tokens) == (3, 2)
        assert call.cached_input_tokens == 1
        assert call.finish_reason == "stop"
        assert call.retry_count == 0
        assert HASH_RE.match(call.prompt_hash)
    finally:
        await loop.close() if hasattr(loop, "close") else None
        session.close()


async def test_native_error_run_still_reports_a_turn(tmp_path: Any) -> None:
    session = _session(tmp_path)
    usage_sink = RecordingUsageSink()
    provider = _ScriptedProvider([[Finish(kind="error", failure={"message": "boom"})]])
    loop = AgentLoop(
        session=session,
        provider=provider,
        model="fake-model",
        cwd=tmp_path,
        services={"usage": usage_sink},
    )
    try:
        with pytest.raises(ProviderStreamError):
            await loop.run("hi")
        assert len(usage_sink.records) == 1
        usage = usage_sink.records[0]
        assert usage.stop_reason == "error"
        assert (usage.input_tokens, usage.output_tokens) == (0, 0)
        assert usage.turn == 1
    finally:
        session.close()


async def test_usage_sink_failure_does_not_break_the_run(tmp_path: Any) -> None:
    session = _session(tmp_path)

    class BrokenSink:
        def record_turn(self, usage: TurnUsage, /) -> None:
            raise RuntimeError("ledger is down")

    loop = AgentLoop(
        session=session,
        provider=_ScriptedProvider([_response("hello")]),
        model="fake-model",
        cwd=tmp_path,
        services={"usage": BrokenSink()},
    )
    try:
        result = await loop.run("hi")
        assert result.status is RunStatus.COMPLETED
        assert result.final_text == "hello"
    finally:
        session.close()


async def test_trace_export_failure_does_not_break_the_run(tmp_path: Any) -> None:
    session = _session(tmp_path)

    class BrokenExport:
        def export_trace_record(self, record: RunRecord | ModelCallRecord, /) -> None:
            raise RuntimeError("collector is down")

    loop = AgentLoop(
        session=session,
        provider=_ScriptedProvider([_response("hello")]),
        model="fake-model",
        cwd=tmp_path,
        services={"trace_export": BrokenExport()},
    )
    try:
        result = await loop.run("hi")
        assert result.status is RunStatus.COMPLETED
    finally:
        session.close()


async def test_concurrent_loops_report_their_own_turns(tmp_path: Any) -> None:
    sessions = [_session(tmp_path), _session(tmp_path)]
    sinks = [RecordingUsageSink(), RecordingUsageSink()]
    loops = [
        AgentLoop(
            session=session,
            provider=_ScriptedProvider([_response(f"hello-{index}")]),
            model="fake-model",
            cwd=tmp_path,
            services={"usage": sinks[index]},
        )
        for index, session in enumerate(sessions)
    ]
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(loop.run("hi")) for loop in loops]
        results = [task.result() for task in tasks]
        assert all(result.status is RunStatus.COMPLETED for result in results)
        for index, sink in enumerate(sinks):
            assert len(sink.records) == 1
            assert sink.records[0].session_id == sessions[index].header.id
    finally:
        for session in sessions:
            session.close()


async def test_native_loop_writes_local_traces_and_reports_usage(tmp_path: Any) -> None:
    session = _session(tmp_path)
    store = TraceStore(tmp_path / "traces")
    usage_sink = RecordingUsageSink()
    loop = AgentLoop(
        session=session,
        provider=_ScriptedProvider([_response("hello")]),
        model="fake-model",
        cwd=tmp_path,
        trace_store=store,
        services={"usage": usage_sink},
    )
    try:
        result = await loop.run("hi")
        log = store.read(result.run_id)
        assert log.run.run_id == result.run_id
        assert [record.type for record in log.records] == ["model_call"]
        assert len(usage_sink.records) == 1
    finally:
        session.close()
