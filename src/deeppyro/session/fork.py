"""Fork a session: copy an exact prefix, mark the inherited cut, and close the tail.

DSH forks by durably copying the source events through a boundary seq, appending
a session/end-seed marker with data.inherited=true, then synthesizing closers for
any open turn: conservative error tool results for unanswered tool calls, an open
step/end, and a turn/end whose reason kind is "forked".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from deeppyro.core.errors import SessionError
from deeppyro.core.ids import new_session_id, now_ms
from deeppyro.core.wire import LogRecord, SessionHeader
from deeppyro.session import writer
from deeppyro.session.session import Session
from deeppyro.session.store import SessionStore

TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"

CLOSER_TEXT: dict[str, dict[str, str]] = {
    "interrupted": {
        "started": (
            "The tool call was interrupted after it was recorded, but no result was durably "
            "recorded. Its outcome is unknown. Decide whether to retry from the tool semantics: "
            "retry only if the operation is read-only or idempotent; if it may have side effects, "
            "first verify external state or ask the user. Do not retry blindly."
        ),
        "notStarted": (
            "The tool call was interrupted before the Harness recorded it as started. "
            "Retry it if it is still needed."
        ),
    },
    "forked": {
        "started": (
            "The history inherited by this branch records this tool call starting but does not "
            "include its result. The parent session may have completed it after the fork point. "
            "Decide whether to retry from the tool semantics: retry only if the operation is "
            "read-only or idempotent; if it may have side effects, first verify external state or "
            "ask the user. Do not retry blindly."
        ),
        "notStarted": (
            "The history inherited by this branch has no record of this tool call starting. The "
            "parent session may have executed it after the fork point. Decide whether to retry "
            "from the tool semantics: retry only if the operation is read-only or idempotent; if "
            "it may have side effects, first verify external state or ask the user. Do not retry "
            "blindly."
        ),
    },
}


class SessionForkError(SessionError):
    """Typed rejection of a fork request (INVALID_BOUNDARY / SESSION_NOT_FOUND / ...)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def fork_session(store: SessionStore, src_id: str, *, at_seq: int | None = None) -> Session:
    """Fork *src_id* through the inclusive event seq *at_seq* (default: last event)."""
    try:
        source = store.open(src_id)
    except SessionError as exc:
        raise SessionForkError(str(exc), "SESSION_NOT_FOUND") from exc
    events = source.records
    boundary = _fork_boundary(src_id, events, at_seq)
    if source.trimmed:
        raise SessionForkError(
            f"session {src_id!r} is trimmed; a contiguous event prefix is required for fork",
            "INVALID_BOUNDARY",
        )
    seed: list[dict[str, Any]] = [] if boundary is None else build_fork_seed(events, boundary)
    header = SessionHeader(
        id=new_session_id(),
        createdAt=now_ms(),
        cwd=source.header.cwd,
        isSeeded=True,
        delegationDepth=source.header.delegationDepth + 1,
        agentPreset=source.header.agentPreset,
        parentSession=source.session_id,
        origin=source.header.origin,
    )
    return store._create_with_seed(header, seed)


def _fork_boundary(src_id: str, events: Sequence[LogRecord], requested: int | None) -> int | None:
    if requested is not None:
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
            raise SessionForkError(
                f"fork boundary for session {src_id!r} must be a non-negative integer, got {requested!r}",
                "INVALID_BOUNDARY",
            )
        boundary = requested
    elif not events:
        return None
    else:
        boundary = events[-1].seq
    if boundary >= len(events):
        last_seq = events[-1].seq if events else "none"
        raise SessionForkError(
            f"fork boundary {boundary} does not exist in session {src_id!r} (last seq: {last_seq})",
            "INVALID_BOUNDARY",
        )
    if events[boundary].seq != boundary:
        raise SessionForkError(
            f"fork boundary {boundary} does not match a contiguous event seq in session {src_id!r}",
            "INVALID_BOUNDARY",
        )
    return boundary


def build_fork_seed(events: Sequence[LogRecord], boundary: int) -> list[dict[str, Any]]:
    """Copy events through *boundary*, add the inherited end-seed, then forked closers."""
    prefix = [writer.event_to_dict(record) for record in events[: boundary + 1]]
    prefix.append(
        {
            "type": "session/end-seed",
            "seq": boundary + 1,
            "time": events[boundary].time,
            "data": {"inherited": True},
        }
    )
    return prefix + open_turn_closers(prefix, {"kind": "forked"})


def open_turn_closers(
    events: Sequence[LogRecord | Mapping[str, Any]], cause: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Deterministic synthetic events that close an open tail turn.

    Accepts LogRecords and/or raw event envelopes so a caller can pass the fork
    prefix including its synthetic session/end-seed marker.
    """
    kind = str(cause.get("kind", "interrupted"))
    text = CLOSER_TEXT.get(kind, CLOSER_TEXT["interrupted"])
    open_turn: int | None = None
    open_step: int | None = None
    pending: dict[str, dict[str, Any]] = {}
    last_seq: int | None = None
    last_time: int = 0
    for event in events:
        event_type = _field(event, "type")
        seq = _field(event, "seq")
        time = _field(event, "time")
        data = _field(event, "data")
        last_seq, last_time = seq, time
        _observe(
            event_type,
            data,
            seq=seq,
            pending=pending,
            surface_op=_optional_field(event, "surfaceOp"),
        )
        if event_type == "turn/start":
            open_turn = _int_or_none(data.get("turn"))
            open_step = None
        elif event_type == "turn/end":
            open_turn = None
            open_step = None
        elif event_type == "step/start":
            open_step = _int_or_none(data.get("step"))
        elif event_type == "step/end":
            open_step = None
    if open_turn is None or last_seq is None:
        return []
    results = _pending_results(pending, text, last_seq=last_seq, last_time=last_time)
    seq = last_seq + len(results) + 1
    if open_step is not None:
        results.append(
            {
                "type": "step/end",
                "seq": seq,
                "time": last_time,
                "data": {"turn": open_turn, "step": open_step},
            }
        )
        seq += 1
    results.append(
        {
            "type": "turn/end",
            "seq": seq,
            "time": last_time,
            "data": {"turn": open_turn, "reason": {"kind": kind}},
        }
    )
    return results


def _observe(
    event_type: str,
    data: Mapping[str, Any],
    *,
    seq: int | None,
    pending: dict[str, dict[str, Any]],
    surface_op: object,
) -> None:
    if event_type in ("turn/start", "turn/end", "step/end"):
        pending.clear()
        return
    if event_type == "assistant/message":
        message = data.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, Mapping) and block.get("type") == "tool-call":
                call_id = block.get("id")
                if isinstance(call_id, str) and call_id:
                    pending[call_id] = {
                        "turn": _int_or_none(data.get("turn")),
                        "step": _int_or_none(data.get("step")),
                        "callSeq": None,
                    }
        return
    if event_type == "tool/call":
        call_id = data.get("callId")
        entry = pending.get(call_id) if isinstance(call_id, str) else None
        if entry is not None and seq is not None:
            entry["callSeq"] = seq
        return
    if event_type == "tool/result":
        message = data.get("message")
        source = message.get("source") if isinstance(message, Mapping) else None
        call_id = source.get("callId") if isinstance(source, Mapping) else None
        entry = pending.get(call_id) if isinstance(call_id, str) else None
        if (
            surface_op == "append"
            and entry is not None
            and entry["turn"] == data.get("turn")
            and entry["step"] == data.get("step")
        ):
            del pending[call_id]


def _pending_results(
    pending: Mapping[str, Mapping[str, Any]], text: Mapping[str, str], *, last_seq: int, last_time: int
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seq = last_seq + 1
    for call_id, entry in pending.items():
        started = entry.get("callSeq") is not None
        message = {
            "id": f"forked-tool-result-{call_id}-{seq}",
            "role": "tool",
            "toolCallId": call_id,
            "isError": True,
            "source": {"kind": "tool", "callId": call_id},
            "content": [{"type": "text", "text": text["started"] if started else text["notStarted"]}],
        }
        event: dict[str, Any] = {
            "type": "tool/result",
            "seq": seq,
            "time": last_time,
            "data": {
                "turn": entry.get("turn"),
                "step": entry.get("step"),
                "message": message,
                "error": {"name": "ToolOutcomeUnknownError", "code": TOOL_OUTCOME_UNKNOWN}
                if started
                else {"name": "ToolNotStartedError", "code": TOOL_NOT_STARTED},
            },
            "surfaceOp": "append",
        }
        if started:
            event["sourceEventSeqs"] = [entry["callSeq"]]
        results.append(event)
        seq += 1
    return results


def _field(event: LogRecord | Mapping[str, Any], key: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key)


def _optional_field(event: LogRecord | Mapping[str, Any], key: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key, None)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
