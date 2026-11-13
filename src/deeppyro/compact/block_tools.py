"""Model-facing retrieval tools over the compress block store.

Compression replaces consumed conversation ranges with summaries.  The model
can still need a detail from one of those ranges, so two small read-only tools
expose the block store back to it:

* search_context finds the most recent blocks whose summary (or ref range /
  title / creation time) contains a keyword, and shows a bounded excerpt;
* decompress returns the full summary of one already-known block id.

Both tools are deliberately narrow: they only read the current session's block
file, never touch the session log, never search across sessions, and never
return the original (uncompressed) messages — the block store only holds the
summaries by design.  Reads go through BlockStore, which tolerates torn lines,
so a damaged store degrades to "no match" rather than an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from deeppyro.compact.blocks import BlockRecord, BlockStore
from deeppyro.compact.compress_tool import format_local
from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.args import optional_int, require_str
from deeppyro.tools.base import BaseTool, make_parameters

#: Maximum size of a search result text in bytes; longer results are truncated.
MAX_TEXT_BYTES = 8192
#: Characters of the summary shown as the one-line preview.
PREVIEW_CHARS = 120
#: Maximum characters of each excerpt shown around a hit.
EXCERPT_CHARS = 200
DEFAULT_LIMIT = 5
MAX_LIMIT = 20

TRUNCATION_NOTE = "\n…[truncated to 8KB — narrow the query or lower limit]"

SEARCH_DESCRIPTION = (
    "Search the full summaries of compressed context blocks in this session by keyword "
    "(case-insensitive). Use this when you need a detail from a range that was compacted away: "
    "it returns the most recent matching blocks with a short excerpt. Call decompress only if the "
    "excerpt is not enough."
)
DECOMPRESS_DESCRIPTION = (
    "Return the complete summary text of one compressed context block (for example b3). Use only "
    "when the search_context excerpt is not enough: the block store keeps summaries, not the "
    "original messages, so this is the fullest record of a compressed range."
)

_SEARCH_PARAMETERS = make_parameters(
    {
        "query": {
            "type": "string",
            "description": "Keyword or phrase to match in block summaries; case-insensitive.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "description": "Maximum number of blocks to return, most recent first (default 5).",
        },
    },
    required=["query"],
)

_DECOMPRESS_PARAMETERS = make_parameters(
    {
        "block_id": {
            "type": "string",
            "description": "Block id to expand, for example b3.",
        }
    },
    required=["block_id"],
)


def _flatten(text: str) -> str:
    """Collapse all whitespace runs to single spaces for excerpts."""
    return " ".join(text.split())


def _service_session_id(ctx: ToolContext) -> str | None:
    """Return the live session id exposed through ToolContext.services, if any.

    The runtime may inject the current session as services["session"] or
    through a sessions service with a .session/.current attribute.  A candidate
    whose id contradicts ctx.session_id is ignored, because the block files are
    addressed by the session that owns the call.
    """
    services = getattr(ctx, "services", None)
    context_id = getattr(ctx, "session_id", "")
    context_id = context_id if isinstance(context_id, str) else ""
    if not isinstance(services, dict):
        return None
    for key in ("session", "current_session", "agent_session"):
        candidate = services.get(key)
        session_id = getattr(candidate, "session_id", None)
        if session_id and isinstance(session_id, str) and (not context_id or session_id == context_id):
            return session_id
    sessions = services.get("sessions")
    if sessions is not None:
        for attribute in ("session", "current", "current_session"):
            candidate = getattr(sessions, attribute, None)
            session_id = getattr(candidate, "session_id", None)
            if session_id and isinstance(session_id, str) and (not context_id or session_id == context_id):
                return session_id
    return None


def _current_session_id(ctx: ToolContext) -> str:
    """Resolve the session whose block file should be read.

    The block store is addressed by session id, so opening the (possibly huge)
    session log is unnecessary.  The live session from services wins; otherwise
    the context's own session id is used.  Without either there is nothing to
    search.
    """
    session_id = _service_session_id(ctx)
    if session_id is not None:
        return session_id
    context_id = getattr(ctx, "session_id", "")
    if isinstance(context_id, str) and context_id:
        return context_id
    raise ToolError("no session is available: the runtime must expose the current session")


@dataclass(frozen=True, slots=True)
class _Hit:
    """One matching block plus where the query matched."""

    block: BlockRecord
    where: str
    position: int | None


def _find_hit(block: BlockRecord, needle: str) -> _Hit | None:
    """Locate needle in a block, preferring the summary for excerpts."""
    summary = _flatten(block.summary)
    position = summary.casefold().find(needle)
    if position >= 0:
        return _Hit(block=block, where="summary", position=position)
    range_text = f"{block.start_ref}–{block.end_ref}"
    if needle in range_text.casefold() or needle in range_text.replace("–", "-").casefold():
        return _Hit(block=block, where="range", position=None)
    if needle in block.title.casefold():
        return _Hit(block=block, where="title", position=None)
    if needle in format_local(block.created_at).casefold():
        return _Hit(block=block, where="created", position=None)
    return None


def _excerpts(summary: str, position: int | None) -> tuple[str, str]:
    """Return (head, tail) excerpts, each at most EXCERPT_CHARS characters."""
    if position is None:
        return summary[:EXCERPT_CHARS], summary[-EXCERPT_CHARS:]
    start = max(0, position - EXCERPT_CHARS // 2)
    head = summary[start : position + EXCERPT_CHARS // 2]
    tail = summary[position : position + EXCERPT_CHARS]
    return head[:EXCERPT_CHARS], tail[:EXCERPT_CHARS]


def _hit_lines(hit: _Hit) -> list[str]:
    """Format one search hit as 2-3 text lines."""
    block = hit.block
    summary = _flatten(block.summary)
    preview = summary[:PREVIEW_CHARS] + ("…" if len(summary) > PREVIEW_CHARS else "")
    lines = [f"{block.start_ref}–{block.end_ref} · {block.id} · {preview}"]
    head, tail = _excerpts(summary, hit.position)
    if hit.where == "summary":
        lines.append(f'   head "{head}" … tail "{tail}"')
    elif head or tail:
        lines.append(f'   matched {hit.where}; head "{head}" … tail "{tail}"')
    else:
        lines.append(f"   matched {hit.where}")
    details = [f"{len(block.summary)} chars", format_local(block.created_at)]
    if block.title:
        details.insert(0, block.title)
    lines.append("   " + " · ".join(details))
    return lines


def _render(header: str, groups: list[list[str]]) -> str:
    """Join the header and whole hit groups (never split a hit across the cut)."""
    lines = [line for group in groups for line in group]
    if not lines:
        return header
    return header + "\n" + "\n".join(lines)


def _truncate(groups: list[list[str]], header: str) -> tuple[str, int, bool]:
    """Render header and hit groups under MAX_TEXT_BYTES, dropping tail groups.

    Returns (text, kept_group_count, truncated).  Truncation happens on whole
    hits so the model never sees a half-formatted entry.
    """
    kept = list(groups)
    text = _render(header, kept)
    if len(text.encode("utf-8")) <= MAX_TEXT_BYTES:
        return text, len(kept), False
    while kept:
        kept.pop()
        text = _render(header, kept)
        if len((text + TRUNCATION_NOTE).encode("utf-8")) <= MAX_TEXT_BYTES:
            return text + TRUNCATION_NOTE, len(kept), True
    return header + TRUNCATION_NOTE, 0, True


class SearchContextTool(BaseTool):
    """Search compressed block summaries for a keyword."""

    name = "search_context"
    description = SEARCH_DESCRIPTION
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = _SEARCH_PARAMETERS

    def __init__(self, blocks: BlockStore) -> None:
        self.blocks = blocks

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Validate the query, scan this session's blocks, and format hits."""
        query = require_str(args, "query").strip()
        limit = optional_int(args, "limit", DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
        session_id = _current_session_id(ctx)
        needle = query.casefold()
        hits: list[_Hit] = []
        for block in self.blocks.list(session_id):
            hit = _find_hit(block, needle)
            if hit is not None:
                hits.append(hit)
        display = query if len(query) <= 200 else query[:200] + "…"
        if not hits:
            return ToolResult(
                text=f'No compressed blocks matched "{display}".',
                meta={"query": query, "blocks": [], "count": 0, "total": 0, "truncated": False},
            )
        hits.sort(key=lambda hit: (hit.block.start_seq, int(hit.block.id[1:])), reverse=True)
        total = len(hits)
        selected = hits[:limit]
        if total > len(selected):
            header = (
                f'Found {total} compressed block(s) matching "{display}" '
                f"(showing {len(selected)} most recent)."
            )
        else:
            header = f'Found {total} compressed block(s) matching "{display}".'
        groups = [_hit_lines(hit) for hit in selected]
        text, kept, truncated = _truncate(groups, header)
        return ToolResult(
            text=text,
            meta={
                "query": query,
                "blocks": [hit.block.id for hit in selected[:kept]],
                "count": kept,
                "total": total,
                "truncated": truncated,
            },
        )


class DecompressTool(BaseTool):
    """Return the full summary of one compressed block."""

    name = "decompress"
    description = DECOMPRESS_DESCRIPTION
    parallel_safe = True
    parameters: ClassVar[dict[str, Any]] = _DECOMPRESS_PARAMETERS

    def __init__(self, blocks: BlockStore) -> None:
        self.blocks = blocks

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Look up one block and return its summary verbatim."""
        block_id = require_str(args, "block_id").strip()
        session_id = _current_session_id(ctx)
        block = self.blocks.get(session_id, block_id)
        if block is None:
            raise ToolError(f"block not found: {block_id}")
        header = f"{block.id} · {block.start_ref}–{block.end_ref} · full summary"
        return ToolResult(
            text=header + "\n\n" + block.summary,
            meta={
                "block": block.id,
                "range": {"start": block.start_ref, "end": block.end_ref},
                "startSeq": block.start_seq,
                "endSeq": block.end_seq,
                "createdAt": block.created_at,
            },
        )


__all__ = [
    "DECOMPRESS_DESCRIPTION",
    "DEFAULT_LIMIT",
    "EXCERPT_CHARS",
    "MAX_LIMIT",
    "MAX_TEXT_BYTES",
    "PREVIEW_CHARS",
    "SEARCH_DESCRIPTION",
    "TRUNCATION_NOTE",
    "DecompressTool",
    "SearchContextTool",
]
