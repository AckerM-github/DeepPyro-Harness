"""Session surface projection: fold, tool pairing, and compaction ranges.

The durable log is an append-only event stream.  Surface events (system,
developer, user, assistant messages and tool results) additionally carry an
envelope that either appends a node or replaces a contiguous range of earlier
nodes with itself.  This module is the pure functional version of that fold:
it turns a record list into the ordered surface used for context derivation
and for selecting a compaction range.

Keeping the fold here (rather than in the session writer) lets the compactor
reason about ranges without importing session internals; the fake session in
the tests uses the same rules.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from deeppyro.compact.tokens import estimate_message_tokens
from deeppyro.core.errors import SessionError
from deeppyro.core.protocols import ToolSchema
from deeppyro.core.wire import Message

logger = logging.getLogger(__name__)

SURFACE_TYPES = frozenset(
    {"system/message", "developer/message", "user/message", "assistant/message", "tool/result"}
)
SURFACE_OP_APPEND = "append"


@dataclass(frozen=True, slots=True)
class SurfaceNode:
    """One visible node on the derived surface."""

    seq: int
    type: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CompactionRegion:
    """A contiguous, safe-to-shadow surface range."""

    start: int
    end: int
    start_index: int
    end_index: int
    shadowed_seqs: list[int]
    shadowed_tokens: float


@dataclass(frozen=True, slots=True)
class EntryState:
    """Compaction-relevant lifecycle facts recovered from the log."""

    unmatched_start_seq: int | None
    unmatched_start_data: dict[str, Any] | None
    open_turn: int | None
    latest_end_seed_seq: int | None


def _field(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, dict):
        return record.get(name, default)
    return getattr(record, name, default)


def _seq_of(record: Any) -> int:
    seq = _field(record, "seq")
    if not isinstance(seq, int):
        raise SessionError(f"record has no integer seq: {record!r}")
    return seq


def _data_of(record: Any) -> dict[str, Any]:
    data = _field(record, "data")
    if not isinstance(data, dict):
        raise SessionError(f"record has no data object: {record!r}")
    return data


def fold_surface(records: Sequence[Any]) -> list[SurfaceNode]:
    """Fold surface events into the current ordered surface.

    Validation mirrors the session writer: replacements must point at earlier
    seqs, must list every shadowed node in sourceEventSeqs, and must not
    replace a system-message head unless the replacement is that same system
    message.  Malformed input raises SessionError.
    """
    nodes: list[SurfaceNode] = []
    for record in records:
        record_type = _field(record, "type")
        if record_type not in SURFACE_TYPES:
            continue
        seq = _seq_of(record)
        data = _data_of(record)
        op = _field(record, "surfaceOp")
        if op is None or op == SURFACE_OP_APPEND:
            nodes.append(SurfaceNode(seq=seq, type=record_type, data=data))
            continue
        if not isinstance(op, dict) or op.get("op") != "replace":
            raise SessionError(f"invalid surfaceOp at seq {seq}: {op!r}")
        start_seq = op.get("startSeq")
        end_seq = op.get("endSeq")
        if not isinstance(start_seq, int) or not isinstance(end_seq, int):
            raise SessionError(f"replacement at seq {seq} needs integer startSeq/endSeq")
        if start_seq >= seq or end_seq >= seq:
            raise SessionError(f"replacement at seq {seq} may only shadow earlier records")
        current = [node.seq for node in nodes]
        try:
            start_index = current.index(start_seq)
            end_index = current.index(end_seq)
        except ValueError as exc:
            raise SessionError(f"replacement at seq {seq} targets a node not on the surface") from exc
        if start_index > end_index:
            raise SessionError(f"replacement at seq {seq} has startSeq after endSeq")
        source_seqs = _field(record, "sourceEventSeqs") or []
        if not isinstance(source_seqs, list) or not source_seqs:
            raise SessionError(f"replacement at seq {seq} requires sourceEventSeqs")
        if len(set(source_seqs)) != len(source_seqs) or any(item >= seq for item in source_seqs):
            raise SessionError(f"replacement at seq {seq} has invalid sourceEventSeqs")
        shadowed = current[start_index : end_index + 1]
        missing = [item for item in shadowed if item not in source_seqs]
        if missing:
            raise SessionError(f"replacement at seq {seq} does not list shadowed seqs {missing}")
        head_is_system = nodes[0].type == "system/message"
        if start_index == 0 and head_is_system and not (record_type == "system/message" and end_index == 0):
            raise SessionError(f"replacement at seq {seq} would shadow the system prompt")
        nodes[start_index : end_index + 1] = [SurfaceNode(seq=seq, type=record_type, data=data)]
    return nodes


def tool_pairing_cuts(nodes: Sequence[SurfaceNode]) -> list[int]:
    """Return the open-tool-call count at every cut of the surface.

    cuts[i] is the count after the first i nodes; cuts[len(nodes)] is always 0
    for a well-formed log.  A negative count means a tool result appeared
    without its call.
    """
    deltas: list[int] = []
    for node in nodes:
        delta = 0
        if node.type == "assistant/message":
            message = node.data.get("message") if isinstance(node.data, dict) else None
            content = message.get("content") if isinstance(message, dict) else []
            if isinstance(content, list):
                delta = sum(
                    1 for part in content if isinstance(part, dict) and part.get("type") == "tool-call"
                )
        elif node.type == "tool/result":
            delta = -1
        deltas.append(delta)
    cuts = [0]
    for delta in deltas:
        cuts.append(cuts[-1] + delta)
    if any(cut < 0 for cut in cuts):
        raise SessionError("tool results are unbalanced on the session surface")
    return cuts


def _node_index(nodes: Sequence[SurfaceNode], seq: int) -> int:
    for index, node in enumerate(nodes):
        if node.seq == seq:
            return index
    raise SessionError(f"surface node {seq} not found")


def balanced_before(nodes: Sequence[SurfaceNode], seq: int) -> bool:
    """True when no tool call is open before seq."""
    cuts = tool_pairing_cuts(nodes)
    return cuts[_node_index(nodes, seq)] == 0


def balanced_after(nodes: Sequence[SurfaceNode], seq: int) -> bool:
    """True when no tool call is open after seq."""
    cuts = tool_pairing_cuts(nodes)
    return cuts[_node_index(nodes, seq) + 1] == 0


def derive_node_message(node: SurfaceNode) -> Message | None:
    """Derive the wire message for one surface node, or None when it is empty."""
    if node.type == "user/message":
        return Message.model_validate(node.data)
    if node.type == "tool/result":
        message = node.data.get("message")
        return Message.model_validate(message) if isinstance(message, dict) else None
    if node.type in ("system/message", "developer/message", "assistant/message"):
        message = node.data.get("message")
        if not isinstance(message, dict):
            return None
        if not message.get("content"):
            return None
        return Message.model_validate(message)
    return None


def surface_messages(nodes: Sequence[SurfaceNode], stop_index: int | None = None) -> list[Message]:
    """Derive messages for nodes before stop_index (all nodes by default)."""
    limit = len(nodes) if stop_index is None else max(0, min(stop_index, len(nodes)))
    messages: list[Message] = []
    for node in nodes[:limit]:
        message = derive_node_message(node)
        if message is not None:
            messages.append(message)
    return messages


def estimate_node_tokens(node: SurfaceNode) -> float:
    message = derive_node_message(node)
    if message is None:
        return 0.0
    return estimate_message_tokens(message)


def select_region(records: Sequence[Any], retain_tokens: int) -> CompactionRegion | None:
    """Choose the oldest safe region to shadow, keeping recent context intact.

    The newest retain_tokens worth of surface nodes are kept.  The cut is then
    moved older until it no longer splits an assistant tool-call from its
    result.  A system prompt at the surface head is never shadowed.  Returns
    None when nothing can be compacted safely.
    """
    nodes = fold_surface(records)
    if not nodes:
        return None
    first_index = 1 if nodes[0].type == "system/message" else 0
    if first_index >= len(nodes):
        return None
    tokens = [estimate_node_tokens(node) for node in nodes]
    keep_from = first_index
    accumulated = 0.0
    for index in range(len(nodes) - 1, first_index - 1, -1):
        accumulated += tokens[index]
        if accumulated >= max(retain_tokens, 0):
            keep_from = index
            break
    if keep_from <= first_index:
        return None
    cuts = tool_pairing_cuts(nodes)
    while keep_from > first_index and cuts[keep_from] != 0:
        keep_from -= 1
    if keep_from <= first_index:
        return None
    start_index = first_index
    end_index = keep_from - 1
    start_node = nodes[start_index]
    end_node = nodes[end_index]
    if cuts[start_index] != 0 or cuts[end_index + 1] != 0:
        return None
    shadowed = [node.seq for node in nodes[start_index : end_index + 1]]
    return CompactionRegion(
        start=start_node.seq,
        end=end_node.seq,
        start_index=start_index,
        end_index=end_index,
        shadowed_seqs=shadowed,
        shadowed_tokens=sum(tokens[start_index : end_index + 1]),
    )


def entry_state(records: Sequence[Any]) -> EntryState:
    """Recover open turn / unmatched compaction / end-seed facts."""
    latest_end_seed: int | None = None
    open_turn: int | None = None
    pending: dict[str, int] = {}
    pending_data: dict[str, dict[str, Any]] = {}
    for record in records:
        record_type = _field(record, "type")
        seq = _seq_of(record)
        data = _data_of(record)
        if record_type == "session/end-seed":
            latest_end_seed = seq
        elif record_type == "turn/start":
            turn = data.get("turn")
            open_turn = turn if isinstance(turn, int) else open_turn
        elif record_type == "turn/end":
            open_turn = None
        elif record_type == "compaction/start":
            compaction_id = str(data.get("compactionId", ""))
            if compaction_id and compaction_id not in pending:
                pending[compaction_id] = seq
                pending_data[compaction_id] = data
        elif record_type == "compaction/end":
            compaction_id = str(data.get("compactionId", ""))
            pending.pop(compaction_id, None)
            pending_data.pop(compaction_id, None)
    unmatched_seq: int | None = None
    unmatched_data: dict[str, Any] | None = None
    if pending:
        compaction_id = min(pending, key=lambda key: pending[key])
        unmatched_seq = pending[compaction_id]
        unmatched_data = pending_data.get(compaction_id)
    return EntryState(
        unmatched_start_seq=unmatched_seq,
        unmatched_start_data=unmatched_data,
        open_turn=open_turn,
        latest_end_seed_seq=latest_end_seed,
    )


def latest_tool_schemas(records: Sequence[Any]) -> list[ToolSchema]:
    """Return the tool catalog advertised by the most recent request/header."""
    for record in reversed(records):
        if _field(record, "type") != "request/header":
            continue
        data = _data_of(record)
        header = data.get("header") if isinstance(data, dict) else None
        raw_tools = header.get("tools") if isinstance(header, dict) else None
        if not isinstance(raw_tools, list):
            return []
        schemas: list[ToolSchema] = []
        for raw in raw_tools:
            if not isinstance(raw, dict):
                continue
            try:
                schemas.append(ToolSchema.model_validate(raw))
            except Exception:
                # A malformed historical tool entry must not break compaction.
                logger.debug("skipping malformed tool schema entry", exc_info=True)
                continue
        return schemas
    return []
