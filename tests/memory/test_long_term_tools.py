"""Tests for the agent-facing long-term memory tools (memory_search/memory_add)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.memory.backend import hash_content, validate_dedupe_key
from deeppyro.memory.errors import MemoryValidationError
from deeppyro.memory.service import MemoryService
from deeppyro.memory.sqlite import SCHEMA_VERSION, SqliteMemoryBackend
from deeppyro.memory.tools import (
    ADD_TOOL_NAME,
    DEFAULT_TOOL_SEARCH_LIMIT,
    MAX_RESULT_CONTENT_CHARS,
    MAX_RESULT_TOTAL_CHARS,
    MAX_SEARCH_LIMIT,
    SEARCH_TOOL_NAME,
    MemoryAddTool,
    MemorySearchTool,
    long_term_memory_tools,
)
from deeppyro.sandbox import PermissionGate
from tests.memory.helpers import FakeClock, make_store


def _service(
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
    backend: bool = True,
) -> MemoryService:
    spot = clock if clock is not None else FakeClock()
    long_term = SqliteMemoryBackend(tmp_path / "long-term.sqlite3", clock=spot) if backend else None
    return MemoryService(store=make_store(tmp_path, clock=spot), backend=long_term)


def _context(
    service: MemoryService | None,
    *,
    permission: Any = None,
    session_id: str = "session-test",
) -> ToolContext:
    services: dict[str, Any] = {} if service is None else {"memory": service}
    return ToolContext(
        cwd=Path("."),
        session_id=session_id,
        turn=0,
        step=0,
        call_id="call-test",
        cancel=asyncio.Event(),
        log=lambda event, payload=None: None,
        permission=permission,
        services=services,
    )


async def _call(
    tool: MemorySearchTool | MemoryAddTool,
    service: MemoryService | None,
    args: dict[str, Any],
    *,
    permission: Any = None,
) -> ToolResult:
    return await tool.run(args, _context(service, permission=permission))


def _payload(result: ToolResult) -> dict[str, Any]:
    parsed = json.loads(result.text)
    assert isinstance(parsed, dict)
    return parsed


def test_tool_names_and_permission_matrix() -> None:
    read_only = long_term_memory_tools(allow_writes=False)
    assert [tool.name for tool in read_only] == [SEARCH_TOOL_NAME]
    assert SEARCH_TOOL_NAME == "memory_search"

    writable = long_term_memory_tools(allow_writes=True)
    assert [tool.name for tool in writable] == [SEARCH_TOOL_NAME, ADD_TOOL_NAME]
    assert ADD_TOOL_NAME == "memory_add"
    assert writable[0].parallel_safe is True
    assert writable[1].parallel_safe is False

    search, add = writable
    assert search.parameters["required"] == ["query"]
    assert set(search.parameters["properties"]) == {"query", "limit", "tags"}
    assert add.parameters["required"] == ["content"]
    assert set(add.parameters["properties"]) == {"content", "key", "importance", "tags"}
    assert add.parameters["properties"]["importance"]["enum"] == ["critical", "normal", "low"]


async def test_search_returns_bounded_hits_with_provenance(tmp_path: Path) -> None:
    clock = FakeClock()
    service = _service(tmp_path, clock=clock)
    service.write_long_term("alpha beta fact one", source_session_id="session-a")
    clock.advance()
    service.write_long_term("alpha beta fact two", source_session_id="session-b")

    result = await _call(MemorySearchTool(), service, {"query": "alpha beta"})
    payload = _payload(result)

    assert payload["query"] == "alpha beta"
    assert payload["count"] == 2
    assert payload["truncated"] is False
    assert result.meta == {"count": 2, "truncated": False}
    assert '": ' not in result.text
    newest, oldest = payload["results"]
    assert newest["content"] == "alpha beta fact two"
    assert newest["source_session_id"] == "session-b"
    assert newest["created_at"] == "2026-01-02T03:04:06.000Z"
    assert oldest["source_session_id"] == "session-a"
    assert oldest["created_at"] == "2026-01-02T03:04:05.000Z"
    assert newest["rank"] == 1 and oldest["rank"] == 2
    assert newest["importance"] == "normal"
    assert newest["tags"] == []


async def test_search_filters_by_exact_tags(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.write_long_term("deploy checklist", tags=("release", "qa"))

    hit = await _call(MemorySearchTool(), service, {"query": "deploy", "tags": ["qa"]})
    assert _payload(hit)["count"] == 1
    assert _payload(hit)["results"][0]["tags"] == ["release", "qa"]

    miss = await _call(MemorySearchTool(), service, {"query": "deploy", "tags": ["ops"]})
    assert _payload(miss)["count"] == 0


async def test_search_truncates_long_content_and_full_pages(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for index in range(MAX_SEARCH_LIMIT + 5):
        service.write_long_term(f"bulk fact {index}")
    long_content = "needle " + "x" * (MAX_RESULT_CONTENT_CHARS * 2)
    service.write_long_term(long_content)

    page = await _call(MemorySearchTool(), service, {"query": "bulk", "limit": MAX_SEARCH_LIMIT})
    page_payload = _payload(page)
    assert page_payload["count"] == MAX_SEARCH_LIMIT
    assert page_payload["truncated"] is True

    needle = await _call(MemorySearchTool(), service, {"query": "needle"})
    needle_payload = _payload(needle)
    assert needle_payload["truncated"] is True
    assert needle_payload["count"] == 1
    clipped = needle_payload["results"][0]
    assert clipped["truncated"] is True
    assert len(clipped["content"]) == MAX_RESULT_CONTENT_CHARS


async def test_search_enforces_total_character_budget(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for index in range(10):
        service.write_long_term(f"chunky fact {index} " + "y" * 900)

    result = await _call(MemorySearchTool(), service, {"query": "chunky", "limit": 10})
    payload = _payload(result)
    total_chars = sum(len(item["content"]) for item in payload["results"])
    assert payload["truncated"] is True
    assert total_chars <= MAX_RESULT_TOTAL_CHARS
    assert payload["count"] < 10


async def test_search_limit_is_bounded_by_schema(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ToolError, match=f"limit must be less than or equal to {MAX_SEARCH_LIMIT}"):
        await _call(MemorySearchTool(), service, {"query": "anything", "limit": MAX_SEARCH_LIMIT + 1})
    with pytest.raises(ToolError, match="limit must be greater than or equal to 1"):
        await _call(MemorySearchTool(), service, {"query": "anything", "limit": 0})
    assert DEFAULT_TOOL_SEARCH_LIMIT <= MAX_SEARCH_LIMIT


async def test_add_same_key_overwrites_in_place(tmp_path: Path) -> None:
    clock = FakeClock()
    service = _service(tmp_path, clock=clock)
    add = MemoryAddTool()

    first = _payload(await _call(add, service, {"content": "deploys use uv", "key": "project/deploy"}))
    assert first["created"] is True
    assert first["key"] == "project/deploy"
    assert first["created_at"] == "2026-01-02T03:04:05.000Z"

    clock.advance()
    second = _payload(await _call(add, service, {"content": "deploys use uv sync", "key": "project/deploy"}))
    assert second["created"] is False
    assert second["id"] == first["id"]
    assert second["created_at"] == "2026-01-02T03:04:06.000Z"
    assert second["chars"] == len("deploys use uv sync")

    status = service.long_term_status()
    assert status is not None and status.count == 1
    hits = service.search_long_term("deploys")
    assert [hit.record.content for hit in hits] == ["deploys use uv sync"]


async def test_add_without_key_appends_facts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    add = MemoryAddTool()
    first = _payload(await _call(add, service, {"content": "first fact"}))
    second = _payload(await _call(add, service, {"content": "second fact"}))
    assert first["created"] is True and second["created"] is True
    assert first["id"] != second["id"]
    assert first["key"] is None
    assert service.long_term_status().count == 2  # type: ignore[union-attr]


async def test_add_records_session_provenance_and_tags(tmp_path: Path) -> None:
    service = _service(tmp_path)
    result = _payload(
        await _call(
            MemoryAddTool(),
            service,
            {"content": "release checklist", "key": "release/checklist", "tags": ["release", "qa"]},
        )
    )
    assert result["tags"] == ["release", "qa"]
    hits = service.search_long_term("release")
    assert hits[0].record.provenance.tags == ("release", "qa")
    assert hits[0].record.provenance.source_session_id == "session-test"


async def test_add_is_rejected_under_read_only_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    gate = PermissionGate("read-only")
    result = await _call(MemoryAddTool(), service, {"content": "nope"}, permission=gate)

    assert result.is_error is True
    assert result.meta == {"kind": "sandbox-unavailable"}
    assert "read-only" in _payload(result)["error"]
    assert service.long_term_status().count == 0  # type: ignore[union-attr]


async def test_missing_backend_is_diagnosable(tmp_path: Path) -> None:
    service = _service(tmp_path, backend=False)
    with pytest.raises(ToolError, match="no long-term memory backend is attached"):
        await _call(MemorySearchTool(), service, {"query": "anything"})
    with pytest.raises(ToolError, match="no long-term memory backend is attached"):
        await _call(MemoryAddTool(), service, {"content": "anything"})


async def test_missing_memory_service_is_reported() -> None:
    with pytest.raises(ToolError, match="memory service is not available in this session"):
        await _call(MemorySearchTool(), None, {"query": "anything"})


async def test_add_reports_invalid_importance(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ToolError, match="memory_add failed"):
        await _call(MemoryAddTool(), service, {"content": "fact", "importance": "urgent"})
    assert service.long_term_status().count == 0  # type: ignore[union-attr]


def test_dedupe_key_validation() -> None:
    assert validate_dedupe_key(None) is None
    assert validate_dedupe_key("  project/deploy  ") == "project/deploy"
    with pytest.raises(MemoryValidationError, match="must be a string"):
        validate_dedupe_key(42)
    with pytest.raises(MemoryValidationError, match="non-empty string"):
        validate_dedupe_key("   ")
    with pytest.raises(MemoryValidationError, match="may not exceed 200 characters"):
        validate_dedupe_key("k" * 201)


def test_backend_upsert_preserves_id_and_refreshes_provenance(tmp_path: Path) -> None:
    clock = FakeClock()
    backend = SqliteMemoryBackend(tmp_path / "long-term.sqlite3", clock=clock)
    try:
        first, created = backend.upsert("v1", dedupe_key="k", importance="low", tags=("a",))
        assert created is True
        assert first.provenance.created_at == "2026-01-02T03:04:05.000Z"

        clock.advance()
        second, created_again = backend.upsert("v2", dedupe_key="k", importance="critical", tags=("b",))

        assert created_again is False
        assert second.id == first.id
        assert second.provenance.dedupe_key == "k"
        assert second.provenance.created_at == "2026-01-02T03:04:06.000Z"
        assert second.provenance.importance == "critical"
        assert backend.count() == 1
        assert backend.status().schema_version == SCHEMA_VERSION == 2
        hits = backend.search("v2")
        assert [hit.record.content for hit in hits] == ["v2"]
        assert hits[0].record.provenance.tags == ("b",)
        assert backend.search("v1") == ()
    finally:
        backend.close()


def test_backend_upsert_requires_a_non_blank_key(tmp_path: Path) -> None:
    backend = SqliteMemoryBackend(tmp_path / "long-term.sqlite3", clock=FakeClock())
    try:
        with pytest.raises(MemoryValidationError, match="non-empty string"):
            backend.upsert("content", dedupe_key="   ")
    finally:
        backend.close()


def test_legacy_schema_one_database_upgrades_in_place(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    digest = hash_content("legacy fact")
    connection = sqlite3.connect(str(path))
    connection.executescript(
        f"""
        CREATE TABLE memory_records (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            source_session_id TEXT,
            source_message_id TEXT,
            run_id TEXT,
            created_at TEXT NOT NULL,
            importance TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]'
        );
        CREATE INDEX memory_records_created_at ON memory_records (created_at);
        CREATE VIRTUAL TABLE memory_search USING fts5 (
            memory_id UNINDEXED,
            body,
            tags,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        INSERT INTO memory_records VALUES (
            'mem_0123456789abcdef', 'legacy fact', '{digest}', NULL, NULL, NULL,
            '2026-01-02T03:04:05.000Z', 'normal', '[]'
        );
        INSERT INTO memory_search (memory_id, body, tags)
            VALUES ('mem_0123456789abcdef', 'legacy fact', '');
        PRAGMA user_version = 1;
        """
    )
    connection.commit()
    connection.close()

    backend = SqliteMemoryBackend(path, clock=FakeClock())
    try:
        assert backend.schema_version == SCHEMA_VERSION
        assert backend.count() == 1
        hits = backend.search("legacy")
        assert [hit.record.content for hit in hits] == ["legacy fact"]
        assert hits[0].record.provenance.dedupe_key is None

        record, created = backend.upsert("legacy fact refreshed", dedupe_key="legacy")
        assert created is True
        assert backend.count() == 2
        assert record.provenance.dedupe_key == "legacy"
    finally:
        backend.close()
