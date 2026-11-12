"""Tests for context compaction: ranges, the record transaction, and the fold."""

from __future__ import annotations

import pytest

from deeppyro.compact import (
    CHECKPOINT_PREAMBLE,
    COMPACTION_INSTRUCTION,
    Compactor,
    entry_state,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
    fold_surface,
)
from deeppyro.core.errors import LlmError, SessionError
from deeppyro.core.ids import new_message_id
from deeppyro.core.protocols import Finish, TextDelta, ToolSchema
from deeppyro.core.wire import Message, TextPart

from .fakes import FakeSessionLog, ScriptedProvider, ScriptedTurn, finish, usage


def add_system(session: FakeSessionLog, text: str = "You are a helpful coding agent.") -> None:
    message = Message(
        role="system",
        content=[TextPart(text=text)],
        id=new_message_id(),
        source={"kind": "system-prompt"},
    )
    session.append(
        "system/message",
        {"turn": 1, "step": 1, "message": message.model_dump(exclude_none=True)},
        surfaceOp="append",
    )


def add_user(session: FakeSessionLog, text: str) -> None:
    message = Message(
        role="user", content=[TextPart(text=text)], id=new_message_id(), source={"kind": "user"}
    )
    session.append("user/message", message.model_dump(exclude_none=True), surfaceOp="append")


def add_assistant(session: FakeSessionLog, text: str) -> None:
    message = Message(
        role="assistant",
        content=[TextPart(text=text)],
        id=new_message_id(),
        source={"kind": "model", "provider": "fake-provider", "model": "fake-model"},
    )
    session.append(
        "assistant/message",
        {"turn": 1, "step": 1, "message": message.model_dump(exclude_none=True)},
        surfaceOp="append",
    )


def add_header(session: FakeSessionLog) -> None:
    schema = ToolSchema(name="echo", description="Echo text.", parameters={"type": "object"})
    header = {
        "config": {
            "provider": "fake-provider",
            "model": "fake-model",
            "maxTokens": None,
            "reasoningEffort": None,
        },
        "adapterDefaults": {"reasoningEffort": True, "maxTokens": True},
        "tools": [schema.model_dump()],
    }
    session.append("request/header", {"turn": 1, "step": 1, "header": header, "reason": "initial"})


def build_conversation(session: FakeSessionLog, *, pairs: int = 4, size: int = 200) -> None:
    add_system(session)
    add_header(session)
    for index in range(pairs):
        add_user(session, f"user-{index} " + "u" * size)
        add_assistant(session, f"assistant-{index} " + "a" * size)


def make_compactor(
    script: list[ScriptedTurn],
    *,
    keep_recent_tokens: int = 120,
    max_tokens: int | None = None,
) -> tuple[Compactor, ScriptedProvider]:
    provider = ScriptedProvider(script, model="sum-model")
    compactor = Compactor(
        provider=provider,
        model="sum-model",
        keep_recent_tokens=keep_recent_tokens,
        context_window=32768,
        threshold=0.85,
        max_tokens=max_tokens,
    )
    return compactor, provider


SUMMARY = "## Current Work\n- wiring the loop tests"


def test_token_estimates_use_frozen_ratio() -> None:
    assert estimate_text_tokens("") == 0.0
    assert estimate_text_tokens("abcdefg") == 2.0
    message = Message(role="user", content=[TextPart(text="abc")])
    assert estimate_message_tokens(message) == pytest.approx(3 / 3.5 + 4)
    assert estimate_messages_tokens([message, message]) == pytest.approx(2 * (3 / 3.5 + 4))


def test_should_compact_uses_threshold_and_context_window() -> None:
    compactor, _ = make_compactor([])
    compactor.context_window = 100
    compactor.threshold = 0.85
    small = [Message(role="user", content=[TextPart(text="abcd")])]
    assert compactor.should_compact(small) is False
    big = [Message(role="user", content=[TextPart(text="x" * 400)])]
    assert compactor.should_compact(big) is True


async def test_compact_writes_four_records_and_folds_checkpoint() -> None:
    session = FakeSessionLog()
    build_conversation(session)
    before = len(session.records)
    compactor, provider = make_compactor(
        [ScriptedTurn(events=[TextDelta(SUMMARY), usage(40, 12), finish("stop")])], max_tokens=512
    )

    assert await compactor.compact(session, session.derive_messages()) is True

    added = session.records[before:]
    assert [record.type for record in added] == [
        "compaction/start",
        "compaction/summary",
        "user/message",
        "compaction/end",
    ]
    start, summary, checkpoint, end = added
    compaction_id = start.data["compactionId"]
    assert summary.data["compactionId"] == compaction_id
    assert end.data["compactionId"] == compaction_id
    assert "error" not in end.data

    assert summary.data["shadowedRange"]["start"] < summary.data["shadowedRange"]["end"]
    assert summary.data["shadowedSeqs"]
    assert summary.data["llmStreamCall"] is True
    assert summary.data["summary"][0]["text"] == SUMMARY
    assert summary.data["rawOutput"][0]["text"] == SUMMARY
    assert summary.data["provider"] == "fake-provider"
    assert summary.data["model"] == "sum-model"
    assert summary.data["maxTokens"] == 512
    assert summary.data["usage"]["inputTokens"] == 40
    assert summary.data["shadowedTokenCount"] > 0

    assert checkpoint.surfaceOp == {
        "op": "replace",
        "startSeq": summary.data["shadowedRange"]["start"],
        "endSeq": summary.data["shadowedRange"]["end"],
    }
    assert checkpoint.sourceEventSeqs == [start.seq, summary.seq, *summary.data["shadowedSeqs"]]
    assert checkpoint.data["role"] == "user"
    assert checkpoint.data["source"] == {"kind": "compact-checkpoint", "compactionId": compaction_id}

    derived = session.derive_messages()
    assert derived[0].role == "system"
    assert derived[1].source is not None and derived[1].source["kind"] == "compact-checkpoint"
    assert CHECKPOINT_PREAMBLE in derived[1].text()
    assert SUMMARY in derived[1].text()
    assert "user-0" not in "".join(message.text() for message in derived)
    # The newest exchange stays visible.
    assert "user-3" in derived[-2].text()
    assert len(derived) == 4

    nodes = fold_surface(session.records)
    assert nodes[0].type == "system/message"
    assert nodes[1].type == "user/message"
    assert nodes[1].seq == checkpoint.seq

    assert provider.requests[0].messages[-1].text() == COMPACTION_INSTRUCTION
    assert provider.requests[0].messages[0].role == "system"
    assert [tool.name for tool in provider.requests[0].tools] == ["echo"]
    assert provider.requests[0].model == "sum-model"
    assert provider.requests[0].max_tokens == 512

    state = entry_state(session.records)
    assert state.unmatched_start_seq is None


async def test_compact_returns_false_without_a_safe_range() -> None:
    session = FakeSessionLog()
    add_system(session)
    add_user(session, "short question")
    add_assistant(session, "short answer")
    before = len(session.records)
    compactor, _ = make_compactor([], keep_recent_tokens=10_000)

    assert await compactor.compact(session, session.derive_messages()) is False
    assert len(session.records) == before


async def test_second_compaction_supersedes_the_previous_checkpoint() -> None:
    session = FakeSessionLog()
    build_conversation(session)
    compactor, _ = make_compactor(
        [
            ScriptedTurn(events=[TextDelta(SUMMARY), finish("stop")]),
            ScriptedTurn(events=[TextDelta("## Current Work\n- second checkpoint"), finish("stop")]),
        ]
    )
    assert await compactor.compact(session) is True
    first_checkpoint = session.records_of("user/message")[-1].seq

    for index in range(4):
        add_user(session, f"more-{index} " + "m" * 200)
        add_assistant(session, f"reply-{index} " + "r" * 200)
    assert await compactor.compact(session) is True

    checkpoints = [record for record in session.records if record.type == "user/message" and record.surfaceOp]
    second_checkpoint = checkpoints[-1]
    assert second_checkpoint.sourceEventSeqs is not None
    assert first_checkpoint in second_checkpoint.sourceEventSeqs
    derived = session.derive_messages()
    sources = [message.source["kind"] for message in derived if message.source]
    assert sources[:2] == ["system-prompt", "compact-checkpoint"]
    assert sources.count("compact-checkpoint") == 1
    assert "second checkpoint" in derived[1].text()
    assert SUMMARY not in derived[1].text()


async def test_compaction_failure_closes_the_transaction_with_error() -> None:
    session = FakeSessionLog()
    build_conversation(session)
    before = len(session.records)
    provider = ScriptedProvider([ScriptedTurn(error=LlmError("summarizer down"))], model="sum-model")
    compactor = Compactor(provider=provider, model="sum-model", keep_recent_tokens=120)

    with pytest.raises(LlmError):
        await compactor.compact(session)

    added = session.records[before:]
    assert [record.type for record in added] == ["compaction/start", "compaction/end"]
    assert added[1].data["error"]["message"] == "summarizer down"
    assert added[0].data["compactionId"] == added[1].data["compactionId"]
    assert entry_state(session.records).unmatched_start_seq is None


async def test_unmatched_compaction_blocks_a_second_one() -> None:
    session = FakeSessionLog()
    build_conversation(session)
    session.append("compaction/start", {"compactionId": "stuck"})
    compactor, _ = make_compactor([ScriptedTurn(events=[TextDelta(SUMMARY), finish("stop")])])

    with pytest.raises(SessionError):
        await compactor.compact(session)


async def test_summary_must_be_smaller_than_the_shadowed_span() -> None:
    session = FakeSessionLog()
    build_conversation(session)
    before = len(session.records)
    compactor, _ = make_compactor([ScriptedTurn(events=[TextDelta("x" * 5000), finish("stop")])])

    with pytest.raises(LlmError) as excinfo:
        await compactor.compact(session)

    assert "not smaller" in str(excinfo.value)
    added = session.records[before:]
    assert [record.type for record in added] == ["compaction/start", "compaction/end"]
    assert added[1].data["error"]["message"]


async def test_empty_summary_is_an_error() -> None:
    session = FakeSessionLog()
    build_conversation(session)
    compactor, _ = make_compactor([ScriptedTurn(events=[TextDelta("   "), finish("stop")])])

    with pytest.raises(LlmError) as excinfo:
        await compactor.compact(session)
    assert "no text summary" in str(excinfo.value)
    assert session.records[-1].type == "compaction/end"
    assert session.records[-1].data["error"]["message"].startswith("summarization produced no text")


async def test_truncated_summary_is_an_error() -> None:
    session = FakeSessionLog()
    build_conversation(session)
    compactor, _ = make_compactor([ScriptedTurn(events=[TextDelta("partial"), Finish(kind="length")])])

    with pytest.raises(LlmError) as excinfo:
        await compactor.compact(session)
    assert "truncated" in str(excinfo.value)
    assert session.records[-1].type == "compaction/end"


async def test_compactor_requires_a_session_with_records() -> None:
    class Bare:
        session_id = "s"

        def append(self, type: str, data: dict) -> int:  # pragma: no cover - never called
            return 0

        def derive_messages(self) -> list:
            return []

    compactor, _ = make_compactor([])
    with pytest.raises(SessionError):
        await compactor.compact(Bare())
