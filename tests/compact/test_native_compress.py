"""Native compress tests: per-block facts, refs, guards, partial writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.compact.blocks import BlockStore
from deeppyro.compact.native_compress import (
    FINGERPRINT_NOTE,
    HEAD_CHARS,
    MINIMUM_STATUS,
    UNCOVERED_STATUS,
    NativeCompressor,
    parse_content,
)
from deeppyro.compact.native_refs import RefStore
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeSession
from tests.compact.test_native_compactor import SessionBuilder, build_session

SID = "session-12345678-1234-1234-1234-123456789abc"


def compressor(tmp_path: Path) -> tuple[NativeCompressor, BlockStore, RefStore]:
    blocks = BlockStore(tmp_path / "blocks")
    refs = RefStore(tmp_path / "blocks")
    return NativeCompressor(blocks, refs), blocks, refs


def content(*blocks: tuple[str, str, str, str]) -> str:
    return "\n\n".join(f"{start}–{end} {title}\n{body}" for start, end, title, body in blocks)


def compaction_events(session: NativeSession) -> list:
    return [event for event in session.events if event.type == EventKind.COMPACTION_DONE]


# -- pure parsing -----------------------------------------------------------
def test_parse_content_reports_invalid_shapes() -> None:
    with pytest.raises(Exception) as raised:
        parse_content("")
    assert "non-empty string" in str(raised.value)
    with pytest.raises(Exception) as raised:
        parse_content("no header here")
    assert "must start with a range header" in str(raised.value)
    with pytest.raises(Exception) as raised:
        parse_content("m00005–m00002 reversed")
    assert "is reversed" in str(raised.value)
    with pytest.raises(Exception) as raised:
        parse_content("m00001–m00002 title\n")
    assert "empty summary" in str(raised.value)


def test_parse_content_keeps_titles_and_bodies() -> None:
    parsed = parse_content(content(("m00001", "m00002", "First", "Summary one.\nMore.")))
    assert len(parsed) == 1
    assert parsed[0].start_ref == "m00001"
    assert parsed[0].end_ref == "m00002"
    assert parsed[0].title == "First"
    assert parsed[0].body == "Summary one.\nMore."


# -- commit / replay --------------------------------------------------------
def test_compress_commits_one_fact_per_block_and_replays(tmp_path: Path) -> None:
    path, builder = build_session(tmp_path, exchanges=3)
    engine, blocks, refs = compressor(tmp_path)
    first, second = builder.message_ids[0], builder.message_ids[1]
    third, fourth = builder.message_ids[3], builder.message_ids[4]
    before = len(builder.session.events)

    outcome = engine.compress(
        builder.session,
        content(
            ("m00001", "m00002", "First exchange", "Summary of the first exchange."),
            ("m00004", "m00005", "Second exchange", "Summary of the second exchange."),
        ),
    )

    assert outcome.ok is True
    assert "Compressed m00001–m00002, m00004–m00005" in outcome.text
    assert FINGERPRINT_NOTE in outcome.text
    assert len(builder.session.events) == before + 2
    events = compaction_events(builder.session)
    assert [event.data["covers"] for event in events] == [[first, second], [third, fourth]]
    compaction_ids = {event.data["compaction_id"] for event in events}
    assert len(compaction_ids) == 1
    compaction_id = compaction_ids.pop()
    assert set(outcome.meta["blocks"][0].keys()) == {
        "id",
        "range",
        "startMessageId",
        "endMessageId",
        "start_index",
        "end_index",
        "savedTokens",
    }
    assert outcome.meta["compactionId"] == compaction_id
    for event in events:
        assert set(event.data) == {"compaction_id", "covers", "message"}
        summary = event.data["message"]
        assert summary["role"] == "user"
        assert summary["source"]["kind"] == "compaction"
        assert summary["source"]["compaction_id"] == event.data["compaction_id"]
    assert outcome.meta["savedTokens"] >= 0

    builder.close()
    reopened = NativeSession.open(path)
    try:
        view = fold(reopened.header, reopened.events)
    finally:
        reopened.close()
    visible = [message.id for message in view.messages]
    summary_ids = [event.data["message"]["id"] for event in events]
    assert visible == [summary_ids[0], builder.message_ids[2], summary_ids[1], builder.message_ids[5]]
    assert first not in visible and second not in visible
    assert third not in visible and fourth not in visible

    stored, corrupt = blocks.load(SID)
    assert corrupt == 0
    assert [record.source for record in stored] == ["compress", "compress"]
    assert [record.start_message_id for record in stored] == [first, third]
    assert [record.end_message_id for record in stored] == [second, fourth]
    assert {record.compaction_id for record in stored} == {compaction_id}
    assert refs.entries(SID)


# -- system prompt ----------------------------------------------------------
def test_compress_preserves_system_prompt_and_can_start_at_first_message(tmp_path: Path) -> None:
    path, builder = build_session(tmp_path, exchanges=2, system=True)
    system_id = builder.system.id
    engine, _blocks, _refs = compressor(tmp_path)
    first = builder.message_ids[0]

    outcome = engine.compress(builder.session, "m00001–m00002 first turn\nSummary of turn one.\n")
    assert outcome.ok is True, outcome.text

    builder.close()
    reopened = NativeSession.open(path)
    try:
        view = fold(reopened.header, reopened.events)
    finally:
        reopened.close()
    assert view.system is not None
    assert view.system.id == system_id
    assert first not in [message.id for message in view.messages]
    assert view.messages[0].source.kind == "compaction"


# -- tool pair guard --------------------------------------------------------
def test_compress_rejects_tool_pair_cuts(tmp_path: Path) -> None:
    path = tmp_path / "session-12345678-1234-1234-1234-123456789abc.jsonl"
    builder = SessionBuilder(path, system_text="system prompt")
    builder.exchange("read a file", "reading", tool=True)
    builder.exchange("second question", "second answer")
    engine, _blocks, _refs = compressor(tmp_path)
    before = len(builder.session.events)

    outcome = engine.compress(builder.session, "m00003–m00003 lone tool result\nToo late.\n")
    assert outcome.ok is False
    assert outcome.code == "COMPACT_SPLITS_TOOL_PAIR"
    assert "cuts through a tool call/result pair" in outcome.text
    assert len(builder.session.events) == before

    outcome = engine.compress(builder.session, "m00001–m00002 cuts a call\nBad range.\n")
    assert outcome.ok is False
    assert outcome.code == "COMPACT_SPLITS_TOOL_PAIR"
    assert len(builder.session.events) == before

    outcome = engine.compress(
        builder.session, "m00001–m00003 whole exchange\nSummary of the tool exchange.\n"
    )
    assert outcome.ok is True, outcome.text
    builder.close()


# -- range guards -----------------------------------------------------------
def test_compress_rejects_overlap_adjacent_and_unknown_refs(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=3)
    engine, blocks, _refs = compressor(tmp_path)
    before = len(builder.session.events)

    outcome = engine.compress(
        builder.session,
        content(
            ("m00001", "m00003", "One", "Overlapping."),
            ("m00003", "m00004", "Two", "Also overlapping."),
        ),
    )
    assert outcome.ok is False
    assert outcome.code == "COMPACT_OVERLAP"
    assert "ascending order and must not overlap" in outcome.text

    outcome = engine.compress(
        builder.session,
        content(("m00001", "m00002", "One", "Adjacent."), ("m00003", "m00004", "Two", "Also adjacent.")),
    )
    assert outcome.ok is False
    assert outcome.code == "COMPACT_ADJACENT_RANGES"

    outcome = engine.compress(
        builder.session, "m00001–m00002 first\nSummary.\n\nm00004–m00005 second\nSummary.\n"
    )
    assert outcome.ok is True, outcome.text
    before = len(builder.session.events)

    outcome = engine.compress(builder.session, "m00999–m00999 out of range\nSummary.\n")
    assert outcome.ok is False
    assert outcome.code == "COMPACT_REF_NOT_FOUND"
    assert "out of range" in outcome.text
    assert len(builder.session.events) == before
    stored, corrupt = blocks.load(SID)
    assert corrupt == 0
    assert len(stored) == 2


# -- minimum ----------------------------------------------------------------
def test_compress_refuses_when_only_pinned_summary_remains(tmp_path: Path) -> None:
    path, builder = build_session(tmp_path, exchanges=2)
    engine, _blocks, _refs = compressor(tmp_path)
    outcome = engine.compress(builder.session, "m00001–m00004 whole history\nEverything, once.\n")
    assert outcome.ok is True, outcome.text
    before = len(builder.session.events)
    assert compaction_events(builder.session)

    outcome = engine.compress(builder.session, "m00001–m00001 old\nRetry.\n")
    assert outcome.ok is True
    assert outcome.text == MINIMUM_STATUS
    assert outcome.meta["minimum"] is True
    assert len(builder.session.events) == before
    builder.close()
    reopened = NativeSession.open(path)
    try:
        view = fold(reopened.header, reopened.events)
    finally:
        reopened.close()
    assert len(view.messages) == 1
    assert view.messages[0].source.kind == "compaction"


# -- partial write ----------------------------------------------------------
def test_partial_write_reports_committed_blocks(tmp_path: Path, monkeypatch) -> None:
    _path, builder = build_session(tmp_path, exchanges=3)
    engine, blocks, _refs = compressor(tmp_path)
    original = NativeSession.append
    calls = {"count": 0}

    def flaky(session, kind, data=None, **envelope):
        if kind == EventKind.COMPACTION_DONE:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("disk full")
        return original(session, kind, data, **envelope)

    monkeypatch.setattr(NativeSession, "append", flaky)
    outcome = engine.compress(
        builder.session,
        content(
            ("m00001", "m00002", "First", "First summary."),
            ("m00004", "m00005", "Second", "Second summary."),
        ),
    )
    monkeypatch.setattr(NativeSession, "append", original)

    assert outcome.ok is False
    assert outcome.code == "COMPACT_PARTIAL_WRITE"
    assert outcome.meta["committedRecords"] == 1
    assert "after 1 block(s) were written" in outcome.text
    events = compaction_events(builder.session)
    assert len(events) == 1
    assert events[0].data["covers"] == [builder.message_ids[0], builder.message_ids[1]]
    stored, corrupt = blocks.load(SID)
    assert corrupt == 0
    assert events[0].data["compaction_id"] in {record.compaction_id for record in stored}
    view = fold(builder.session.header, builder.session.events)
    summary_id = events[0].data["message"]["id"]
    assert summary_id in [message.id for message in view.messages]
    builder.close()


# -- refs -------------------------------------------------------------------
def test_compress_freezes_refs_for_new_messages(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=3)
    engine, _blocks, refs = compressor(tmp_path)
    first = engine.compress(builder.session, "m00001–m00002 first\nSummary one.\n")
    assert first.ok is True
    entries = {entry.ref: entry.message_id for entry in refs.entries(SID)}
    assert entries["m00001"] == builder.message_ids[0]
    assert entries["m00002"] == builder.message_ids[1]
    assert len(entries) == 6

    second = engine.compress(builder.session, "m00004–m00005 rest\nSummary two.\n")
    assert second.ok is True, second.text
    stored = {entry.ref: entry.message_id for entry in refs.entries(SID)}
    assert stored["m00004"] == builder.message_ids[3]
    assert stored["m00005"] == builder.message_ids[4]
    assert second.meta["blocks"][0]["range"] == {"start": "m00004", "end": "m00005"}
    builder.close()


# -- result surface ---------------------------------------------------------
def test_compress_result_text_and_uncovered_status(tmp_path: Path) -> None:
    _path, builder = build_session(tmp_path, exchanges=2)
    engine, _blocks, _refs = compressor(tmp_path)
    outcome = engine.compress(builder.session, "m00001–m00002 first\nShort summary.\n")
    assert outcome.ok is True
    assert outcome.text.startswith("[Compressed m00001–m00002 → 1 block(s), ~")
    assert outcome.text.endswith(UNCOVERED_STATUS)
    assert "full summary" in outcome.text
    assert "head" in outcome.text and "tail" in outcome.text
    assert HEAD_CHARS == 30
    assert outcome.meta["savedTokens"] >= 0
    builder.close()


# -- no v4 imports ----------------------------------------------------------
def test_native_compress_module_has_no_v4_imports() -> None:
    import ast

    path = Path(__file__).resolve().parents[2] / "src" / "deeppyro" / "compact" / "native_compress.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    forbidden = (
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
    for module in modules:
        assert not module.startswith(forbidden), f"{path.name} imports v4 module {module}"
