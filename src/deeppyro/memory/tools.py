"""Agent-facing long-term memory tools.

Two tools wire the long-term SQLite memory surface into the model-facing tool
registry without ever touching the database directly: both go through
:class:`deeppyro.memory.service.MemoryService`, which the memory plugin provides
as the "memory" service. Retrieval returns bounded JSON as a tool result (never
in the system prompt) so the provider cache prefix stays stable, and writes carry
provenance back to the native session that issued them.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.memory.errors import MemoryStoreError
from deeppyro.memory.service import MemoryService
from deeppyro.memory.types import DEFAULT_IMPORTANCE, IMPORTANCES
from deeppyro.tools.base import BaseTool
from deeppyro.tools.params import (
    NonEmptyStr,
    OptionalNonEmptyStr,
    OptionalStrippedStr,
    ToolArgs,
    schema_enum,
)
from deeppyro.tools.permissions import gate_for

#: Model-facing tool names.
SEARCH_TOOL_NAME = "memory_search"
ADD_TOOL_NAME = "memory_add"

#: Hits returned by default, and the hard per-call bound the schema advertises.
DEFAULT_TOOL_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 20

#: Character budgets that keep one search result small enough for a tool turn.
MAX_RESULT_CONTENT_CHARS = 1000
MAX_RESULT_TOTAL_CHARS = 8000


def _json(payload: object) -> str:
    """Render one tool payload as compact JSON (no wasted tokens)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _service(ctx: ToolContext) -> MemoryService:
    """Return the memory service this session was given."""
    service = (ctx.services or {}).get("memory")
    if not isinstance(service, MemoryService):
        raise ToolError("memory service is not available in this session")
    return service


def _bounded_results(hits: tuple[Any, ...]) -> tuple[list[dict[str, Any]], bool]:
    """Project search hits into bounded JSON entries, clipping content and count."""
    results: list[dict[str, Any]] = []
    truncated = False
    total_chars = 0
    for hit in hits:
        record = hit.record
        content = record.content
        item_truncated = len(content) > MAX_RESULT_CONTENT_CHARS
        shown = content[:MAX_RESULT_CONTENT_CHARS] if item_truncated else content
        if results and total_chars + len(shown) > MAX_RESULT_TOTAL_CHARS:
            truncated = True
            break
        total_chars += len(shown)
        if item_truncated:
            truncated = True
        results.append(
            {
                "id": record.id,
                "content": shown,
                "created_at": record.provenance.created_at,
                "importance": record.provenance.importance,
                "tags": list(record.provenance.tags),
                "rank": hit.rank,
                "score": hit.score,
                "source_session_id": record.provenance.source_session_id,
                "source_message_id": record.provenance.source_message_id,
                "run_id": record.provenance.run_id,
                "truncated": item_truncated,
            }
        )
    return results, truncated


class MemorySearchTool(BaseTool):
    """Search long-term memory and return bounded, provenance-carrying hits."""

    name = SEARCH_TOOL_NAME
    description = (
        "Search long-term memory (facts recorded in earlier sessions) for one full-text query. "
        "Returns a bounded JSON list of hits with provenance and created_at time; "
        "results are ranked and clipped, so treat a truncated hit as a pointer, not the full fact."
    )
    parallel_safe = True

    class Args(ToolArgs):
        query: NonEmptyStr = Field(description="Full-text query; whitespace-separated terms are ANDed.")
        limit: int | None = Field(
            default=None,
            ge=1,
            le=MAX_SEARCH_LIMIT,
            description=f"Maximum hits to return (default {DEFAULT_TOOL_SEARCH_LIMIT}, max {MAX_SEARCH_LIMIT}).",
        )
        tags: list[str] | None = Field(
            default=None,
            description="Optional tags every hit must carry (exact match).",
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed = MemorySearchTool.Args.parse(args)
        limit = parsed.limit if parsed.limit is not None else DEFAULT_TOOL_SEARCH_LIMIT
        service = _service(ctx)
        try:
            hits = service.search_long_term(parsed.query, limit=limit, tags=parsed.tags or ())
        except MemoryStoreError as exc:
            raise ToolError(f"memory_search failed: {exc}") from exc
        results, bounded_truncated = _bounded_results(hits)
        # A full page of hits may hide more matches, so it also counts as truncated.
        truncated = bounded_truncated or len(hits) >= limit
        payload = {
            "query": parsed.query,
            "count": len(results),
            "truncated": truncated,
            "results": results,
        }
        return ToolResult(
            text=_json(payload),
            meta={"count": len(results), "truncated": truncated},
        )


class MemoryAddTool(BaseTool):
    """Add or overwrite one long-term memory fact."""

    name = ADD_TOOL_NAME
    description = (
        "Record one durable long-term memory fact so it can be found later with memory_search. "
        "Pass a stable key to update a fact in place (the same key overwrites the earlier record); "
        "omit it to append a new fact. Writes carry the current session as provenance."
    )
    parallel_safe = False

    class Args(ToolArgs):
        content: NonEmptyStr = Field(
            description="The fact to remember; keep it one self-contained sentence or two."
        )
        key: OptionalStrippedStr = Field(
            default=None,
            description="Stable dedupe key (e.g. 'project/command'); the same key overwrites the earlier fact.",
        )
        importance: OptionalNonEmptyStr = Field(
            default=None,
            json_schema_extra=schema_enum(*IMPORTANCES),
            description="Optional importance: one of critical, normal, low.",
        )
        tags: list[str] | None = Field(
            default=None,
            description="Optional tags for exact-match filtering in memory_search.",
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed = MemoryAddTool.Args.parse(args)
        gate = gate_for(ctx)
        if getattr(gate, "preset", "") == "read-only":
            return ToolResult(
                text=_json({"error": "memory_add is unavailable under read-only policy"}),
                is_error=True,
                meta={"kind": "sandbox-unavailable"},
            )
        service = _service(ctx)
        try:
            receipt = service.upsert_long_term(
                parsed.content,
                key=parsed.key,
                source_session_id=ctx.session_id,
                importance=parsed.importance or DEFAULT_IMPORTANCE,
                tags=parsed.tags or (),
            )
        except MemoryStoreError as exc:
            raise ToolError(f"memory_add failed: {exc}") from exc
        record = receipt.record
        payload = {
            "id": record.id,
            "key": record.provenance.dedupe_key,
            "created": receipt.created,
            "created_at": record.provenance.created_at,
            "importance": record.provenance.importance,
            "tags": list(record.provenance.tags),
            "chars": len(record.content),
        }
        return ToolResult(
            text=_json(payload),
            meta={
                "id": record.id,
                "created": receipt.created,
                "key": record.provenance.dedupe_key,
            },
        )


def long_term_memory_tools(*, allow_writes: bool) -> list[BaseTool]:
    """Return the long-term memory tools available under one permission preset.

    Read-only presets get retrieval only; write access is registered only for
    workspace-write and wider presets. The write tool additionally re-checks the
    live session gate because the registration-time preset may be a default.
    """
    tools: list[BaseTool] = [MemorySearchTool()]
    if allow_writes:
        tools.append(MemoryAddTool())
    return tools


__all__ = [
    "ADD_TOOL_NAME",
    "DEFAULT_TOOL_SEARCH_LIMIT",
    "MAX_RESULT_CONTENT_CHARS",
    "MAX_RESULT_TOTAL_CHARS",
    "MAX_SEARCH_LIMIT",
    "SEARCH_TOOL_NAME",
    "MemoryAddTool",
    "MemorySearchTool",
    "long_term_memory_tools",
]
