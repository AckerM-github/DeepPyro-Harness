"""Native pre-send compaction through the TUI turn layer.

The native agent loop never compacts by itself (design decision D4): the TUI
checks the context threshold before every send, shows a compacting status, and
lets NativeCompactor append exactly one compaction.done fact. These tests use
the same scripted provider and SessionBuilder as the compaction suite, so no
network is touched and tool-pair boundaries are exercised for real.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.compact.native_compactor import select_region, tool_safe_cuts
from deeppyro.compact.native_service import NativeCompactService
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeSession
from deeppyro.tui.sink import TuiSink
from tests.compact.test_native_compactor import (
    HUGE_WINDOW,
    TINY_WINDOW,
    ScriptedProvider,
    build_session,
    summary_turn,
)
from tests.tui.test_native_runtime import _TurnHost

SUMMARY = "The parser bug was found and the fix landed."


def _service(
    tmp_path: Path,
    script: list[Any] | None = None,
    *,
    context_window: int = TINY_WINDOW,
) -> NativeCompactService:
    provider = ScriptedProvider(script or [summary_turn(SUMMARY)])
    return NativeCompactService(
        provider,
        "scripted-1",
        blocks_root=tmp_path / "blocks",
        keep_recent_tokens=0,
        context_window=context_window,
        threshold=0.85,
    )


class _Agent:
    """Minimal native agent double: records sends and reports host counters."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> Any:
        self.sent.append(text)
        return SimpleNamespace(final_text="native answer", finish_reason="stop", usage=None)

    def usage(self) -> dict[str, int]:
        return {"input": 11, "output": 7, "cached": 0}

    def turn_count(self) -> int:
        return 0

    def last_assistant_text(self) -> str:
        return "native answer"


async def _name_session_title() -> None:
    return None


def _host(
    tmp_path: Path,
    session: Any,
    compactor: Any,
    *,
    agent: Any = None,
) -> tuple[_TurnHost, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    runtime = SimpleNamespace(
        compactor=compactor,
        session=session,
        sink=TuiSink(events.append),
        agent=agent if agent is not None else _Agent(),
        name_session_title=_name_session_title,
    )
    return _TurnHost(runtime), events


def _compaction_events(session: Any) -> list[Any]:
    return [event for event in session.events if event.type == EventKind.COMPACTION_DONE]


async def test_pre_send_compaction_emits_status_and_one_fact(tmp_path: Path) -> None:
    path, builder = build_session(tmp_path, exchanges=3)
    service = _service(tmp_path)
    handle = NativeSessionHandle(builder.session)
    host, events = _host(tmp_path, handle, service)
    messages = tuple(fold(builder.session.header, builder.session.events).messages)
    expected = select_region(messages, 0)
    assert expected is not None
    before = len(builder.session.events)

    await host._compact_before_send()

    assert events == [{"type": "status", "phase": "compacting"}]
    assert host._phase == "thinking"
    assert len(builder.session.events) == before + 1
    facts = _compaction_events(builder.session)
    assert len(facts) == 1
    # Tool-pair boundaries: the retained tail and the compacted head both cut
    # only where no tool call is left dangling.
    cuts = tool_safe_cuts(messages)
    region = expected
    assert cuts[region.start_index] is True
    assert cuts[region.end_index + 1] is True
    assert tuple(facts[0].data["covers"]) == region.message_ids
    folded = handle.fold()
    summary_ids = [
        message.id for message in folded.messages if str(getattr(message.source, "compaction_id", ""))
    ]
    assert summary_ids
    assert all(message_id not in {m.id for m in folded.messages} for message_id in region.message_ids)

    builder.close()
    reopened = NativeSession.open(path)
    try:
        replay = fold(reopened.header, reopened.events)
    finally:
        reopened.close()
    replay_ids = [message.id for message in replay.messages]
    assert summary_ids[0] in replay_ids
    assert all(message_id not in replay_ids for message_id in region.message_ids)


async def test_pre_send_compaction_is_skipped_below_the_threshold(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    service = _service(tmp_path, context_window=HUGE_WINDOW)
    handle = NativeSessionHandle(builder.session)
    host, events = _host(tmp_path, handle, service)
    before = len(builder.session.events)

    await host._compact_before_send()

    assert events == []
    assert host._phase == "thinking"
    assert len(builder.session.events) == before
    assert _compaction_events(builder.session) == []


async def test_turn_compacts_before_sending_the_prompt(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=3)
    service = _service(tmp_path)
    handle = NativeSessionHandle(builder.session)
    agent = _Agent()
    host, events = _host(tmp_path, handle, service, agent=agent)
    before = len(builder.session.events)

    await host._run_turn("next request")

    assert agent.sent == ["next request"]
    assert events[0] == {"type": "status", "phase": "compacting"}
    assert events[-1] == {"type": "status", "phase": "done"}
    assert any(event.get("type") == "final" for event in events)
    assert len(_compaction_events(builder.session)) == 1
    assert len(builder.session.events) == before + 1
    infos = host.conversation.infos
    assert infos and "compacted" in infos[0]


async def test_compaction_failure_is_reported_and_the_turn_still_runs(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    handle = NativeSessionHandle(builder.session)
    before = len(builder.session.events)

    class _Broken:
        def should_compact(self, _messages: Any) -> bool:
            return True

        async def pre_send_compact(self, _session: Any, *, view: Any = None) -> None:
            raise RuntimeError("boom")

    host, events = _host(tmp_path, handle, _Broken())
    await host._compact_before_send()

    assert events == [
        {"type": "status", "phase": "compacting"},
        {"type": "error", "message": "compaction failed: RuntimeError: boom"},
    ]
    assert host._phase == "thinking"
    assert len(builder.session.events) == before
    assert _compaction_events(builder.session) == []
