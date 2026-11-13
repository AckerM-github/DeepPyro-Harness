"""Tests for the search_context and decompress retrieval tools."""

from __future__ import annotations

import pathlib
import re

import pytest

from deeppyro.builtin.compress import CompressConfig, CompressPlugin
from deeppyro.compact.block_tools import (
    DEFAULT_LIMIT,
    EXCERPT_CHARS,
    MAX_LIMIT,
    MAX_TEXT_BYTES,
    TRUNCATION_NOTE,
    DecompressTool,
    SearchContextTool,
)
from deeppyro.compact.blocks import BlockStore
from deeppyro.compact.compress_tool import format_local
from deeppyro.core.errors import ToolError
from deeppyro.session import SessionStore
from tests.compact import support

SESSION = "session-block-tools"
CREATED = 1_700_000_000_000


def _add(
    blocks: BlockStore,
    *,
    session_id: str = SESSION,
    start_ref: str = "m00001",
    end_ref: str = "m00005",
    start_seq: int = 1,
    end_seq: int = 5,
    title: str = "",
    summary: str = "Summary of the consumed work.",
    created_at: int = CREATED,
):
    return blocks.add(
        session_id,
        start_ref=start_ref,
        end_ref=end_ref,
        start_seq=start_seq,
        end_seq=end_seq,
        title=title,
        summary=summary,
        created_at=created_at,
    )


def _ctx(services=None, session=None, *, session_id: str | None = None):
    kwargs = {}
    if services is not None:
        kwargs = {"services": services}
    if session is not None:
        return support.make_ctx(session, **kwargs)
    if session_id is not None:
        return support.make_ctx(session_id=session_id, **kwargs)
    return support.make_ctx(**kwargs)


async def _search(tool: SearchContextTool, ctx, **args):
    return await tool.run(args, ctx)


# -- schemas -------------------------------------------------------------


def test_search_schema() -> None:
    tool = SearchContextTool(BlockStore(pathlib.Path("unused")))
    assert tool.name == "search_context"
    assert tool.parallel_safe is True
    schema = tool.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"query", "limit"}
    assert schema["properties"]["query"]["type"] == "string"
    limit = schema["properties"]["limit"]
    assert limit["type"] == "integer"
    assert limit["minimum"] == 1
    assert limit["maximum"] == MAX_LIMIT


def test_decompress_schema() -> None:
    tool = DecompressTool(BlockStore(pathlib.Path("unused")))
    assert tool.name == "decompress"
    assert tool.parallel_safe is True
    schema = tool.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["block_id"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"block_id"}
    assert schema["properties"]["block_id"]["type"] == "string"


# -- query validation ----------------------------------------------------


async def test_search_requires_query(tmp_path: pathlib.Path) -> None:
    tool = SearchContextTool(BlockStore(tmp_path / "blocks"))
    with pytest.raises(ToolError, match="query is required"):
        await _search(tool, _ctx(session_id=SESSION))


async def test_search_rejects_blank_query(tmp_path: pathlib.Path) -> None:
    tool = SearchContextTool(BlockStore(tmp_path / "blocks"))
    with pytest.raises(ToolError, match="non-empty"):
        await _search(tool, _ctx(session_id=SESSION), query="   ")


async def test_search_rejects_non_string_query(tmp_path: pathlib.Path) -> None:
    tool = SearchContextTool(BlockStore(tmp_path / "blocks"))
    with pytest.raises(ToolError, match="query must be a string"):
        await _search(tool, _ctx(session_id=SESSION), query=5)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (0, "greater than or equal to 1"),
        (21, "less than or equal to 20"),
        ("3", "limit must be an integer"),
        (True, "limit must be an integer"),
    ],
)
async def test_search_rejects_invalid_limit(tmp_path: pathlib.Path, value: object, message: str) -> None:
    tool = SearchContextTool(BlockStore(tmp_path / "blocks"))
    with pytest.raises(ToolError, match=message):
        await _search(tool, _ctx(session_id=SESSION), query="deploy", limit=value)


async def test_search_without_session_raises(tmp_path: pathlib.Path) -> None:
    tool = SearchContextTool(BlockStore(tmp_path / "blocks"))
    with pytest.raises(ToolError, match="no session is available"):
        await _search(tool, _ctx(session_id=""), query="deploy")


# -- search results ------------------------------------------------------


async def test_search_with_no_blocks(tmp_path: pathlib.Path) -> None:
    tool = SearchContextTool(BlockStore(tmp_path / "blocks"))
    result = await _search(tool, _ctx(session_id=SESSION), query="windows")
    assert result.is_error is False
    assert result.text == 'No compressed blocks matched "windows".'
    assert result.meta == {"query": "windows", "blocks": [], "count": 0, "total": 0, "truncated": False}


async def test_search_missing_store_root_is_empty(tmp_path: pathlib.Path) -> None:
    tool = SearchContextTool(BlockStore(tmp_path / "does-not-exist"))
    result = await _search(tool, _ctx(session_id=SESSION), query="anything")
    assert result.text == 'No compressed blocks matched "anything".'


async def test_search_is_case_insensitive_and_shows_excerpt(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, title="Crash triage", summary="The Windows crash came from a stale driver handle.")
    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="WINDOWS CRASH")

    assert result.is_error is False
    assert result.text.startswith('Found 1 compressed block(s) matching "WINDOWS CRASH".')
    assert "m00001–m00005 · b1 · The Windows crash came from a stale driver handle." in result.text
    assert 'head "' in result.text and '… tail "' in result.text
    assert "Crash triage" in result.text
    assert result.meta["blocks"] == ["b1"]
    assert result.meta["total"] == 1
    assert result.meta["truncated"] is False


async def test_search_reports_no_match(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, summary="A completely unrelated summary about networking.")
    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="driver")
    assert result.text == 'No compressed blocks matched "driver".'
    assert result.meta["count"] == 0
    assert result.meta["total"] == 0


async def test_search_returns_most_recent_first(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, start_ref="m00001", end_ref="m00005", start_seq=10, end_seq=20, summary="deploy plan v1")
    _add(blocks, start_ref="m00020", end_ref="m00025", start_seq=50, end_seq=60, summary="deploy plan v2")
    _add(blocks, start_ref="m00040", end_ref="m00045", start_seq=90, end_seq=100, summary="deploy plan v3")

    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="deploy plan")

    assert result.meta["blocks"] == ["b3", "b2", "b1"]
    assert result.text.index("b3") < result.text.index("b2") < result.text.index("b1")


async def test_search_limit_keeps_the_most_recent(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, start_seq=10, end_seq=20, summary="migration notes alpha")
    _add(blocks, start_seq=50, end_seq=60, summary="migration notes beta")
    _add(blocks, start_seq=90, end_seq=100, summary="migration notes gamma")

    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="migration", limit=1)

    assert "(showing 1 most recent)" in result.text
    assert result.meta["count"] == 1
    assert result.meta["total"] == 3
    assert result.meta["blocks"] == ["b3"]


async def test_search_default_limit_is_five(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    for index in range(7):
        _add(
            blocks,
            start_seq=index * 10,
            end_seq=index * 10 + 5,
            summary=f"recurring needle number {index}",
        )

    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="needle")

    assert DEFAULT_LIMIT == 5
    assert result.meta["count"] == 5
    assert result.meta["total"] == 7
    assert result.meta["blocks"] == ["b7", "b6", "b5", "b4", "b3"]


async def test_search_accepts_max_limit(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    for index in range(7):
        _add(blocks, start_seq=index * 10, end_seq=index * 10 + 5, summary=f"needle {index}")

    result = await _search(
        SearchContextTool(blocks), _ctx(session_id=SESSION), query="needle", limit=MAX_LIMIT
    )

    assert result.meta["count"] == 7
    assert result.meta["total"] == 7


@pytest.mark.parametrize("query", ["m00040", "m00040-m00045", "M00040–M00045"])
async def test_search_matches_ref_range(tmp_path: pathlib.Path, query: str) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, start_ref="m00040", end_ref="m00045", summary="A summary with no keyword inside.")
    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query=query)
    assert "matched range" in result.text
    assert result.meta["blocks"] == ["b1"]


async def test_search_matches_title(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, title="Database migration", summary="A summary with no keyword inside.")
    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="database migration")
    assert "matched title" in result.text


async def test_search_matches_created_time(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, summary="A summary with no keyword inside.", created_at=CREATED)
    stamp = format_local(CREATED)
    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query=stamp[:10])
    assert "matched created" in result.text
    assert stamp in result.text


async def test_search_excerpts_are_bounded(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, summary="A" * 400 + "NEEDLE" + "B" * 400)

    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="needle")

    match = re.search(r'head "(.*?)" … tail "(.*?)"', result.text, re.DOTALL)
    assert match is not None
    assert len(match.group(1)) <= EXCERPT_CHARS
    assert len(match.group(2)) <= EXCERPT_CHARS


async def test_search_text_is_capped_at_eight_kb(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    for index in range(20):
        _add(
            blocks,
            start_seq=index * 10,
            end_seq=index * 10 + 5,
            summary=("needle " + "x" * 2000 + "\n") * 2,
        )

    result = await _search(
        SearchContextTool(blocks), _ctx(session_id=SESSION), query="needle", limit=MAX_LIMIT
    )

    assert result.meta["truncated"] is True
    assert result.meta["count"] < 20
    assert TRUNCATION_NOTE in result.text
    assert len(result.text.encode("utf-8")) <= MAX_TEXT_BYTES


async def test_search_ignores_other_sessions_blocks(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, session_id="session-other", summary="needle in another session")
    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="needle")
    assert result.text == 'No compressed blocks matched "needle".'


async def test_search_tolerates_corrupt_block_line(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, summary="needle survives corruption")
    path = blocks.path(SESSION)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")

    result = await _search(SearchContextTool(blocks), _ctx(session_id=SESSION), query="needle")

    assert result.meta["blocks"] == ["b1"]
    assert "needle survives corruption" in result.text


# -- session resolution --------------------------------------------------


async def test_search_uses_live_session_from_services(tmp_path: pathlib.Path) -> None:
    store, _root, blocks, _refs = support.stores(tmp_path)
    session = support.seed_session(store, session_id=SESSION)
    _add(blocks, session_id=session.session_id, summary="needle via live session")

    result = await _search(SearchContextTool(blocks), _ctx(session=session), query="needle")

    assert result.meta["blocks"] == ["b1"]


class _Sessions:
    def __init__(self, store: SessionStore) -> None:
        self.store = store


async def test_search_uses_sessions_service_with_context_id(tmp_path: pathlib.Path) -> None:
    store, _root, blocks, _refs = support.stores(tmp_path)
    _add(blocks, summary="needle via sessions service")
    ctx = _ctx(session_id=SESSION, services={"sessions": _Sessions(store)})

    result = await _search(SearchContextTool(blocks), ctx, query="needle")

    assert result.meta["blocks"] == ["b1"]


async def test_search_ignores_service_session_with_other_id(tmp_path: pathlib.Path) -> None:
    store, _root, blocks, _refs = support.stores(tmp_path)
    other = support.seed_session(store, session_id="session-other")
    _add(blocks, summary="needle for the context session")
    ctx = _ctx(session_id=SESSION, services={"session": other})

    result = await _search(SearchContextTool(blocks), ctx, query="needle")

    assert result.meta["blocks"] == ["b1"]


# -- decompress ----------------------------------------------------------


async def test_decompress_returns_full_summary(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    summary = "Full detail: " + "detail " * 500
    _add(blocks, title="Long investigation", summary=summary)

    result = await DecompressTool(blocks).run({"block_id": "b1"}, _ctx(session_id=SESSION))

    assert result.is_error is False
    assert result.text.startswith("b1 · m00001–m00005 · full summary\n\n")
    assert result.text.endswith(summary)
    assert len(result.text) > MAX_TEXT_BYTES // 4
    assert result.meta == {
        "block": "b1",
        "range": {"start": "m00001", "end": "m00005"},
        "startSeq": 1,
        "endSeq": 5,
        "createdAt": CREATED,
    }


async def test_decompress_unknown_block_raises(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    with pytest.raises(ToolError, match="block not found: b9"):
        await DecompressTool(blocks).run({"block_id": "b9"}, _ctx(session_id=SESSION))


async def test_decompress_requires_block_id(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    with pytest.raises(ToolError, match="block_id is required"):
        await DecompressTool(blocks).run({}, _ctx(session_id=SESSION))


async def test_decompress_does_not_see_other_session_blocks(tmp_path: pathlib.Path) -> None:
    blocks = BlockStore(tmp_path / "blocks")
    _add(blocks, session_id="session-other", summary="other session summary")
    with pytest.raises(ToolError, match="block not found: b1"):
        await DecompressTool(blocks).run({"block_id": "b1"}, _ctx(session_id=SESSION))


async def test_decompress_uses_live_session(tmp_path: pathlib.Path) -> None:
    store, _root, blocks, _refs = support.stores(tmp_path)
    session = support.seed_session(store, session_id=SESSION)
    _add(blocks, session_id=session.session_id, summary="live session summary")

    result = await DecompressTool(blocks).run({"block_id": "b1"}, _ctx(session=session))

    assert "live session summary" in result.text


# -- plugin registration -------------------------------------------------


class _FakeCtx:
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


def test_plugin_registers_retrieval_tools(tmp_path: pathlib.Path) -> None:
    store = SessionStore(root=tmp_path / "sessions")
    ctx = _FakeCtx({"sessions": _Sessions(store)})
    plugin = CompressPlugin(ctx, CompressConfig())  # type: ignore[arg-type]
    plugin.setup(ctx, CompressConfig())

    assert len(ctx.factories) == 2
    tools = [tool for factory in ctx.factories for tool in factory(None, None)]  # type: ignore[operator]
    assert [tool.name for tool in tools] == ["compress", "search_context", "decompress"]
    blocks_root = store.root / "blocks"
    for tool in tools:
        assert tool.blocks.root == blocks_root
