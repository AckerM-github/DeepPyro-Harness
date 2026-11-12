"""Canonical surface fold and message derivation for session v4 logs.

DSH keeps a single ordered list of model-visible nodes (the surface).  Surface
records are appended or, for compactions and rewrites, substituted in place for
an earlier contiguous run of nodes.  Derivation walks that surface order and
projects each node to a wire Message; the raw log alone is not enough because a
replace must delete the shadowed nodes from the model history while keeping
their original position.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord, Message, ReplaceOp
from deeppyro.session.events import (
    KNOWN_EVENT_TYPES,
    PROJECTION_EVENTS,
    SURFACE_EVENTS,
    assert_source_event_references,
)

#: Plan kinds returned by plan_surface_transition.
APPEND = "append"
REPLACE = "replace"
SKIP = "skip"


def fold_surface(records: Sequence[LogRecord], *, strict: bool = True) -> list[LogRecord]:
    """Replay *records* through the canonical surface fold.

    Strict mode mirrors DSH exactly: a replace must reference two nodes that are
    currently on the surface and must satisfy the source-reference, tool-result
    rewrite, and system-head rewrite rules.  Tolerant mode (used for trimmed
    fixture logs whose prefix was cut) appends a replace whose target is absent
    instead of failing, and skips records without a surfaceOp.
    """
    ordered = list(records)
    by_seq = {record.seq: record for record in ordered}
    nodes: list[LogRecord] = []
    for record in ordered:
        kind, start, end = plan_surface_transition(nodes, record, strict=strict, records_by_seq=by_seq)
        if kind == APPEND:
            nodes.append(record)
        elif kind == REPLACE:
            assert start is not None and end is not None
            nodes[start : end + 1] = [record]
    return nodes


def plan_surface_transition(
    nodes: Sequence[LogRecord],
    record: LogRecord,
    *,
    strict: bool,
    records_by_seq: Mapping[int, LogRecord],
) -> tuple[str, int | None, int | None]:
    """Validate one record against the current surface and describe its effect.

    The returned tuple is (kind, startIdx, endIdx); start/end are only set for a
    replace.  Nothing is mutated, so callers can validate first and commit the
    in-memory fold only after the append has reached durable storage.
    """
    if record.ignorable is True and record.type not in KNOWN_EVENT_TYPES:
        # Unknown ignorable records are log-only by definition.
        return SKIP, None, None
    _assert_developer_header(record, records_by_seq, strict=strict)
    if record.type in PROJECTION_EVENTS:
        # v1 has no projection interpreter (image/offload). The durable log keeps
        # the record; it never becomes a surface node.
        return SKIP, None, None
    if record.type not in SURFACE_EVENTS:
        return SKIP, None, None
    op = record.surfaceOp
    if op is None:
        message = f"session event {record.type!r} is surface-eligible and requires a surfaceOp marker"
        if strict:
            raise SessionError(message)
        return SKIP, None, None
    if not isinstance(op, ReplaceOp):
        return APPEND, None, None
    start_idx = _node_index(nodes, op.startSeq)
    end_idx = _node_index(nodes, op.endSeq)
    if start_idx is None or end_idx is None:
        if strict:
            missing = op.startSeq if start_idx is None else op.endSeq
            raise SessionError(f"surface replace: seq {missing} not found in surface")
        return APPEND, None, None
    if start_idx > end_idx:
        raise SessionError(
            f"surface replace: start seq {op.startSeq} (index {start_idx}) is after "
            f"end seq {op.endSeq} (index {end_idx})"
        )
    shadowed = nodes[start_idx : end_idx + 1]
    _assert_source_references(record, [node.seq for node in shadowed])
    _assert_tool_result_rewrite(record, shadowed)
    _assert_system_head_rewrite(record, nodes, start_idx, shadowed)
    return REPLACE, start_idx, end_idx


def _node_index(nodes: Sequence[LogRecord], seq: int) -> int | None:
    for index, node in enumerate(nodes):
        if node.seq == seq:
            return index
    return None


def _assert_source_references(record: LogRecord, shadowed_seqs: Sequence[int]) -> None:
    event: dict[str, Any] = {}
    if record.sourceEventSeqs is not None:
        event["sourceEventSeqs"] = record.sourceEventSeqs
    assert_source_event_references(event, shadowed_seqs, subject=f"event {record.seq}")


def _assert_tool_result_rewrite(record: LogRecord, shadowed: Sequence[LogRecord]) -> None:
    if record.type != "tool/result":
        return
    if len(shadowed) != 1:
        raise SessionError("tool/result surface replacement must rewrite exactly one current node")
    original = shadowed[0]
    if original.type != "tool/result":
        raise SessionError("tool/result surface replacement must target a current tool/result")
    if not _deep_equal_json(_normalized_tool_data(original.data), _normalized_tool_data(record.data)):
        raise SessionError("tool/result surface replacement may change only content")


def _normalized_tool_data(data: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(data)
    message = output.get("message")
    if isinstance(message, Mapping):
        output["message"] = {**message, "content": None}
    return output


def _assert_system_head_rewrite(
    record: LogRecord, nodes: Sequence[LogRecord], start_idx: int, shadowed: Sequence[LogRecord]
) -> None:
    if start_idx != 0 or nodes[0].type != "system/message":
        return
    if record.type != "system/message" or len(shadowed) != 1:
        raise SessionError(
            "surface replace: node 0 holds the system prompt and may be rewritten only by a "
            "system/message over exactly that node"
        )


def _assert_developer_header(
    record: LogRecord, records_by_seq: Mapping[int, LogRecord], *, strict: bool
) -> None:
    """Validate tool additions against their immutable historical request header."""
    if record.type != "developer/message":
        return
    data = record.data
    if "headerSeq" not in data:
        return
    header_seq = data["headerSeq"]
    if not isinstance(header_seq, int) or isinstance(header_seq, bool) or header_seq >= record.seq:
        raise SessionError("developer/message headerSeq must reference an earlier request/header")
    header_event = records_by_seq.get(header_seq)
    if header_event is None:
        if strict:
            raise SessionError("developer/message headerSeq must reference an earlier request/header")
        return
    if header_event.type != "request/header":
        raise SessionError("developer/message headerSeq must reference an earlier request/header")
    header = header_event.data.get("header")
    tools = header.get("tools") if isinstance(header, Mapping) else None
    tools = tools if isinstance(tools, list) else []
    message = data.get("message")
    content = message.get("content") if isinstance(message, Mapping) else []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, Mapping) or block.get("type") != "tool-addition":
            continue
        name = block.get("toolName")
        definitions = [tool for tool in tools if isinstance(tool, Mapping) and tool.get("name") == name]
        if len(definitions) != 1:
            raise SessionError(
                f"developer/message tool-addition {name!r} must name exactly one tool in headerSeq {header_seq}"
            )
        definition = definitions[0]
        if not isinstance(definition.get("description"), str) or not isinstance(
            definition.get("parameters"), Mapping
        ):
            raise SessionError(
                f"developer/message tool-addition {name!r} requires a complete tool definition "
                f"in headerSeq {header_seq}"
            )
        if "deferLoading" in definition and definition["deferLoading"] is not True:
            raise SessionError("developer/message referenced tool deferLoading must be true when present")


def _deep_equal_json(left: object, right: object) -> bool:
    """Structural equality over the JSON value domain used by session events."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, list) or isinstance(right, list):
        if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
            return False
        return all(_deep_equal_json(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if set(left) != set(right):
            return False
        return all(_deep_equal_json(left[key], right[key]) for key in left)
    return left == right


def derive_messages(records: Sequence[LogRecord], *, strict: bool = True) -> list[Message]:
    """Derive the model-visible message history from a session log."""
    nodes = fold_surface(records, strict=strict)
    messages: list[Message] = []
    for node in nodes:
        message = event_message(node)
        if message is not None:
            messages.append(message)
    return messages


def event_message(record: LogRecord) -> Message | None:
    """Project one surface node to its wire Message, or None when it yields none.

    Empty-content system, developer, and assistant messages are dropped; the
    developer/message retirement placeholder (content=[]) therefore disappears
    from the derived history while its node still occupies the old position.
    """
    data = record.data
    if record.type == "user/message":
        payload: object = data
    elif record.type in ("system/message", "developer/message", "assistant/message", "tool/result"):
        payload = data.get("message")
    else:
        return None
    if not isinstance(payload, Mapping):
        return None
    content = payload.get("content")
    if (
        record.type in ("system/message", "developer/message", "assistant/message")
        and isinstance(content, list)
        and len(content) == 0
    ):
        return None
    return Message.model_validate(payload)
