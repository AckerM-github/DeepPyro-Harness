"""Tests for the reusable native agent host (KI-001 phase-1 bridge)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.events import (
    AssistantMessageEvent,
    NullAgentHooks,
    RunFinishedEvent,
    RunStarted,
    StepStarted,
    TextDeltaEvent,
    TurnStarted,
)
from deeppyro.agent.native_host import (
    RECORD_KIND_MAP,
    NativeAgentHost,
    NativeHeaderView,
    NativeSessionHandle,
    UnsupportedRecordTypeError,
)
from deeppyro.core.ids import new_run_id, new_session_id, new_step_id, new_trace_id, new_turn_id
from deeppyro.core.llm import (
    Finish,
    ModelRequest,
    StopReason,
    StreamEvent,
    TextDelta,
    Usage,
    UsageEvent,
)
from deeppyro.core.messages import PluginSource
from deeppyro.observability.protocols import TurnUsage
from deeppyro.observability.trace import ModelCallRecord, RunRecord
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeSession

_STOP_REASONS = {
    "stop": StopReason.STOP,
    "tool_calls": StopReason.TOOL_CALLS,
    "length": StopReason.LENGTH,
}


@dataclass
class ScriptedTurn:
    """One scripted native provider stream: events plus optional mid-stream pause."""

    events: list[StreamEvent]
    pause: asyncio.Event | None = None
    pause_after: int = 1


class ScriptedProvider:
    """Deterministic native LlmProvider: one ScriptedTurn per model call."""

    name = "scripted"

    def __init__(self, turns: list[ScriptedTurn]) -> None:
        self._turns = list(turns)
        self.requests: list[ModelRequest] = []
        self.started = asyncio.Event()

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        turn = self._turns.pop(0)
        self.started.set()
        for index, event in enumerate(turn.events):
            if isinstance(event, BaseException):
                raise event
            yield event
            if turn.pause is not None and index + 1 >= turn.pause_after:
                await turn.pause.wait()

    async def aclose(self) -> None:
        return None


def finish(kind: str) -> Finish:
    return Finish(stop_reason=_STOP_REASONS[kind])


def usage(input_tokens: int, output_tokens: int, *, cached: int | None = None) -> UsageEvent:
    return UsageEvent(
        Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cached_input_tokens=cached,
        )
    )


class _HookRecorder(NullAgentHooks):
    """Capture every typed hook the loop emits, in emission order."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.deltas: list[str] = []

    def run_started(self, event: RunStarted) -> None:
        self.events.append(event)

    def turn_started(self, event: TurnStarted) -> None:
        self.events.append(event)

    def step_started(self, event: StepStarted) -> None:
        self.events.append(event)

    def text_delta(self, event: TextDeltaEvent) -> None:
        self.events.append(event)
        self.deltas.append(event.text)

    def assistant_message(self, event: AssistantMessageEvent) -> None:
        self.events.append(event)

    def run_finished(self, event: RunFinishedEvent) -> None:
        self.events.append(event)


class _RecordingUsageSink:
    """UsageSink double: collects one TurnUsage per finished native turn."""

    def __init__(self) -> None:
        self.records: list[TurnUsage] = []

    def record_turn(self, usage: TurnUsage, /) -> object:
        self.records.append(usage)
        return usage


class _RecordingTraceExport:
    """TraceExportSink double: collects the records the loop already exported."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def export_trace_record(self, record: Any, /) -> None:
        self.records.append(record)


def _session(tmp_path: Path) -> NativeSession:
    path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    return NativeSession.create(path, cwd=tmp_path, session_id=path.stem)


def _host(tmp_path: Path, provider: ScriptedProvider, **kwargs: Any) -> NativeAgentHost:
    return NativeAgentHost(
        session=_session(tmp_path),
        provider=provider,
        model="fake-model",
        cwd=tmp_path,
        **kwargs,
    )


async def test_two_turns_reuse_one_session_and_context(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            ScriptedTurn(events=[TextDelta("first answer"), finish("stop"), usage(11, 4, cached=2)]),
            ScriptedTurn(events=[TextDelta("second answer"), finish("stop"), usage(7, 3, cached=1)]),
        ]
    )
    host = _host(tmp_path, provider)
    try:
        first = await host.send("first question")
        second = await host.send("second question")

        assert first.final_text == "first answer"
        assert second.final_text == "second answer"
        assert first.run_id != second.run_id
        assert first.session_id == second.session_id == host.session_id
        assert host.busy is False

        # The second run saw the first turn because the loop folds the session.
        assert len(provider.requests) == 2
        second_request = [message.text() for message in provider.requests[1].messages]
        assert "first question" in second_request
        assert "first answer" in second_request
        assert second_request[-1] == "second question"
        assert [(message.role, message.text()) for message in host.derive_messages()] == [
            ("user", "first question"),
            ("assistant", "first answer"),
            ("user", "second question"),
            ("assistant", "second answer"),
        ]
        view = fold(host.native_session.header, host.native_session.events)
        assert view.issues == ()
        assert host.usage() == {"input": 18, "output": 7, "cached": 3}
        assert host.context_tokens() == 7
    finally:
        await host.aclose()


async def test_streaming_hooks_follow_the_loop_lifecycle(tmp_path: Path) -> None:
    recorder = _HookRecorder()
    provider = ScriptedProvider(
        [ScriptedTurn(events=[TextDelta("he"), TextDelta("llo"), finish("stop"), usage(3, 2)])]
    )
    host = _host(tmp_path, provider, hooks=recorder)
    try:
        result = await host.send("hi")
    finally:
        await host.aclose()

    kinds = [type(event).__name__ for event in recorder.events]
    assert kinds[:3] == ["RunStarted", "TurnStarted", "StepStarted"]
    assert kinds[-1] == "RunFinishedEvent"
    assert "TextDeltaEvent" in kinds
    assert "".join(recorder.deltas) == "hello"
    assert any(isinstance(event, AssistantMessageEvent) for event in recorder.events)
    run_started = recorder.events[0]
    assert isinstance(run_started, RunStarted)
    assert run_started.session_id == result.session_id
    turn_started = recorder.events[1]
    assert isinstance(turn_started, TurnStarted)
    assert turn_started.prompt_message_id is not None
    finished = recorder.events[-1]
    assert isinstance(finished, RunFinishedEvent)
    assert finished.status == "completed"
    assert result.final_text == "hello"


async def test_interrupt_cancels_in_flight_run_and_next_send_still_works(tmp_path: Path) -> None:
    pause = asyncio.Event()
    provider = ScriptedProvider(
        [
            ScriptedTurn(
                events=[TextDelta("par"), TextDelta("tial"), finish("stop")],
                pause=pause,
                pause_after=1,
            ),
            ScriptedTurn(events=[TextDelta("recovered"), finish("stop"), usage(2, 1)]),
        ]
    )
    host = _host(tmp_path, provider)
    try:
        task = asyncio.create_task(host.send("long question"))
        await asyncio.wait_for(provider.started.wait(), timeout=5)
        assert host.busy is True
        assert await host.interrupt() is True
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.busy is False
        assert await host.interrupt() is False

        events = host.native_session.events
        finished = [event for event in events if event.type == EventKind.RUN_FINISHED.value][-1]
        assert finished.data == {"status": "cancelled", "reason": {"kind": "aborted", "by": "user"}}
        turn_ended = [event for event in events if event.type == EventKind.TURN_ENDED.value][-1]
        assert turn_ended.data["reason"] == {"kind": "aborted", "by": "user"}

        # The session stays usable: a fresh loop run continues on the same log.
        result = await host.send("try again")
        assert result.final_text == "recovered"
        assert len(provider.requests) == 2
    finally:
        await host.aclose()


async def test_usage_and_context_chain_to_caller_sinks(tmp_path: Path) -> None:
    usage_sink = _RecordingUsageSink()
    trace_export = _RecordingTraceExport()
    provider = ScriptedProvider(
        [ScriptedTurn(events=[TextDelta("answer"), finish("stop"), usage(11, 4, cached=2)])]
    )
    host = _host(
        tmp_path,
        provider,
        services={"usage": usage_sink, "trace_export": trace_export},
    )
    try:
        await host.send("hello")
        assert host.usage() == {"input": 11, "output": 4, "cached": 2}
        assert host.handle.usage() == {"input": 11, "output": 4, "cached": 2}
        assert host.context_tokens() == 11
        assert len(usage_sink.records) == 1
        assert usage_sink.records[0].input_tokens == 11
        assert usage_sink.records[0].source == "native"
        assert usage_sink.records[0].session_id == host.session_id
        assert [type(record) for record in trace_export.records] == [RunRecord, ModelCallRecord]
    finally:
        await host.aclose()


async def test_inject_queues_notice_for_the_next_run(tmp_path: Path) -> None:
    provider = ScriptedProvider([ScriptedTurn(events=[TextDelta("answer"), finish("stop"), usage(5, 2)])])
    host = _host(tmp_path, provider)
    try:
        handle = host.handle
        assert handle.inject("cwd changed", source={"kind": "working-directory"}) is True
        assert handle.injected_count == 1
        assert handle.dropped_count == 0
        assert [notice.text for notice in handle.pending_notices] == ["cwd changed"]

        assert (await host.send("question")).final_text == "answer"
        assert handle.pending_notices == ()

        messages = host.derive_messages()
        assert [(message.role, message.text()) for message in messages] == [
            ("user", "cwd changed"),
            ("user", "question"),
            ("assistant", "answer"),
        ]
        notice = messages[0]
        assert isinstance(notice.source, PluginSource)
        assert notice.source.plugin == "working-directory"
        request = [message.text() for message in provider.requests[0].messages]
        assert request == ["cwd changed", "question"]
    finally:
        await host.aclose()


async def test_inject_queue_is_bounded_and_counts_drops(tmp_path: Path) -> None:
    provider = ScriptedProvider([ScriptedTurn(events=[TextDelta("ok"), finish("stop"), usage(2, 1)])])
    host = _host(tmp_path, provider, notice_limit=1)
    try:
        handle = host.handle
        assert handle.inject("first") is True
        assert handle.inject("second") is False
        assert handle.injected_count == 1
        assert handle.dropped_count == 1
        assert [notice.text for notice in handle.pending_notices] == ["first"]

        await host.send("go")
        user_texts = [message.text() for message in host.derive_messages() if message.role == "user"]
        assert user_texts == ["first", "go"]
    finally:
        await host.aclose()


async def test_append_maps_known_record_types_and_rejects_unknown(tmp_path: Path) -> None:
    session = _session(tmp_path)
    handle = NativeSessionHandle(session)
    try:
        assert set(RECORD_KIND_MAP) == {
            "agent-preset/selected",
            "approval/policy",
            "model/selection",
            "sandbox/mode",
            "session/title",
            "todo/write",
            "working-directory/change",
        }
        before = session.last_seq
        with pytest.raises(UnsupportedRecordTypeError) as excinfo:
            handle.append("team/message/queued", {"data": {}})
        assert "team/message/queued" in str(excinfo.value)
        assert session.last_seq == before

        assert handle.append("model/selection", {"provider": "p", "model": "m"}) == before + 1
        assert session.events[-1].type == EventKind.MODEL_SELECTED.value
        assert session.events[-1].data == {"provider": "p", "model": "m"}

        assert handle.append("agent-preset/selected", {"preset": "standard", "mode": "default"}) == (
            before + 2
        )
        assert session.events[-1].type == EventKind.PRESET_SELECTED.value

        assert handle.append("session/title", {"title": "Hello", "source": {"kind": "llm"}}) == (before + 3)
        assert session.events[-1].data == {"title": "Hello", "source": "llm"}
        view = fold(session.header, session.events)
        assert view.state.title == "Hello"
        assert view.state.title_source == "llm"

        handle.append("sandbox/mode", {"mode": "workspace-write"})
        assert session.events[-1].type == EventKind.SANDBOX_MODE.value
        handle.append("approval/policy", {"policy": "never"})
        assert session.events[-1].type == EventKind.PERMISSION_POLICY.value
        handle.append("working-directory/change", {"cwd": str(tmp_path)})
        assert session.events[-1].type == EventKind.WORKSPACE_CWD_CHANGED.value
        handle.append("todo/write", {"todos": [{"content": "ship it", "status": "pending"}]})
        assert session.events[-1].type == EventKind.TODO_WRITTEN.value
    finally:
        handle.aclose()


async def test_close_releases_the_session_lock_and_stops_writes(tmp_path: Path) -> None:
    provider = ScriptedProvider([ScriptedTurn(events=[TextDelta("ok"), finish("stop"), usage(2, 1)])])
    session = _session(tmp_path)
    host = NativeAgentHost(session=session, provider=provider, model="fake-model", cwd=tmp_path)
    assert (await host.send("hi")).final_text == "ok"
    with pytest.raises(SessionLockedError):
        NativeSession.open(session.path)

    await host.aclose()
    await host.aclose()  # idempotent
    assert host.closed is True

    reopened = NativeSession.open(session.path)
    try:
        assert reopened.header.id == session.header.id
        assert [event.type for event in reopened.events] == [event.type for event in session.events]
    finally:
        reopened.close()

    with pytest.raises(ValueError, match="closed"):
        await host.send("after close")
    with pytest.raises(ValueError, match="closed"):
        host.handle.append("model/selection", {"provider": "p", "model": "m"})


async def test_handle_exposes_v4_shaped_read_surface(tmp_path: Path) -> None:
    session = _session(tmp_path)
    handle = NativeSessionHandle(session)
    try:
        header = handle.header
        assert isinstance(header, NativeHeaderView)
        assert handle.header is header  # the TUI asserts header identity is stable
        assert handle.session_id == session.header.id
        assert handle.native_session is session
        assert header.id == session.header.id
        assert header.created_at == session.header.created_at
        assert header.createdAt == session.header.created_at
        assert header.cwd == session.header.cwd
        assert header.preset == "standard"
        assert header.agentPreset == "standard"
        assert header.isSeeded is False
        assert header.delegationDepth == 0
        assert header.parentSession is None
        assert header.origin == "user"
        assert header.model is None
        assert header.system is None
        assert handle.derive_messages() == []
        assert handle.messages() == []
        assert handle.pending_notices == ()
    finally:
        handle.aclose()


class _StaticPrompt:
    """PromptSource double: returns its text and records the offered tool names."""

    def __init__(self, text: str = "SYSTEM V1") -> None:
        self.text = text
        self.tools: list[str] = []
        self.sessions: list[str] = []

    def render(self, session: Any, tools: Any) -> str:
        self.sessions.append(session.id)
        self.tools = [tool.name for tool in tools]
        return self.text


async def test_system_prompt_is_persisted_refreshed_and_last_wins(tmp_path: Path) -> None:
    prompt = _StaticPrompt()
    provider = ScriptedProvider([ScriptedTurn(events=[TextDelta("ok"), finish("stop"), usage(2, 1)])])
    host = _host(tmp_path, provider)
    try:
        assert host.attach_prompt(prompt) is True
        view = fold(host.native_session.header, host.native_session.events)
        assert view.system is not None
        assert view.system.text() == "SYSTEM V1"
        assert prompt.sessions == [host.native_session.header.id]

        def system_events() -> list[Any]:
            return [
                event for event in host.native_session.events if event.type == EventKind.SYSTEM_MESSAGE.value
            ]

        assert len(system_events()) == 1
        assert host.refresh_prompt() is False
        prompt.text = "SYSTEM V2"
        assert host.refresh_prompt() is True
        assert len(system_events()) == 2
        folded = fold(host.native_session.header, host.native_session.events)
        assert folded.system is not None
        assert folded.system.text() == "SYSTEM V2"

        await host.send("hi")
        assert len(system_events()) == 2
        assert prompt.tools == []
    finally:
        await host.aclose()


async def test_deliver_appends_when_idle_and_queues_when_busy(tmp_path: Path) -> None:
    paused = asyncio.Event()
    provider = ScriptedProvider(
        [
            ScriptedTurn(events=[TextDelta("partial")], pause=paused, pause_after=1),
            ScriptedTurn(events=[TextDelta("done"), finish("stop"), usage(2, 1)]),
        ]
    )
    host = _host(tmp_path, provider)
    try:
        host.deliver("idle text")
        messages = host.derive_messages()
        assert messages[-1].role == "user"
        assert messages[-1].text() == "idle text"
        assert host.turn_count() == 0

        pending: list[int] = []
        host.on_pending(lambda: pending.append(1))
        task = asyncio.create_task(host.send("first prompt"))
        await provider.started.wait()
        assert host.deliver("queued text", source=PluginSource(plugin="scheduler")) is True
        notices = host.handle.pending_notices
        assert len(notices) == 1
        assert notices[0].text == "queued text"
        assert pending == [1]
        assert host.busy is True

        assert await host.interrupt() is True
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host.busy is False

        await host.send("second prompt")
        user_texts = [message.text() for message in host.derive_messages() if message.role == "user"]
        assert "queued text" in user_texts
        assert host.turn_count() == 2
        assert host.last_assistant_text() == "done"
    finally:
        await host.aclose()


async def test_wait_for_idle_waits_for_the_current_run(tmp_path: Path) -> None:
    paused = asyncio.Event()
    provider = ScriptedProvider([ScriptedTurn(events=[TextDelta("x")], pause=paused, pause_after=1)])
    host = _host(tmp_path, provider)
    try:
        await host.wait_for_idle()
        task = asyncio.create_task(host.send("hi"))
        await provider.started.wait()
        waiter = asyncio.create_task(host.wait_for_idle())
        await asyncio.sleep(0)
        assert waiter.done() is False
        assert await host.interrupt() is True
        await waiter
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await host.aclose()


async def test_restore_closes_an_interrupted_open_turn(tmp_path: Path) -> None:
    session = _session(tmp_path)
    run_id = new_run_id()
    trace_id = new_trace_id()
    turn_id = new_turn_id()
    step_id = new_step_id()
    session.append(EventKind.TURN_STARTED, {}, run_id=run_id, turn_id=turn_id, trace_id=trace_id)
    session.append(
        EventKind.STEP_STARTED,
        {},
        run_id=run_id,
        turn_id=turn_id,
        step_id=step_id,
        trace_id=trace_id,
    )
    host = NativeAgentHost(
        session=session,
        provider=ScriptedProvider([]),
        model="fake-model",
        cwd=tmp_path,
    )
    try:
        assert host.restore() is True
        types = [event.type for event in session.events]
        assert types[-2:] == [EventKind.STEP_ENDED.value, EventKind.TURN_ENDED.value]
        assert session.events[-1].data["reason"] == {"kind": "aborted", "by": "disposed"}
        assert fold(session.header, session.events).issues == ()
        assert host.restore() is False
    finally:
        await host.aclose()


def test_handle_folds_and_appends_native_events(tmp_path: Path) -> None:
    session = _session(tmp_path)
    handle = NativeSessionHandle(session)
    try:
        assert handle.events == ()
        handle.append("model/selection", {"provider": "p", "model": "m"})
        view = handle.fold()
        assert view.state.model is not None
        assert view.state.model.provider == "p"
        assert handle.fold() is view
        handle.append_native(EventKind.SESSION_TITLE, {"title": "T", "source": "user"})
        assert handle.fold() is not view
        assert handle.fold().state.title == "T"
        assert len(handle.events) == 2
    finally:
        handle.aclose()
