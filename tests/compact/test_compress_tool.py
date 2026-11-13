"""Tests for the compress tool: parsing, validation, transaction, result text."""

from __future__ import annotations

import pathlib
from itertools import pairwise

import pytest
from pydantic import ValidationError

from deeppyro.builtin.compress import CompressConfig, CompressPlugin
from deeppyro.compact.blocks import CompactError
from deeppyro.compact.compress_tool import (
    FINGERPRINT_NOTE,
    HEAD_CHARS,
    MINIMUM_STATUS,
    TAIL_CHARS,
    UNCOVERED_STATUS,
    CompressTool,
    parse_content,
)
from deeppyro.compact.refs import RefEntry
from deeppyro.core.errors import ConfigError, SessionError
from deeppyro.session import Session, SessionStore, fold_surface
from tests.compact import support


def _body(text: str = "Summary of the consumed work.") -> str:
    return text


async def _run(tool: CompressTool, session: Session, content: str):
    return await tool.run({"content": content}, support.make_ctx(session))


# -- parsing -------------------------------------------------------------


def test_parse_content_splits_blocks_and_normalizes_refs() -> None:
    content = "m00005–m00072 主题\n\nline one\nline two\nm00080–m00085\n\nsecond body\n"
    blocks = parse_content(content)
    assert [block.start_ref for block in blocks] == ["m00005", "m00080"]
    assert [block.end_ref for block in blocks] == ["m00072", "m00085"]
    assert blocks[0].title == "主题"
    assert blocks[0].body == "line one\nline two"
    assert blocks[1].title == ""
    assert blocks[1].body == "second body"


@pytest.mark.parametrize("sep", ["–", "-", "—"])
def test_parse_content_accepts_all_range_separators(sep: str) -> None:
    blocks = parse_content(f"m1{sep}m3 topic\n\nbody")
    assert blocks[0].start_ref == "m00001"
    assert blocks[0].end_ref == "m00003"
    assert blocks[0].title == "topic"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("", "COMPACT_INVALID_CONTENT"),
        ("   \n  ", "COMPACT_INVALID_CONTENT"),
        ("just a summary", "COMPACT_INVALID_CONTENT"),
        ("summary first\nm00001–m00002 topic\nbody", "COMPACT_INVALID_CONTENT"),
        ("m00003–m00001 topic\n\nbody", "COMPACT_INVALID_RANGE"),
        ("m00001–m00002 topic\n\n   ", "COMPACT_EMPTY_SUMMARY"),
    ],
)
def test_parse_content_rejects_malformed_payloads(content: str, code: str) -> None:
    with pytest.raises(CompactError) as info:
        parse_content(content)
    assert info.value.code == code


# -- tool metadata -------------------------------------------------------


def test_tool_schema_is_content_only() -> None:
    assert CompressTool.name == "compress"
    assert CompressTool.parallel_safe is False
    parameters = CompressTool.parameters
    assert set(parameters["properties"]) == {"content"}
    assert parameters["properties"]["content"]["type"] == "string"
    assert parameters["required"] == ["content"]
    assert parameters["additionalProperties"] is False
    assert "billion-context" not in CompressTool.description


# -- single-block transaction -------------------------------------------


async def test_single_block_transaction(tmp_path: pathlib.Path) -> None:
    tool, store, blocks, refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    before = len(session.records)

    result = await _run(tool, session, "m00001–m00002 主题\n\nCompressed summary.")

    assert result.is_error is False
    assert len(session.records) == before + 4
    assert [record.type for record in session.records[-4:]] == [
        "compaction/start",
        "compaction/summary",
        "user/message",
        "compaction/end",
    ]
    stored = blocks.list(session.session_id)
    assert len(stored) == 1
    assert stored[0].id == "b1"
    assert stored[0].start_seq == 1
    assert stored[0].end_seq == 2
    assert stored[0].start_ref == "m00001"
    assert stored[0].end_ref == "m00002"
    assert stored[0].title == "主题"

    messages = session.derive_messages()
    assert len(messages) == 6
    checkpoint = messages[1]
    assert (checkpoint.source or {}).get("kind") == "compact-checkpoint"
    assert "Compressed summary." in checkpoint.text()

    # The checkpoint replaced the range in place: system, cp, u2, a2, tool, u3.
    assert messages[0].role == "system"
    assert messages[2].id == "msg_u2"

    meta = result.meta or {}
    assert isinstance(meta["compactionId"], str) and meta["compactionId"]
    assert meta["blocks"] == [
        {"id": "b1", "range": {"start": "m00001", "end": "m00002"}, "startSeq": 1, "endSeq": 2}
    ]
    assert meta["savedTokens"] > 0

    first_line = result.text.splitlines()[0]
    assert first_line.startswith("[Compressed m00001–m00002 → 1 block(s), ~")
    assert first_line.endswith(" tokens saved.]")
    assert FINGERPRINT_NOTE in result.text
    assert result.text.rstrip().endswith(UNCOVERED_STATUS)

    frozen = {entry.ref: entry.seq for entry in refs.entries(session.session_id)}
    assert frozen == {"m00001": 1, "m00002": 2}

    # The strict surface fold accepts the transaction.
    nodes = fold_surface(session.records, strict=True)
    assert [node.type for node in nodes] == [
        "system/message",
        "user/message",
        "user/message",
        "assistant/message",
        "tool/result",
        "user/message",
    ]


async def test_checkpoint_source_event_seqs_cover_shadowed_nodes(tmp_path: pathlib.Path) -> None:
    tool, store, _blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store)

    result = await _run(tool, session, "m00001–m00002\n\nbody")
    assert result.is_error is False

    summary_record = session.records[-3]
    checkpoint_record = session.records[-2]
    assert summary_record.type == "compaction/summary"
    assert checkpoint_record.type == "user/message"
    seqs = checkpoint_record.sourceEventSeqs
    assert seqs is not None
    assert seqs == sorted({summary_record.seq, 1, 2})
    assert all(seq < checkpoint_record.seq for seq in seqs)
    assert all(left < right for left, right in pairwise(seqs))


async def test_result_text_fingerprints_and_time_range(tmp_path: pathlib.Path) -> None:
    tool, store, _blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    body = "A" * 40 + "\n" + "B" * 120
    times = [session.records[1].time, session.records[2].time]

    result = await _run(tool, session, f"m00001–m00002 topic\n\n{body}")

    flat = body.replace("\n", " ")
    assert f'head "{flat[:HEAD_CHARS]}"' in result.text
    assert f'tail "{flat[-TAIL_CHARS:]}"' in result.text
    assert f"b1 full summary {len(body)}ch" in result.text
    assert f"[{support.local_stamp(times[0])} – {support.local_stamp(times[1])}]" in result.text


# -- multi-block / gap / surface order ----------------------------------


async def test_multi_block_keeps_gap_and_positions(tmp_path: pathlib.Path) -> None:
    tool, store, blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store, tool_exchange=False)
    content = "m00001–m00001 first\n\nFirst summary.\nm00003–m00004 second\n\nSecond summary."

    result = await _run(tool, session, content)
    assert result.is_error is False

    stored = blocks.list(session.session_id)
    assert [record.id for record in stored] == ["b1", "b2"]
    assert [(record.start_seq, record.end_seq) for record in stored] == [(1, 1), (3, 4)]

    messages = session.derive_messages()
    kinds = [(message.source or {}).get("kind") for message in messages]
    assert kinds == ["system-prompt", "compact-checkpoint", "model", "compact-checkpoint", "user"]
    assert messages[1].text().find("First summary.") >= 0
    assert messages[2].id == "msg_a1"  # the skipped gap message stays visible
    assert messages[3].text().find("Second summary.") >= 0
    assert messages[4].id == "msg_u3"

    fold_surface(session.records, strict=True)  # strict replay accepts the result

    first_checkpoint = session.records[-4]
    second_checkpoint = session.records[-2]
    assert first_checkpoint.sourceEventSeqs == sorted({session.records[-5].seq, 1})
    assert second_checkpoint.sourceEventSeqs == sorted({session.records[-3].seq, 3, 4})


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("m00001–m00002 a\n\nA\nm00003–m00003 b\n\nB", "COMPACT_ADJACENT_RANGES"),
        ("m00001–m00003 a\n\nA\nm00002–m00004 b\n\nB", "COMPACT_OVERLAP"),
        ("m00003–m00004 a\n\nA\nm00001–m00002 b\n\nB", "COMPACT_OVERLAP"),
    ],
)
async def test_range_geometry_is_validated_before_writing(
    tmp_path: pathlib.Path, content: str, code: str
) -> None:
    tool, store, blocks, refs = support.make_tool(tmp_path)
    session = support.seed_session(store, tool_exchange=False)
    before = len(session.records)

    result = await _run(tool, session, content)

    assert result.is_error is True
    assert (result.meta or {})["code"] == code
    assert len(session.records) == before
    assert blocks.list(session.session_id) == []
    assert refs.entries(session.session_id) == []


# -- tool pairing --------------------------------------------------------


async def test_range_cutting_a_tool_exchange_is_rejected(tmp_path: pathlib.Path) -> None:
    tool, store, blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    before = len(session.records)

    result = await _run(tool, session, "m00004–m00004\n\nbody")

    assert result.is_error is True
    assert (result.meta or {})["code"] == "COMPACT_SPLITS_TOOL_PAIR"
    assert len(session.records) == before
    assert blocks.list(session.session_id) == []


async def test_whole_tool_exchange_can_be_compressed(tmp_path: pathlib.Path) -> None:
    tool, store, blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store)

    result = await _run(tool, session, "m00004–m00005 tool exchange\n\nTool summary.")

    assert result.is_error is False
    stored = blocks.list(session.session_id)
    assert [(record.start_seq, record.end_seq) for record in stored] == [(4, 6)]
    messages = session.derive_messages()
    assert [message.role for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "user",
        "user",
    ]
    assert (messages[4].source or {}).get("kind") == "compact-checkpoint"
    assert messages[5].id == "msg_u3"
    fold_surface(session.records, strict=True)


# -- refs and surface guards --------------------------------------------


async def test_system_prompt_cannot_be_compressed(tmp_path: pathlib.Path) -> None:
    tool, store, _blocks, refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    refs.register(
        session.session_id,
        [RefEntry(ref="m00001", seq=0, message_id="msg_sys", assigned_at=0)],
    )

    result = await _run(tool, session, "m00001–m00001\n\nbody")

    assert result.is_error is True
    assert (result.meta or {})["code"] == "COMPACT_SYSTEM_PROMPT"


async def test_ref_out_of_range_reports_available_count(tmp_path: pathlib.Path) -> None:
    tool, store, _blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store)

    result = await _run(tool, session, "m00099–m00100\n\nbody")

    assert result.is_error is True
    assert (result.meta or {})["code"] == "COMPACT_REF_NOT_FOUND"


async def test_recompressing_a_frozen_range_is_rejected(tmp_path: pathlib.Path) -> None:
    tool, store, _blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    first = await _run(tool, session, "m00001–m00002\n\nfirst summary")
    assert first.is_error is False
    before = len(session.records)

    second = await _run(tool, session, "m00001–m00002\n\nsecond summary")

    assert second.is_error is True
    assert (second.meta or {})["code"] == "COMPACT_REF_NOT_ON_SURFACE"
    assert len(session.records) == before


# -- all messages compressed --------------------------------------------


async def test_compressing_everything_reports_minimum(tmp_path: pathlib.Path) -> None:
    tool, store, _blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store, tool_exchange=False)

    result = await _run(tool, session, "m00001–m00005 everything\n\nAll of it.")

    assert result.is_error is False
    assert result.text.rstrip().endswith(MINIMUM_STATUS)
    assert UNCOVERED_STATUS not in result.text
    messages = session.derive_messages()
    assert [message.role for message in messages] == ["system", "user"]
    assert (messages[1].source or {}).get("kind") == "compact-checkpoint"
    fold_surface(session.records, strict=True)


# -- session resolution --------------------------------------------------


async def test_missing_session_is_reported(tmp_path: pathlib.Path) -> None:
    tool, _store, _blocks, _refs = support.make_tool(tmp_path)

    result = await tool.run({"content": "m00001–m00002\n\nbody"}, support.make_ctx(None))

    assert result.is_error is True
    assert (result.meta or {})["code"] == "COMPACT_SESSION_UNAVAILABLE"


async def test_session_is_resolved_through_sessions_service(tmp_path: pathlib.Path) -> None:
    tool, store, _blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    ctx = support.make_ctx(None, session_id=session.session_id, services={"sessions": store})

    result = await tool.run({"content": "m00001–m00002\n\nbody"}, ctx)

    assert result.is_error is False
    assert store.open(session.session_id).records[-2].type == "user/message"


async def test_session_is_resolved_through_sessions_plugin(tmp_path: pathlib.Path) -> None:
    tool, store, _blocks, _refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    ctx = support.make_ctx(None, session_id=session.session_id, services={"sessions": _Sessions(store)})

    result = await tool.run({"content": "m00001–m00002\n\nbody"}, ctx)

    assert result.is_error is False


async def test_write_failure_rolls_back_blocks_and_refs(tmp_path: pathlib.Path) -> None:
    tool, store, blocks, refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    failing = _FailingSession(session, fail_after=0)
    before = len(session.records)

    result = await tool.run({"content": "m00001–m00002\n\nbody"}, support.make_ctx(failing))

    assert result.is_error is True
    assert (result.meta or {})["code"] == "COMPACT_UNEXPECTED"
    assert len(session.records) == before
    assert blocks.list(session.session_id) == []
    assert refs.entries(session.session_id) == []


async def test_parsing_failure_writes_nothing(tmp_path: pathlib.Path) -> None:
    tool, store, blocks, refs = support.make_tool(tmp_path)
    session = support.seed_session(store)
    before = len(session.records)

    result = await tool.run({"content": "not a header"}, support.make_ctx(session))

    assert result.is_error is True
    assert (result.meta or {})["code"] == "COMPACT_INVALID_CONTENT"
    assert len(session.records) == before
    assert blocks.list(session.session_id) == []
    assert refs.entries(session.session_id) == []


# -- plugin --------------------------------------------------------------


class _FakeCtx:
    """Minimal PluginContext stand-in for the plugin setup tests."""

    def __init__(self, services: dict[str, object]) -> None:
        self._services = services
        self.factories: list[object] = []

    def service(self, name: str) -> object:
        try:
            return self._services[name]
        except KeyError as exc:
            raise LookupError(name) from exc

    def register_tool(self, factory: object) -> None:
        self.factories.append(factory)


class _Sessions:
    def __init__(self, store: SessionStore) -> None:
        self.store = store


class _ConfigService:
    def __init__(self, value: object) -> None:
        self.value = value


def _plugin_ctx(store: SessionStore, global_config: object | None = None) -> _FakeCtx:
    services: dict[str, object] = {"sessions": _Sessions(store)}
    if global_config is not None:
        services["config"] = _ConfigService(global_config)
    return _FakeCtx(services)


def test_plugin_registers_tool_with_default_block_root(tmp_path: pathlib.Path) -> None:
    store, root, _blocks, _refs = support.stores(tmp_path)
    ctx = _plugin_ctx(store)
    plugin = CompressPlugin(ctx, CompressConfig())  # type: ignore[arg-type]
    plugin.setup(ctx, CompressConfig())

    assert plugin.blocks is not None
    assert plugin.blocks.root == root / "blocks"
    # One factory for compress, one for the search/decompress retrieval pair.
    assert len(ctx.factories) == 2
    [tool] = ctx.factories[0](None, None)  # type: ignore[operator]
    assert isinstance(tool, CompressTool)
    assert tool.blocks.root == root / "blocks"
    assert [item.name for item in ctx.factories[1](None, None)] == ["search_context", "decompress"]  # type: ignore[operator]


def test_plugin_row_config_overrides_blocks_dir(tmp_path: pathlib.Path) -> None:
    store, _root, _blocks, _refs = support.stores(tmp_path)
    custom = tmp_path / "custom-blocks"
    ctx = _plugin_ctx(store)
    plugin = CompressPlugin(ctx, CompressConfig(blocksDir=str(custom)))  # type: ignore[arg-type]
    plugin.setup(ctx, CompressConfig(blocksDir=str(custom)))

    assert plugin.blocks is not None
    assert plugin.blocks.root == custom


def test_plugin_global_config_disables_tool(tmp_path: pathlib.Path) -> None:
    store, _root, _blocks, _refs = support.stores(tmp_path)
    ctx = _plugin_ctx(store, {"compact": {"compressEnabled": False}})
    plugin = CompressPlugin(ctx, CompressConfig())  # type: ignore[arg-type]
    plugin.setup(ctx, CompressConfig())

    assert ctx.factories == []
    assert plugin.blocks is None


@pytest.mark.parametrize(
    "global_config",
    [
        {"compact": {"compressEnabled": "yes"}},
        {"compact": {"compressEnabled": 1}},
        {"compact": {"blocksDir": 5}},
    ],
)
def test_plugin_rejects_invalid_config(tmp_path: pathlib.Path, global_config: object) -> None:
    store, _root, _blocks, _refs = support.stores(tmp_path)
    ctx = _plugin_ctx(store, global_config)
    plugin = CompressPlugin(ctx, CompressConfig())  # type: ignore[arg-type]
    with pytest.raises(ConfigError):
        plugin.setup(ctx, CompressConfig())


def test_compress_config_model_validation() -> None:
    config = CompressConfig()
    assert config.blocksDir is None
    assert config.compressEnabled is True
    with pytest.raises(ValidationError):
        CompressConfig(blocksDir=5)  # type: ignore[arg-type]


class _FailingSession:
    """Session proxy whose append fails immediately (simulated disk error)."""

    def __init__(self, inner: Session, *, fail_after: int = 0) -> None:
        self._inner = inner
        self._fail_after = fail_after
        self._calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def append(self, type_: str, data: dict, **envelope: object) -> int:
        self._calls += 1
        if self._calls > self._fail_after:
            raise SessionError("simulated write failure")
        return self._inner.append(type_, data, **envelope)
