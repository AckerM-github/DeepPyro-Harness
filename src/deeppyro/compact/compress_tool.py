"""The compress tool: model-authored summaries for consumed context ranges.

The model sends ONE content string holding every range to compress.  Each range
starts with a header line such as::

    m00005–m00072 主题

followed by that range's summary.  The tool resolves the refs, validates the
ranges against the current surface, stores the summaries as blocks, freezes the
refs, and only then writes the durable transaction:

    compaction/start -> compaction/summary (per block) -> user/message checkpoint
    (surfaceOp replace, per block) -> compaction/end

Each checkpoint replaces exactly its own contiguous range, so gaps between
blocks stay visible.  All validation happens before the first append: a
rejected call never writes a session event.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from deeppyro.compact.blocks import BlockRecord, BlockStore, CompactError
from deeppyro.compact.prompts import frame_summary
from deeppyro.compact.refs import RefStore, resolve_refs
from deeppyro.compact.refs import ref_ordinal as _ref_ordinal
from deeppyro.compact.tokens import estimate_message_tokens, estimate_messages_tokens
from deeppyro.core.errors import SessionError
from deeppyro.core.ids import new_message_id
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.core.wire import LogRecord, Message
from deeppyro.session.derive import event_message, fold_surface
from deeppyro.tools.base import BaseTool, make_parameters

HEADER_RE = re.compile(r"^m(\d{1,6})\s*[–—-]\s*m?(\d{1,6})(?:\s+(.*))?$")

HEAD_CHARS = 30
TAIL_CHARS = 100

MINIMUM_STATUS = (
    "No compressible ranges remain — the context is already at its minimum; "
    "continue the task without compressing."
)
UNCOVERED_STATUS = (
    "Uncovered history remains — the ranges you skipped stay visible; "
    "compress them in a later call if they are consumed."
)
FINGERPRINT_NOTE = (
    "Fingerprints show head/tail only — every summary is stored in full. "
    "To retrieve a specific detail from a new block, call search_context({ query }) "
    "first; decompress only if the excerpt doesn't answer it."
)

DESCRIPTION = (
    "Replace consumed conversation ranges with self-contained summaries you write, "
    "identified by their refs. content = ONE plain string holding ALL ranges — each block "
    "starts with its 'm00150–m00220 optional topic' header line followed by that block's "
    "summary. Batch multiple ranges into ONE call — do not split into one call per range. "
    "Use when content is genuinely consumed and the details no longer need to stay verbatim."
)


@dataclass(frozen=True, slots=True)
class CompressBlock:
    """One parsed header plus its summary body."""

    start_ref: str
    end_ref: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class _Range:
    """A validated block with resolved seqs and token accounting."""

    block: CompressBlock
    block_id: str | None
    start_ref: str
    end_ref: str
    start_seq: int
    end_seq: int
    start_index: int
    end_index: int
    shadowed_seqs: tuple[int, ...]
    shadowed_tokens: float
    start_time: int
    end_time: int
    saved_tokens: int


def parse_content(content: str) -> list[CompressBlock]:
    """Split a compress payload into blocks; raises CompactError when malformed."""
    if not isinstance(content, str) or not content.strip():
        raise CompactError(
            "content must be a non-empty string of range headers and summaries",
            code="COMPACT_INVALID_CONTENT",
        )
    blocks: list[CompressBlock] = []
    header: re.Match[str] | None = None
    title = ""
    body_lines: list[str] = []
    for line in content.splitlines():
        candidate = HEADER_RE.match(line.strip())
        if candidate is not None:
            if header is not None:
                blocks.append(_finish_block(header, title, body_lines))
            elif any(part.strip() for part in body_lines):
                raise CompactError(
                    "content must start with a range header like 'm00005–m00072 topic'",
                    code="COMPACT_INVALID_CONTENT",
                )
            header = candidate
            title = (candidate.group(3) or "").strip()
            body_lines = []
            continue
        if header is None:
            if line.strip():
                raise CompactError(
                    "content must start with a range header like 'm00005–m00072 topic'",
                    code="COMPACT_INVALID_CONTENT",
                )
            continue
        body_lines.append(line)
    if header is None:
        raise CompactError(
            "content must contain at least one range header like 'm00005–m00072 topic'",
            code="COMPACT_INVALID_CONTENT",
        )
    blocks.append(_finish_block(header, title, body_lines))
    return blocks


def _finish_block(header: re.Match[str], title: str, body_lines: Sequence[str]) -> CompressBlock:
    start_ref = f"m{int(header.group(1)):05d}"
    end_ref = f"m{int(header.group(2)):05d}"
    if _ref_ordinal(end_ref) < _ref_ordinal(start_ref):
        raise CompactError(
            f"range {start_ref}–{end_ref} is reversed; the start ref must not come after the end ref",
            code="COMPACT_INVALID_RANGE",
        )
    body = "\n".join(body_lines).strip()
    if not body:
        raise CompactError(
            f"range {start_ref}–{end_ref} has an empty summary; write the summary body under its header",
            code="COMPACT_EMPTY_SUMMARY",
        )
    return CompressBlock(start_ref=start_ref, end_ref=end_ref, title=title, body=body)


def requested_refs(blocks: Sequence[CompressBlock]) -> list[str]:
    """Return the unique refs a call refers to, in first-seen order.

    A single-message range such as m00005-m00005 names the same ref twice; that
    is a valid request, not a duplicate reference, so dedupe before resolving.
    """
    refs: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        for ref in (block.start_ref, block.end_ref):
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


def format_local(milliseconds: int) -> str:
    """Format an epoch-millisecond timestamp in the host's local time format."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(milliseconds / 1000))


def _fingerprint(text: str, limit: int, *, tail: bool) -> str:
    flat = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    if tail:
        return flat[-limit:] if len(flat) > limit else flat
    return flat[:limit]


class CompressTool(BaseTool):
    """Tool protocol implementation for the compress transaction."""

    name = "compress"
    description = DESCRIPTION
    parallel_safe = False
    parameters = make_parameters(
        {
            "content": {
                "type": "string",
                "description": (
                    "ONE plain string holding ALL ranges to compress. Each block starts with a "
                    "header line 'm00150–m00220 optional topic', followed by that block's "
                    "self-contained summary. Batch multiple ranges into one call."
                ),
            }
        },
        required=["content"],
    )

    def __init__(self, store: Any, blocks: BlockStore, refs: RefStore) -> None:
        self.store = store
        self.blocks = blocks
        self.refs = refs

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Validate, persist blocks/refs, and record the checkpoint transaction."""
        content = args.get("content")
        try:
            blocks = parse_content(content)  # type: ignore[arg-type]
            session = _resolve_session(self.store, ctx)
            if session is None:
                raise CompactError(
                    "no session is available to compress; the runtime must expose the current "
                    "session through services['sessions']",
                    code="COMPACT_SESSION_UNAVAILABLE",
                )
            return self._compress(session, blocks, ctx)
        except CompactError as exc:
            return ToolResult(text=f"Error: {exc}", is_error=True, meta={"code": exc.code})
        except Exception as exc:  # noqa: BLE001 - defensive: a tool must never break the turn
            return ToolResult(
                text=f"Error: compress failed: {exc}",
                is_error=True,
                meta={"code": "COMPACT_UNEXPECTED", "name": type(exc).__name__},
            )

    # -- transaction -------------------------------------------------------
    def _compress(self, session: Any, blocks: list[CompressBlock], ctx: ToolContext) -> ToolResult:
        ranges = self._validate(session, blocks)
        block_offsets = self.blocks.offsets(session.session_id)
        ref_offsets = self.refs.offsets(session.session_id)
        appended = 0
        compaction_id = str(uuid.uuid4())
        try:
            stored: list[BlockRecord] = []
            resolution = resolve_refs(session, requested_refs(blocks), self.refs)
            pending = {entry.ref: entry for entry in resolution.pending}
            if pending:
                self.refs.register(session.session_id, list(pending.values()))
            for item in ranges:
                record = self.blocks.add(
                    session.session_id,
                    start_ref=item.start_ref,
                    end_ref=item.end_ref,
                    start_seq=item.start_seq,
                    end_seq=item.end_seq,
                    title=item.block.title,
                    summary=item.block.body,
                )
                stored.append(record)
            ranges = [replace_range(item, record.id) for item, record in zip(ranges, stored, strict=True)]

            lifecycle: dict[str, Any] = {"compactionId": compaction_id, "source": "compress"}
            if isinstance(getattr(ctx, "turn", None), int):
                lifecycle["turn"] = ctx.turn
            session.append("compaction/start", dict(lifecycle))
            appended += 1
            for item in ranges:
                summary_seq = session.append(
                    "compaction/summary",
                    {
                        "compactionId": compaction_id,
                        "source": "compress",
                        "blockId": item.block_id,
                        "range": {"start": item.start_ref, "end": item.end_ref},
                        "title": item.block.title,
                        "summary": [{"type": "text", "text": item.block.body}],
                        "shadowedRange": {"start": item.start_seq, "end": item.end_seq},
                        "shadowedSeqs": list(item.shadowed_seqs),
                        "shadowedTokenCount": round(item.shadowed_tokens),
                    },
                )
                appended += 1
                checkpoint = Message(
                    role="user",
                    content=frame_summary(item.block.body),  # type: ignore[arg-type]
                    id=new_message_id(),
                    source={
                        "kind": "compact-checkpoint",
                        "compactionId": compaction_id,
                        "blockId": item.block_id,
                        "range": {"start": item.start_ref, "end": item.end_ref},
                    },
                )
                session.append(
                    "user/message",
                    checkpoint.model_dump(exclude_none=True),
                    surfaceOp={"op": "replace", "startSeq": item.start_seq, "endSeq": item.end_seq},
                    sourceEventSeqs=sorted({summary_seq, *item.shadowed_seqs}),
                )
                appended += 1
            session.append(
                "compaction/end",
                {
                    "compactionId": compaction_id,
                    "source": "compress",
                    "blockIds": [r.block_id for r in ranges],
                },
            )
            appended += 1
        except Exception as exc:
            if appended == 0:
                self.blocks.rollback(session.session_id, block_offsets)
                self.refs.rollback(session.session_id, ref_offsets)
                raise
            self._close_failed(session, compaction_id)
            return ToolResult(
                text=(
                    f"Error: compress failed after {appended} record(s) were written: {exc}. "
                    "The already-written checkpoints are valid; re-run compress for the remaining ranges."
                ),
                is_error=True,
                meta={"code": "COMPACT_PARTIAL_WRITE", "committedRecords": appended},
            )

        assert len(stored) == len(ranges)
        saved = sum(item.saved_tokens for item in ranges)
        text = self._result_text(stored, ranges, saved, session)
        return ToolResult(
            text=text,
            meta={
                "compactionId": compaction_id,
                "blocks": [
                    {
                        "id": record.id,
                        "range": {"start": record.start_ref, "end": record.end_ref},
                        "startSeq": record.start_seq,
                        "endSeq": record.end_seq,
                    }
                    for record in stored
                ],
                "savedTokens": saved,
            },
        )

    def _close_failed(self, session: Any, compaction_id: str) -> None:
        try:
            session.append(
                "compaction/end",
                {
                    "compactionId": compaction_id,
                    "source": "compress",
                    "error": {"message": "compress failed"},
                },
            )
        except (OSError, TypeError, ValueError, SessionError):
            pass

    # -- validation --------------------------------------------------------
    def _validate(self, session: Any, blocks: list[CompressBlock]) -> list[_Range]:
        records: list[LogRecord] = session.records
        strict = not bool(getattr(session, "trimmed", False))
        nodes = fold_surface(records, strict=strict)
        index_by_seq = {node.seq: index for index, node in enumerate(nodes)}
        resolution = resolve_refs(session, requested_refs(blocks), self.refs)
        by_ref = resolution.by_ref()
        ranges: list[_Range] = []
        previous_end = 0
        for block in blocks:
            start = by_ref[block.start_ref]
            end = by_ref[block.end_ref]
            if start.ordinal > end.ordinal:
                raise CompactError(
                    f"range {block.start_ref}–{block.end_ref} is reversed",
                    code="COMPACT_INVALID_RANGE",
                )
            if start.seq not in index_by_seq:
                raise CompactError(
                    f"message {block.start_ref} (seq {start.seq}) is no longer on the visible surface; "
                    "refs point at already-compressed messages",
                    code="COMPACT_REF_NOT_ON_SURFACE",
                )
            if end.seq not in index_by_seq:
                raise CompactError(
                    f"message {block.end_ref} (seq {end.seq}) is no longer on the visible surface; "
                    "refs point at already-compressed messages",
                    code="COMPACT_REF_NOT_ON_SURFACE",
                )
            start_index = index_by_seq[start.seq]
            end_index = index_by_seq[end.seq]
            if start_index > end_index:
                raise CompactError(
                    f"range {block.start_ref}–{block.end_ref} is not in conversation order",
                    code="COMPACT_INVALID_RANGE",
                )
            if start.ordinal <= previous_end:
                raise CompactError(
                    "ranges must be listed in ascending order and must not overlap",
                    code="COMPACT_OVERLAP",
                )
            if previous_end and start.ordinal == previous_end + 1:
                raise CompactError(
                    "adjacent ranges must be merged into one block; leave at least one message "
                    "between blocks or use a single header for a contiguous span",
                    code="COMPACT_ADJACENT_RANGES",
                )
            previous_end = end.ordinal
            if start_index == 0 and nodes[0].type == "system/message":
                raise CompactError(
                    "the system prompt cannot be compressed; start the range after the first message",
                    code="COMPACT_SYSTEM_PROMPT",
                )
            shadowed = nodes[start_index : end_index + 1]
            shadowed_seqs = tuple(node.seq for node in shadowed)
            messages = [message for node in shadowed if (message := event_message(node)) is not None]
            if not messages:
                raise CompactError(
                    f"range {block.start_ref}–{block.end_ref} covers no visible messages",
                    code="COMPACT_EMPTY_RANGE",
                )
            shadowed_tokens = estimate_messages_tokens(messages)
            probe = Message(
                role="user",
                content=frame_summary(block.body),  # type: ignore[arg-type]
                id=new_message_id(),
                source={"kind": "compact-checkpoint"},
            )
            framed_tokens = estimate_message_tokens(probe)
            ranges.append(
                _Range(
                    block=block,
                    block_id=None,
                    start_ref=block.start_ref,
                    end_ref=block.end_ref,
                    start_seq=start.seq,
                    end_seq=end.seq,
                    start_index=start_index,
                    end_index=end_index,
                    shadowed_seqs=shadowed_seqs,
                    shadowed_tokens=shadowed_tokens,
                    start_time=shadowed[0].time,
                    end_time=shadowed[-1].time,
                    saved_tokens=max(0, round(shadowed_tokens - framed_tokens)),
                )
            )
        self._check_tool_spans(records, ranges)
        return ranges

    @staticmethod
    def _tool_spans(records: Sequence[LogRecord]) -> list[tuple[int, int]]:
        """Return merged [start, end] spans that must be compressed atomically.

        A span runs from the assistant message that issued a tool call through
        the matching tool/result (and tool/call envelope record).  A range that
        cuts through a span would leave a tool result without its call, or an
        assistant tool-call without its result, so such ranges are rejected.
        """
        call_seq: dict[str, int] = {}
        result_seq: dict[str, int] = {}
        message_seq: dict[str, int] = {}
        for record in records:
            data = record.data if isinstance(record.data, dict) else {}
            if record.type == "tool/call":
                call_id = data.get("callId")
                if isinstance(call_id, str):
                    call_seq[call_id] = record.seq
            elif record.type == "tool/result":
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                source = message.get("source") if isinstance(message.get("source"), dict) else {}
                call_id = message.get("toolCallId") or source.get("callId")
                if isinstance(call_id, str):
                    result_seq[call_id] = record.seq
            elif record.type == "assistant/message":
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                content = message.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "tool-call":
                            call_id = part.get("id")
                            if isinstance(call_id, str):
                                message_seq.setdefault(call_id, record.seq)
        raw_spans: list[tuple[int, int]] = []
        for call_id, call in call_seq.items():
            result = result_seq.get(call_id, call)
            start = min(call, result, message_seq.get(call_id, call))
            end = max(call, result)
            raw_spans.append((start, end))
        for call_id, result in result_seq.items():
            if call_id not in call_seq:
                raw_spans.append((message_seq.get(call_id, result), result))
        raw_spans.sort()
        merged: list[tuple[int, int]] = []
        for start, end in raw_spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def _check_tool_spans(self, records: Sequence[LogRecord], ranges: Sequence[_Range]) -> None:
        spans = self._tool_spans(records)
        for item in ranges:
            for span_start, span_end in spans:
                overlaps = span_start <= item.end_seq and span_end >= item.start_seq
                contains = item.start_seq <= span_start and span_end <= item.end_seq
                if overlaps and not contains:
                    raise CompactError(
                        f"range {item.start_ref}–{item.end_ref} cuts through a tool call/result pair "
                        f"(seq {span_start}–{span_end}); extend the range to include the whole exchange "
                        "or leave it outside the range",
                        code="COMPACT_SPLITS_TOOL_PAIR",
                    )

    # -- result text -------------------------------------------------------
    def _result_text(
        self,
        stored: Sequence[BlockRecord],
        ranges: Sequence[_Range],
        saved: int,
        session: Any,
    ) -> str:
        header = ", ".join(f"{record.start_ref}–{record.end_ref}" for record in stored)
        lines = [f"[Compressed {header} → {len(stored)} block(s), ~{saved} tokens saved.]"]
        for record, item in zip(stored, ranges, strict=True):
            head = _fingerprint(item.block.body, HEAD_CHARS, tail=False)
            tail = _fingerprint(item.block.body, TAIL_CHARS, tail=True)
            lines.append(
                f" · {record.id} full summary {len(item.block.body)}ch "
                f"[{format_local(item.start_time)} – {format_local(item.end_time)}] · "
                f'head "{head}" … tail "{tail}"'
            )
        lines.append(FINGERPRINT_NOTE)
        lines.append("")
        lines.append(MINIMUM_STATUS if self._everything_compressed(session) else UNCOVERED_STATUS)
        return "\n".join(lines)

    def _everything_compressed(self, session: Any) -> bool:
        """True when no addressable message is left visible after this call.

        Checkpoints written by this transaction replaced their ranges, so any
        remaining non-system message is either a gap the caller skipped or
        history after the last range.
        """
        strict = not bool(getattr(session, "trimmed", False))
        nodes = fold_surface(session.records, strict=strict)
        for node in nodes:
            if node.type == "system/message":
                continue
            message = event_message(node)
            if message is None:
                continue
            if (message.source or {}).get("kind") == "compact-checkpoint":
                continue
            return False
        return True


def replace_range(item: _Range, block_id: str) -> _Range:
    """Return a copy of *item* carrying its assigned block id."""
    return _Range(
        block=item.block,
        block_id=block_id,
        start_ref=item.start_ref,
        end_ref=item.end_ref,
        start_seq=item.start_seq,
        end_seq=item.end_seq,
        start_index=item.start_index,
        end_index=item.end_index,
        shadowed_seqs=item.shadowed_seqs,
        shadowed_tokens=item.shadowed_tokens,
        start_time=item.start_time,
        end_time=item.end_time,
        saved_tokens=item.saved_tokens,
    )


def _resolve_session(store: Any, ctx: ToolContext) -> Any | None:
    """Find the live session object for this tool call, then fall back to the store."""
    services = getattr(ctx, "services", None) or {}
    session_id = getattr(ctx, "session_id", "")
    for key in ("session", "current_session", "agent_session"):
        candidate = services.get(key) if isinstance(services, dict) else None
        if candidate is None:
            continue
        if (
            callable(getattr(candidate, "append", None))
            and isinstance(getattr(candidate, "records", None), list)
            and (not session_id or getattr(candidate, "session_id", session_id) == session_id)
        ):
            return candidate
    sessions = services.get("sessions") if isinstance(services, dict) else None
    if sessions is not None:
        for attribute in ("session", "current", "current_session"):
            candidate = getattr(sessions, attribute, None)
            if candidate is not None and callable(getattr(candidate, "append", None)):
                return candidate
        nested = getattr(sessions, "store", None)
        if nested is not None and callable(getattr(nested, "open", None)):
            store = nested
        elif callable(getattr(sessions, "open", None)):
            store = sessions
    if store is None or not session_id:
        return None
    try:
        return store.open(session_id)
    except (LookupError, OSError, ValueError, SessionError):
        return None


__all__ = [
    "DESCRIPTION",
    "HEADER_RE",
    "HEAD_CHARS",
    "MINIMUM_STATUS",
    "TAIL_CHARS",
    "UNCOVERED_STATUS",
    "CompressBlock",
    "CompressTool",
    "format_local",
    "parse_content",
    "replace_range",
]
