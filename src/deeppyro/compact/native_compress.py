"""Native mode for the compress tool: model-authored summaries over fold.

v4 records a compaction transaction: start, per-block summary records, a
replacement user/message per block, then end.  A native session folds facts, so
a block is exactly one compaction.done event: covers that block's message ids
and inserts the framed summary.  Ranges are identified by native refs, which
map to message ids (see native_refs).

The parser, guards and result text intentionally match the v4 tool so the model
sees the same contract:

* the same header format and pure parse_content errors;
* the same overlap/adjacency/order validation, now over view.messages indices;
* the same tool-pair safety, expressed as cut safety: a block may not start on
  a tool result whose call precedes it, nor end on an assistant message whose
  tool calls have results after it;
* one compaction_id per call, one compaction.done per block, committed oldest
  first; a call that fails before writing leaves the session untouched and a
  later block failure keeps earlier blocks committed.

The native compress path never touches the system prompt: the folded view keeps
the system message outside view.messages, so a block starting at the first
message is valid and COMPACT_SYSTEM_PROMPT disappears by construction.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from deeppyro.agent.context_plan import estimate_message_tokens, message_tokens
from deeppyro.compact.blocks import BlockRecord, BlockStore, CompactError
from deeppyro.compact.native_compactor import tool_safe_cuts
from deeppyro.compact.native_refs import (
    RefEntry,
    RefPlan,
    RefStore,
    addressable_messages,
    canonical_ref,
    ref_ordinal,
    resolve_refs,
)
from deeppyro.compact.prompts import frame_summary
from deeppyro.core.errors import SessionError
from deeppyro.core.ids import new_message_id, now_ms
from deeppyro.core.messages import CompactionSource, Message, part_from_dict
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.schema import EventKind, NativeSession

logger = logging.getLogger(__name__)

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

ENVELOPE_KEYS = ("run_id", "turn_id", "step_id", "trace_id")


@dataclass(frozen=True, slots=True)
class CompressBlock:
    """One parsed header plus its summary body."""

    start_ref: str
    end_ref: str
    title: str
    body: str


@dataclass(frozen=True, slots=True)
class CompressedRange:
    """A validated block resolved to view.messages indices."""

    block: CompressBlock
    start_index: int
    end_index: int
    message_ids: tuple[str, ...]
    shadowed_tokens: int
    framed_tokens: int
    saved_tokens: int
    start_time: int
    end_time: int
    block_id: str = ""
    compaction_id: str = ""


@dataclass(frozen=True, slots=True)
class NativeCompressOutcome:
    """Tool-shaped result of one native compress invocation."""

    ok: bool
    text: str
    meta: Mapping[str, object]

    @property
    def code(self) -> str:
        return str(self.meta.get("code", ""))


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
    start_ref = canonical_ref(int(header.group(1)))
    end_ref = canonical_ref(int(header.group(2)))
    if ref_ordinal(end_ref) < ref_ordinal(start_ref):
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
    """Return the unique refs a call refers to, in first-seen order."""
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


def _native_session(session: NativeSession | object) -> NativeSession:
    native = getattr(session, "native_session", session)
    if not isinstance(native, NativeSession):
        raise TypeError("native compress requires a NativeSession or a native session handle")
    return native


def _summary_message(body: str, compaction_id: str) -> Message:
    return Message(
        role="user",
        content=tuple(part_from_dict(part) for part in frame_summary(body)),
        source=CompactionSource(compaction_id),
        id=new_message_id(),
        created_at=now_ms(),
    )


def _envelope(envelope: Mapping[str, object] | None) -> dict[str, str]:
    if not envelope:
        return {}
    return {
        key: str(envelope[key])
        for key in ENVELOPE_KEYS
        if isinstance(envelope.get(key), str) and envelope.get(key)
    }


class NativeCompressor:
    """Validate and commit model-authored compression blocks on a native session."""

    def __init__(self, blocks: BlockStore, refs: RefStore) -> None:
        self.blocks = blocks
        self.refs = refs

    def compress(
        self,
        session: NativeSession | object,
        content: str,
        *,
        envelope: Mapping[str, object] | None = None,
    ) -> NativeCompressOutcome:
        """Compress the ranges named in *content*, returning a tool-shaped outcome."""
        try:
            parsed = parse_content(content)
            native = _native_session(session)
            view = fold(native.header, native.events)
            plan = self.refs.plan(view.header.id, view)
            if not addressable_messages(view, plan):
                return NativeCompressOutcome(
                    ok=True,
                    text=MINIMUM_STATUS,
                    meta={"compactionId": None, "blocks": [], "savedTokens": 0, "minimum": True},
                )
            ranges = self._validate(view, plan, parsed)
        except CompactError as exc:
            return NativeCompressOutcome(ok=False, text=f"Error: {exc}", meta={"code": exc.code})
        except Exception as exc:  # noqa: BLE001 - a tool must never break the turn
            return NativeCompressOutcome(
                ok=False,
                text=f"Error: compress failed: {exc}",
                meta={"code": "COMPACT_UNEXPECTED", "name": type(exc).__name__},
            )
        return self._commit(native, view, ranges, envelope)

    # -- validation --------------------------------------------------------
    def _validate(
        self,
        view: SessionView,
        plan: RefPlan,
        blocks: Sequence[CompressBlock],
    ) -> list[CompressedRange]:
        locations = {location.ref: location for location in resolve_refs(view, plan, requested_refs(blocks))}
        cuts = tool_safe_cuts(view.messages)
        ranges: list[CompressedRange] = []
        previous_end = 0
        for block in blocks:
            start = locations[block.start_ref]
            end = locations[block.end_ref]
            if start.index > end.index:
                raise CompactError(
                    f"range {block.start_ref}–{block.end_ref} is not in conversation order",
                    code="COMPACT_INVALID_RANGE",
                )
            if start.index <= previous_end and ranges:
                raise CompactError(
                    "ranges must be listed in ascending order and must not overlap",
                    code="COMPACT_OVERLAP",
                )
            if ranges and start.index == previous_end + 1:
                raise CompactError(
                    "adjacent ranges must be merged into one block; leave at least one message "
                    "between blocks or use a single header for a contiguous span",
                    code="COMPACT_ADJACENT_RANGES",
                )
            previous_end = end.index
            if not cuts[start.index] or not cuts[end.index + 1]:
                raise CompactError(
                    f"range {block.start_ref}–{block.end_ref} cuts through a tool call/result pair; "
                    "extend the range to include the whole exchange or leave it outside the range",
                    code="COMPACT_SPLITS_TOOL_PAIR",
                )
            covered = view.messages[start.index : end.index + 1]
            if not covered:
                raise CompactError(
                    f"range {block.start_ref}–{block.end_ref} covers no visible messages",
                    code="COMPACT_EMPTY_RANGE",
                )
            shadowed = message_tokens(covered)
            framed = estimate_message_tokens(_summary_message(block.body, "probe"))
            ranges.append(
                CompressedRange(
                    block=block,
                    start_index=start.index,
                    end_index=end.index,
                    message_ids=tuple(message.id for message in covered),
                    shadowed_tokens=shadowed,
                    framed_tokens=framed,
                    saved_tokens=max(0, round(shadowed - framed)),
                    start_time=covered[0].created_at,
                    end_time=covered[-1].created_at,
                )
            )
        return ranges

    # -- commit ------------------------------------------------------------
    def _commit(
        self,
        native: NativeSession,
        view: SessionView,
        ranges: Sequence[CompressedRange],
        envelope: Mapping[str, object] | None,
    ) -> NativeCompressOutcome:
        session_id = view.header.id
        block_offsets = self.blocks.offsets(session_id)
        ref_offsets = self.refs.offsets(session_id)
        compaction_id = str(uuid.uuid4())
        assoc = _envelope(envelope)
        committed: list[CompressedRange] = []
        try:
            pending = [entry for entry in self.refs.plan(session_id, view).pending]
            if pending:
                self.refs.register(session_id, pending)
            for item in ranges:
                record = self.blocks.add(
                    session_id,
                    start_ref=item.block.start_ref,
                    end_ref=item.block.end_ref,
                    start_seq=item.start_index,
                    end_seq=item.end_index,
                    title=item.block.title,
                    summary=item.block.body,
                    source="compress",
                    compaction_id=compaction_id,
                    start_message_id=item.message_ids[0],
                    end_message_id=item.message_ids[-1],
                )
                summary = _summary_message(item.block.body, compaction_id)
                native.append(
                    EventKind.COMPACTION_DONE,
                    {
                        "compaction_id": compaction_id,
                        "covers": list(item.message_ids),
                        "message": summary.to_dict(),
                    },
                    run_id=assoc.get("run_id"),
                    turn_id=assoc.get("turn_id"),
                    step_id=assoc.get("step_id"),
                    trace_id=assoc.get("trace_id"),
                )
                committed.append(_with_record(item, record, compaction_id))
        except CompactError:
            self._recover(session_id, view, block_offsets, ref_offsets, committed)
            raise
        except Exception as exc:
            self._recover(session_id, view, block_offsets, ref_offsets, committed)
            if not committed:
                raise
            ids = [item.block_id for item in committed]
            return NativeCompressOutcome(
                ok=False,
                text=(
                    f"Error: compress failed after {len(committed)} block(s) were written: {exc}. "
                    "The already-written checkpoints are valid; re-run compress for the remaining ranges."
                ),
                meta={
                    "code": "COMPACT_PARTIAL_WRITE",
                    "compactionId": compaction_id,
                    "committedBlocks": ids,
                    "committedRecords": len(committed),
                },
            )
        saved = sum(item.saved_tokens for item in committed)
        text = self._result_text(native, committed, saved)
        return NativeCompressOutcome(
            ok=True,
            text=text,
            meta={
                "compactionId": compaction_id,
                "blocks": [
                    {
                        "id": item.block_id,
                        "range": {"start": item.block.start_ref, "end": item.block.end_ref},
                        "startMessageId": item.message_ids[0],
                        "endMessageId": item.message_ids[-1],
                        "start_index": item.start_index,
                        "end_index": item.end_index,
                        "savedTokens": item.saved_tokens,
                    }
                    for item in committed
                ],
                "savedTokens": saved,
            },
        )

    def _recover(
        self,
        session_id: str,
        view: SessionView,
        block_offsets: tuple[int, int],
        ref_offsets: int,
        committed: Sequence[CompressedRange],
    ) -> None:
        """Restore sidecars to match exactly what was committed as session facts."""
        try:
            if not committed:
                self.blocks.rollback(session_id, block_offsets)
                self.refs.rollback(session_id, ref_offsets)
                return
            self.refs.rollback(session_id, ref_offsets)
            needed = {message_id for item in committed for message_id in item.message_ids}
            plan = self.refs.plan(session_id, view)
            entries = [
                RefEntry(ref=ref, message_id=message.id, assigned_at=now_ms())
                for ref, message in plan.assignments
                if message.id in needed
            ]
            if entries:
                self.refs.register(session_id, entries)
        except (OSError, ValueError, CompactError, SessionError):
            logger.exception("could not reconcile compaction sidecars after a failed compress")

    # -- result text -------------------------------------------------------
    def _result_text(
        self,
        native: NativeSession,
        ranges: Sequence[CompressedRange],
        saved: int,
    ) -> str:
        header = ", ".join(f"{item.block.start_ref}–{item.block.end_ref}" for item in ranges)
        lines = [f"[Compressed {header} → {len(ranges)} block(s), ~{saved} tokens saved.]"]
        for item in ranges:
            head = _fingerprint(item.block.body, HEAD_CHARS, tail=False)
            tail = _fingerprint(item.block.body, TAIL_CHARS, tail=True)
            lines.append(
                f" · {item.block_id} full summary {len(item.block.body)}ch "
                f"[{format_local(item.start_time)} – {format_local(item.end_time)}] · "
                f'head "{head}" … tail "{tail}"'
            )
        lines.append(FINGERPRINT_NOTE)
        lines.append("")
        lines.append(MINIMUM_STATUS if self._everything_compressed(native) else UNCOVERED_STATUS)
        return "\n".join(lines)

    def _everything_compressed(self, native: NativeSession) -> bool:
        latest = fold(native.header, native.events)
        return all(isinstance(message.source, CompactionSource) for message in latest.messages)


def _with_record(
    item: CompressedRange,
    record: BlockRecord,
    compaction_id: str,
) -> CompressedRange:
    return CompressedRange(
        block=item.block,
        start_index=item.start_index,
        end_index=item.end_index,
        message_ids=item.message_ids,
        shadowed_tokens=item.shadowed_tokens,
        framed_tokens=item.framed_tokens,
        saved_tokens=item.saved_tokens,
        start_time=item.start_time,
        end_time=item.end_time,
        block_id=record.id,
        compaction_id=compaction_id,
    )


__all__ = [
    "FINGERPRINT_NOTE",
    "HEADER_RE",
    "HEAD_CHARS",
    "MINIMUM_STATUS",
    "TAIL_CHARS",
    "UNCOVERED_STATUS",
    "CompressBlock",
    "CompressedRange",
    "NativeCompressOutcome",
    "NativeCompressor",
    "format_local",
    "parse_content",
    "requested_refs",
]
