"""Native compaction tests: one fact over fold, no v4 bracket, trace-only usage."""

from __future__ import annotations

import ast
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from deeppyro.agent.context_plan import message_tokens
from deeppyro.compact.blocks import BlockStore
from deeppyro.compact.native_compactor import (
    CompactionRegion,
    NativeCompactor,
    select_region,
    should_compact_before_send,
    tool_safe_cuts,
)
from deeppyro.compact.native_refs import RefStore
from deeppyro.compact.native_service import NativeCompactService
from deeppyro.core.errors import LlmError
from deeppyro.core.ids import (
    new_call_id,
    new_message_id,
    new_run_id,
    new_step_id,
    new_trace_id,
    new_turn_id,
    now_ms,
)
from deeppyro.core.llm import (
    Finish,
    ModelRequest,
    StopReason,
    StreamEvent,
    TextDelta,
    Usage,
    UsageEvent,
)
from deeppyro.core.messages import (
    CompactionSource,
    Message,
    ModelSource,
    SystemSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.observability.trace import RunRecord, TraceStore
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.schema import EventKind, NativeSession

CWD = "C:/work/deeppyro"
CREATED_AT = 1_700_000_000_000
SUMMARY = (
    "The user asked to fix the parser. The parser bug was found in tokenize(); "
    "the fix landed and the tests pass."
)
TINY_WINDOW = 10
HUGE_WINDOW = 10**9


# -- provider doubles -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    events: tuple[StreamEvent, ...]


class ScriptedProvider:
    name = "scripted"

    def __init__(self, script: list[ScriptedTurn]) -> None:
        self.script = list(script)
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        turn = self.script.pop(0)
        for event in turn.events:
            yield event

    async def aclose(self) -> None:
        return None


def finish(stop_reason: StopReason = StopReason.STOP, message: str | None = None) -> Finish:
    return Finish(stop_reason=stop_reason, message=message)


def usage(input_tokens: int = 120, output_tokens: int = 40) -> UsageEvent:
    return UsageEvent(
        Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )
    )


def summary_turn(text: str = SUMMARY) -> ScriptedTurn:
    return ScriptedTurn(events=(TextDelta(text=text), usage(), finish()))


# -- synthetic messages -----------------------------------------------------
def user_msg(text: str, message_id: str | None = None) -> Message:
    return Message(
        id=message_id or new_message_id(),
        role="user",
        content=(TextPart(text),),
        created_at=CREATED_AT,
        source=UserSource(),
    )


def assistant_msg(text: str, *, call_id: str | None = None) -> Message:
    content: tuple[TextPart | ToolCallPart, ...] = (TextPart(text),)
    if call_id is not None:
        content = (*content, ToolCallPart(id=call_id, name="read_file", arguments="{}"))
    return Message(
        id=new_message_id(),
        role="assistant",
        content=content,
        created_at=CREATED_AT,
        source=ModelSource(provider="fake", model="fake-1"),
    )


def tool_msg(call_id: str, text: str) -> Message:
    return Message(
        id=new_message_id(),
        role="tool",
        content=(TextPart(text),),
        created_at=CREATED_AT,
        source=ToolSource(call_id=call_id),
        tool_call_id=call_id,
    )


def summary_msg(compaction_id: str = "compaction-1") -> Message:
    return Message(
        id=new_message_id(),
        role="user",
        content=(TextPart(SUMMARY),),
        created_at=CREATED_AT,
        source=CompactionSource(compaction_id),
    )


def view_of(messages: tuple[Message, ...], system: Message | None = None) -> SessionView:
    return SessionView(
        header=None,  # type: ignore[arg-type]
        messages=messages,
        system=system,
        state=None,  # type: ignore[arg-type]
        pending_approvals=(),
        last_seq=0,
        last_message_id=None,
        plugin_events=(),
        issues=(),
    )


# -- native session builder -------------------------------------------------
class SessionBuilder:
    """Write a valid multi-turn native session log."""

    def __init__(self, path: Path, *, system_text: str | None = None) -> None:
        self.session = NativeSession.create(path, cwd=CWD, created_at=CREATED_AT)
        self.ts = CREATED_AT
        self.run_id = new_run_id()
        self.trace_id = new_trace_id()
        self.turn_id: str | None = None
        self.step_id: str | None = None
        self.message_ids: list[str] = []
        self.system: Message | None = None
        if system_text is not None:
            self.add_system(system_text)

    def _next_ts(self) -> int:
        self.ts = max(now_ms(), self.ts + 1)
        return self.ts

    def add_system(self, text: str) -> Message:
        message = Message(
            id=new_message_id(),
            role="system",
            content=(TextPart(text),),
            created_at=self._next_ts(),
            source=SystemSource(),
        )
        self.session.append(EventKind.SYSTEM_MESSAGE, {"message": message.to_dict()}, ts=self.ts)
        self.system = message
        return message

    def begin_turn(self) -> tuple[str, str]:
        assert self.turn_id is None
        self.turn_id = new_turn_id()
        self.step_id = new_step_id()
        self.session.append(
            EventKind.TURN_STARTED,
            ts=self._next_ts(),
            run_id=self.run_id,
            turn_id=self.turn_id,
            trace_id=self.trace_id,
        )
        self.session.append(
            EventKind.STEP_STARTED,
            ts=self._next_ts(),
            run_id=self.run_id,
            turn_id=self.turn_id,
            step_id=self.step_id,
            trace_id=self.trace_id,
        )
        return self.turn_id, self.step_id

    def end_turn(self) -> None:
        assert self.turn_id is not None and self.step_id is not None
        self.session.append(
            EventKind.STEP_ENDED,
            ts=self._next_ts(),
            run_id=self.run_id,
            turn_id=self.turn_id,
            step_id=self.step_id,
            trace_id=self.trace_id,
        )
        self.session.append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "completed"}},
            ts=self._next_ts(),
            run_id=self.run_id,
            turn_id=self.turn_id,
            trace_id=self.trace_id,
        )
        self.turn_id = None
        self.step_id = None

    def user(self, text: str) -> Message:
        message = Message(
            id=new_message_id(),
            role="user",
            content=(TextPart(text),),
            created_at=self._next_ts(),
            source=UserSource(),
        )
        self.session.append(
            EventKind.USER_MESSAGE, {"message": message.to_dict()}, ts=self.ts, turn_id=self.turn_id
        )
        self.message_ids.append(message.id)
        return message

    def assistant(self, text: str, *, tool: bool = False) -> tuple[Message, str | None]:
        assert self.turn_id is not None and self.step_id is not None
        call_id = new_call_id() if tool else None
        content: tuple[TextPart | ToolCallPart, ...] = (TextPart(text),)
        if call_id is not None:
            content = (*content, ToolCallPart(id=call_id, name="read_file", arguments="{}"))
        message = Message(
            id=new_message_id(),
            role="assistant",
            content=content,
            created_at=self._next_ts(),
            source=ModelSource(provider="fake", model="fake-1"),
        )
        self.session.append(
            EventKind.ASSISTANT_MESSAGE,
            {
                "message": message.to_dict(),
                "finish_reason": "tool_calls" if tool else "stop",
            },
            ts=self.ts,
            run_id=self.run_id,
            turn_id=self.turn_id,
            step_id=self.step_id,
            trace_id=self.trace_id,
        )
        self.message_ids.append(message.id)
        if call_id is not None:
            self.session.append(
                EventKind.TOOL_CALL,
                {"call_id": call_id, "name": "read_file", "arguments": "{}"},
                ts=self._next_ts(),
                run_id=self.run_id,
                turn_id=self.turn_id,
                step_id=self.step_id,
                trace_id=self.trace_id,
            )
        return message, call_id

    def tool_result(self, call_id: str, text: str) -> Message:
        assert self.turn_id is not None and self.step_id is not None
        message = Message(
            id=new_message_id(),
            role="tool",
            content=(TextPart(text),),
            created_at=self._next_ts(),
            source=ToolSource(call_id=call_id),
            tool_call_id=call_id,
        )
        self.session.append(
            EventKind.TOOL_RESULT,
            {"call_id": call_id, "message": message.to_dict()},
            ts=self.ts,
            run_id=self.run_id,
            turn_id=self.turn_id,
            step_id=self.step_id,
            trace_id=self.trace_id,
        )
        self.message_ids.append(message.id)
        return message

    def exchange(
        self,
        user_text: str,
        assistant_text: str,
        *,
        tool: bool = False,
    ) -> Message:
        self.begin_turn()
        self.user(user_text)
        message, call_id = self.assistant(assistant_text, tool=tool)
        if call_id is not None:
            self.tool_result(call_id, "file contents")
        self.end_turn()
        return message

    def close(self) -> None:
        if self.turn_id is not None:
            self.end_turn()
        self.session.close()


def build_session(tmp_path: Path, exchanges: int = 3, *, system: bool = True) -> tuple[Path, SessionBuilder]:
    path = tmp_path / "session-12345678-1234-1234-1234-123456789abc.jsonl"
    builder = SessionBuilder(path, system_text="You are a helpful coding agent." if system else None)
    for index in range(exchanges):
        builder.exchange(
            f"User request number {index}. "
            + "Please inspect the parser carefully and explain exactly what breaks "
            + "when the input contains nested brackets or an unterminated string. "
            + "Include the failing test names and the smallest reproducer you can find.",
            f"Assistant answer number {index}. "
            + "The parser fails because the tokenizer keeps the bracket depth on a "
            + "shared stack and never resets it between statements. I added a reset "
            + "at statement boundaries and updated the regression tests for it.",
        )
    return path, builder


def compactor(
    tmp_path: Path,
    script: list[ScriptedTurn],
    *,
    keep_recent_tokens: int = 0,
    trace_store: TraceStore | None = None,
) -> tuple[NativeCompactor, ScriptedProvider]:
    provider = ScriptedProvider(script)
    blocks = BlockStore(tmp_path / "blocks")
    refs = RefStore(tmp_path / "blocks")
    engine = NativeCompactor(
        provider,
        "scripted-1",
        keep_recent_tokens=keep_recent_tokens,
        context_window=len(script) * 1000 + 1000,
        blocks=blocks,
        refs=refs,
        trace_store=trace_store,
    )
    return engine, provider


# -- policy / region selection ---------------------------------------------
def test_should_compact_compares_estimated_tokens_with_threshold() -> None:
    messages = (user_msg("a" * 400), assistant_msg("b" * 400))
    assert should_compact_before_send(messages, context_window=TINY_WINDOW, threshold=0.85) is True
    assert should_compact_before_send(messages, context_window=HUGE_WINDOW, threshold=0.85) is False
    assert should_compact_before_send(messages, context_window=0, threshold=0.85) is False
    assert should_compact_before_send(view_of(messages), context_window=TINY_WINDOW) is True


def test_select_region_keeps_recent_tail_inside_tool_pair() -> None:
    call_id = new_call_id()
    messages = (
        user_msg("first user request with some words"),
        assistant_msg("calling a tool", call_id=call_id),
        tool_msg(call_id, "tool output that must stay with its call"),
    )
    region = select_region(messages, keep_recent_tokens=0)
    assert region is not None
    # The retained tail may not begin on the tool result, so the assistant call
    # moves into the tail with it; only the first user message is compacted.
    assert region.start_index == 0
    assert region.end_index == 0
    assert region.message_ids == (messages[0].id,)

    cuts = tool_safe_cuts(messages)
    assert cuts == (True, True, False, True)


def test_select_region_pins_an_existing_summary() -> None:
    summary = summary_msg()
    messages = (
        user_msg("old request"),
        summary,
        user_msg("new request"),
        assistant_msg("new answer"),
    )
    region = select_region(messages, keep_recent_tokens=0)
    assert region is not None
    assert region.start_index == 2
    assert summary.id not in region.message_ids


def test_region_validates_shape() -> None:
    with pytest.raises(ValueError):
        CompactionRegion(start_index=1, end_index=0, message_ids=(), message_tokens=0)
    with pytest.raises(ValueError):
        CompactionRegion(start_index=0, end_index=0, message_ids=("a", "b"), message_tokens=0)


# -- compaction fact --------------------------------------------------------
async def test_compaction_appends_exactly_one_fact_and_replays(tmp_path: Path) -> None:
    path, builder = build_session(tmp_path, exchanges=3)
    engine, _provider = compactor(tmp_path, [summary_turn()])
    session = builder.session
    before = len(session.events)
    expected = select_region(fold(session.header, session.events).messages, 0)
    assert expected is not None

    done = await engine.compact(session)

    assert done is not None
    assert len(session.events) == before + 1
    event = session.events[-1]
    assert event.type == EventKind.COMPACTION_DONE
    assert set(event.data) == {"compaction_id", "covers", "message"}
    assert event.data["compaction_id"] == done.compaction_id
    assert tuple(event.data["covers"]) == done.region.message_ids
    assert tuple(event.data["covers"]) == expected.message_ids
    summary = done.summary_message
    assert summary.role == "user"
    assert isinstance(summary.source, CompactionSource)
    assert summary.source.compaction_id == done.compaction_id
    summary_text = "".join(part.text for part in summary.content if isinstance(part, TextPart))
    assert SUMMARY in summary_text
    assert not any(event.type in {"compaction/start", "compaction/end"} for event in session.events)
    assert "input_tokens" not in json.dumps(dict(event.data))

    # Replay after restart: fold from the log alone finds the summary and drops covers.
    builder.close()
    reopened = NativeSession.open(path)
    try:
        view = fold(reopened.header, reopened.events)
    finally:
        reopened.close()
    ids = [message.id for message in view.messages]
    assert summary.id in ids
    assert all(message_id not in ids for message_id in done.region.message_ids)


async def test_compaction_preserves_system_prompt(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2, system=True)
    engine, provider = compactor(tmp_path, [summary_turn()])
    system = fold(builder.session.header, builder.session.events).system
    assert system is not None

    await engine.compact(builder.session)

    request = provider.requests[0]
    assert request.system is not None
    assert request.system.id == system.id
    assert request.system.to_dict() == system.to_dict()
    assert request.messages[-1].source.__class__.__name__ == "PluginSource"
    folded = fold(builder.session.header, builder.session.events)
    assert folded.system is not None and folded.system.id == system.id
    assert all(message.id != system.id for message in folded.messages)


async def test_second_compaction_never_covers_the_pinned_summary(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    engine, provider = compactor(tmp_path, [summary_turn(), summary_turn("second summary later")])
    first = await engine.compact(builder.session)
    assert first is not None
    builder.exchange(
        'The user asked for another review pass. "Please inspect the parser carefully and explain exactly what breaks when the input contains nested brackets or an unterminated string. Include the failing test names and the smallest reproducer you can find."',
        'The assistant replied with the full analysis. "The parser fails because the tokenizer keeps the bracket depth on a shared stack and never resets it between statements. I added a reset at statement boundaries and updated the regression tests for it."',
    )
    second = await engine.compact(builder.session)
    assert second is not None
    assert first.summary_message.id not in second.region.message_ids
    assert second.region.start_index >= 1
    assert len(provider.requests) == 2


# -- failure paths leave no fact -------------------------------------------
async def test_failed_request_leaves_no_fact_and_no_block(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    engine, _provider = compactor(tmp_path, [ScriptedTurn(events=(finish(StopReason.ERROR, "boom"),))])
    before = len(builder.session.events)
    with pytest.raises(LlmError):
        await engine.compact(builder.session)
    assert len(builder.session.events) == before
    assert not list((tmp_path / "blocks").glob("*.jsonl"))


async def test_summary_not_smaller_than_shadowed_content_leaves_no_fact(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    engine, _provider = compactor(tmp_path, [summary_turn("x" * 20000)])
    before = len(builder.session.events)
    with pytest.raises(LlmError, match="not smaller"):
        await engine.compact(builder.session)
    assert len(builder.session.events) == before
    assert not list((tmp_path / "blocks").glob("*.jsonl"))


async def test_aborted_summarization_is_reported(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    engine, _provider = compactor(
        tmp_path,
        [ScriptedTurn(events=(TextDelta(text="partial summary"), finish(StopReason.ABORTED)))],
    )
    with pytest.raises(Exception, match="aborted"):
        await engine.compact(builder.session)


# -- service entry points ---------------------------------------------------
async def test_service_manual_and_presend_paths(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    provider = ScriptedProvider([summary_turn(), summary_turn("another summary")])
    service = NativeCompactService(
        provider,
        "scripted-1",
        blocks_root=tmp_path / "service-blocks",
        keep_recent_tokens=0,
        context_window=TINY_WINDOW,
        threshold=0.85,
    )
    view = fold(builder.session.header, builder.session.events)
    assert service.should_compact(view) is True
    assert service.presend_should_compact(view) is True

    done = await service.pre_send_compact(builder.session)
    assert done is not None
    assert len(provider.requests) == 1

    builder.exchange(
        'The user asked for another review pass. "Please inspect the parser carefully and explain exactly what breaks when the input contains nested brackets or an unterminated string. Include the failing test names and the smallest reproducer you can find."',
        'The assistant replied with the full analysis. "The parser fails because the tokenizer keeps the bracket depth on a shared stack and never resets it between statements. I added a reset at statement boundaries and updated the regression tests for it."',
    )
    manual = await service.native_compact(builder.session)
    assert manual is not None
    assert len(provider.requests) == 2

    quiet = NativeCompactService(
        provider,
        "scripted-1",
        blocks_root=tmp_path / "quiet-blocks",
        keep_recent_tokens=0,
        context_window=HUGE_WINDOW,
    )
    fresh_view = fold(builder.session.header, builder.session.events)
    assert quiet.should_compact(fresh_view) is False
    assert await quiet.pre_send_compact(builder.session) is None


# -- trace only observability ----------------------------------------------
async def test_usage_and_latency_go_to_trace_not_the_session(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    traces = TraceStore(tmp_path / "traces")
    run = RunRecord(
        run_id=builder.run_id,
        trace_id=builder.trace_id,
        session_id=builder.session.header.id,
        started_at=CREATED_AT,
        provider="scripted",
        model="scripted-1",
    )
    traces.create_run(run)
    engine, _provider = compactor(tmp_path, [summary_turn()], trace_store=traces)
    turn_id, step_id = builder.begin_turn()
    try:
        done = await engine.compact(
            builder.session,
            envelope={
                "run_id": builder.run_id,
                "turn_id": turn_id,
                "step_id": step_id,
                "trace_id": builder.trace_id,
            },
        )
        assert done is not None
        latest = builder.session.events[-1]
        assert latest.run_id == builder.run_id
        assert latest.turn_id == turn_id
        assert "response" not in latest.data
        assert "usage" not in json.dumps(dict(latest.data))
    finally:
        builder.end_turn()

    log = traces.read(builder.run_id)
    records = [record for record in log.records if record.type == "model_call"]
    assert len(records) == 1
    record = records[0]
    assert record.provider == "scripted"
    assert record.model == "scripted-1"
    assert record.finish_reason == "stop"
    assert "input_tokens" not in json.dumps(dict(builder.session.events[-1].data))


# -- boundary ---------------------------------------------------------------
NATIVE_MODULES = (
    "src/deeppyro/compact/native_compactor.py",
    "src/deeppyro/compact/native_compress.py",
    "src/deeppyro/compact/native_refs.py",
    "src/deeppyro/compact/native_service.py",
)
FORBIDDEN_PREFIXES = (
    "deeppyro.core.wire",
    "deeppyro.core.protocols",
    "deeppyro.session.derive",
    "deeppyro.compact.compactor",
    "deeppyro.compact.surface",
    "deeppyro.compact.tokens",
    "deeppyro.compact.refs",
    "deeppyro.compact.compress_tool",
    "deeppyro.compact.block_tools",
)


def test_native_modules_do_not_import_v4_compact_or_wire() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in NATIVE_MODULES:
        source = (root / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        for module in imported:
            assert not module.startswith(FORBIDDEN_PREFIXES), f"{relative} imports {module}"


async def test_compaction_reports_changed_session(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    engine, provider = compactor(tmp_path, [summary_turn()])
    session = builder.session
    before = len(session.events)
    # A provider that mutates the session while the summarizer is running.
    original_stream = provider.stream

    async def mutating_stream(request: ModelRequest) -> AsyncIterator[StreamEvent]:
        concurrent_summary = Message(
            id=new_message_id(),
            role="user",
            content=(TextPart("a concurrent compaction summary"),),
            created_at=builder._next_ts(),
            source=CompactionSource("concurrent-compaction"),
        )
        session.append(
            EventKind.COMPACTION_DONE,
            {
                "compaction_id": "concurrent-compaction",
                "covers": builder.message_ids[:2],
                "message": concurrent_summary.to_dict(),
            },
            ts=builder._next_ts(),
        )
        async for event in original_stream(request):
            yield event

    provider.stream = mutating_stream  # type: ignore[method-assign]
    with pytest.raises(Exception, match="session changed"):
        await engine.compact(session)
    # Only the concurrent compaction was appended; the compactor wrote nothing.
    assert len(session.events) == before + 1
    concurrent = [event for event in session.events if event.type == EventKind.COMPACTION_DONE]
    assert len(concurrent) == 1
    assert concurrent[0].data["covers"] == builder.message_ids[:2]


def test_tool_safe_cuts_and_balanced_tool_messages() -> None:
    call_id = new_call_id()
    messages = (
        user_msg("q"),
        assistant_msg("calling", call_id=call_id),
        tool_msg(call_id, "result"),
        assistant_msg("answer"),
    )
    assert tool_safe_cuts(messages) == (True, True, False, True, True)
    assert message_tokens(messages) > 0
