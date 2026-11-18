"""Synthetic native/session-v4 differential cases for the protocol surfaces the
recorded baseline cannot reach (KI-011).

The recorded fixtures under fixtures/native only carry real text and tool
traffic.  Each JSON file under tests/fixtures/native/synthetic is a
hand-authored, offline pair of the same conversation:

* a session v4 log (header + surface records), and
* the equivalent schema 1 native event stream,
* optionally the trace side channel that carries per-call usage.

Running a case writes both sides into a temporary directory, reads them back
through the production parsers (read_session_file / NativeCodec), derives the
message history on each side, and compares semantic projections rather than
bytes: message sequence and parts, tool call/result pairing, usage presence,
and one capability-specific invariant.  The handoff capability is covered by
the task facts both vocabularies can express: session v4 keeps the task plugin
payloads as ignorable extension records and native schema 1 makes task.state,
task.handoff and task.handoff.result core kinds.

render_matrix lists the covered/uncovered matrix; scripts/differential_matrix.py
prints it in CI.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from deeppyro.builtin.task_tools import HANDOFF_EVENT, HANDOFF_RESULT_EVENT, TASK_EVENT
from deeppyro.compact.prompts import CHECKPOINT_PREAMBLE, SUMMARY_CLOSE_TAG, SUMMARY_OPEN_TAG
from deeppyro.core.messages import (
    CompactionSource,
    ImagePart,
    ModelSource,
    PluginSource,
    ReasoningPart,
    RepairSource,
    SystemSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.core.wire import (
    SURFACE_EVENT_TYPES,
    ReplaceOp,
)
from deeppyro.core.wire import (
    ContentPart as WireContentPart,
)
from deeppyro.core.wire import (
    ImagePart as WireImagePart,
)
from deeppyro.core.wire import (
    Message as WireMessage,
)
from deeppyro.core.wire import (
    ReasoningPart as WireReasoningPart,
)
from deeppyro.core.wire import (
    TextPart as WireTextPart,
)
from deeppyro.core.wire import (
    ToolCallPart as WireToolCallPart,
)
from deeppyro.observability.trace import TraceLog, join_trace, read_trace
from deeppyro.session.derive import derive_messages, event_message, fold_surface
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.reader import SessionRead, read_session_file
from deeppyro.session.schema import (
    SESSION_ID_PATTERN,
    Event,
    EventKind,
    NativeCodec,
    NativeSession,
    SessionLog,
)

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "native" / "synthetic"

FORBIDDEN_USAGE_KEYS = frozenset(
    {
        "usage",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "cost_usd",
    }
)

HANDOFF_V4_TYPES = (TASK_EVENT, HANDOFF_EVENT, HANDOFF_RESULT_EVENT)
HANDOFF_NATIVE_TYPES = ("task.state", "task.handoff", "task.handoff.result")
RESERVED_SUBAGENT_TYPES = (
    "subagent.started",
    "subagent.finished",
    "team.member.changed",
    "team.task.changed",
)
NATIVE_APPROVAL_OUTCOMES = {
    "allowed-once": "allowed",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "unavailable": "unavailable",
}
USAGE_FIELDS = frozenset(
    {"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens"}
)
V4_SURFACE_EVENT_TYPES = frozenset(SURFACE_EVENT_TYPES)


class DifferentialMismatch(AssertionError):
    """The two sides of a synthetic case disagree on a semantic projection."""


class NotSupportedError(RuntimeError):
    """The vocabularies cannot represent a capability, so the case is xfail."""


class Capability(StrEnum):
    """One protocol surface that the recorded baseline does not cover."""

    IMAGE = "image"
    APPROVAL = "approval"
    COMPACTION = "compaction"
    MCP = "mcp"
    PTC = "ptc"
    SUBAGENT = "subagent"
    HANDOFF = "handoff"


CAPABILITIES: tuple[Capability, ...] = (
    Capability.IMAGE,
    Capability.APPROVAL,
    Capability.COMPACTION,
    Capability.MCP,
    Capability.PTC,
    Capability.SUBAGENT,
    Capability.HANDOFF,
)


class Coverage(StrEnum):
    """How a synthetic case participates in the differential."""

    COVERED = "covered"
    XFAIL = "xfail"


@dataclass(frozen=True, slots=True)
class SyntheticCase:
    """One hand-authored native/v4 pair exercising a single protocol surface."""

    case_id: str
    capability: Capability
    title: str
    coverage: Coverage
    limitations: tuple[str, ...]
    xfail_reason: str | None
    v4: Mapping[str, object] | None
    native: Mapping[str, object] | None
    trace: Mapping[str, object] | None

    def require_representation(self) -> None:
        """Raise when native schema 1 cannot record the task handoff facts yet."""
        if self.capability is not Capability.HANDOFF:
            return
        native_kinds = {kind.value for kind in EventKind}
        missing_native = [name for name in HANDOFF_NATIVE_TYPES if name not in native_kinds]
        if not missing_native:
            return
        raise NotSupportedError(
            f"{self.case_id}: native schema 1 cannot record task handoff facts yet "
            f"(missing {missing_native}); the v4 side uses ignorable extension records "
            f"{HANDOFF_V4_TYPES}; KI-011 tracks this gap"
        )


@dataclass(frozen=True, slots=True)
class MatrixRow:
    """One line of the covered/uncovered matrix."""

    case_id: str
    capability: Capability
    coverage: Coverage
    limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    """A side-agnostic projection of one message for semantic comparison."""

    role: str
    message_id: str
    parts: tuple[tuple[str, ...], ...]
    source: tuple[str, ...]
    tool_call_id: str | None
    is_error: bool


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def load_cases(root: Path | None = None) -> list[SyntheticCase]:
    """Load every synthetic case from root (defaults to the checked-in set)."""
    base = FIXTURE_ROOT if root is None else root
    if not base.is_dir():
        raise DifferentialMismatch(f"synthetic fixture directory is missing: {base}")
    cases: list[SyntheticCase] = []
    for path in sorted(base.glob("*.json")):
        raw = _read_json_object(path)
        cases.append(_parse_case(raw, path))
    if not cases:
        raise DifferentialMismatch(f"synthetic fixture directory has no cases: {base}")
    _check(len({case.case_id for case in cases}) == len(cases), "synthetic case ids must be unique")
    return cases


def _read_json_object(path: Path) -> Mapping[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DifferentialMismatch(f"{path.name}: invalid JSON: {exc}") from exc
    return _require_mapping(raw, path.name)


def _parse_case(raw: Mapping[str, object], path: Path) -> SyntheticCase:
    where = path.name
    case_id = _require_text(raw.get("case_id"), f"{where}: case_id")
    capability_text = _require_text(raw.get("capability"), f"{where}: capability")
    coverage_text = _require_text(raw.get("coverage"), f"{where}: coverage")
    try:
        capability = Capability(capability_text)
    except ValueError as exc:
        raise DifferentialMismatch(f"{where}: unknown capability {capability_text!r}") from exc
    try:
        coverage = Coverage(coverage_text)
    except ValueError as exc:
        raise DifferentialMismatch(f"{where}: unknown coverage {coverage_text!r}") from exc
    limitations = _optional_texts(raw.get("limitations"), f"{where}: limitations")
    reason_raw = raw.get("xfail_reason")
    reason = None if reason_raw is None else _require_text(reason_raw, f"{where}: xfail_reason")
    if coverage is Coverage.XFAIL:
        _check(reason is not None, f"{where}: an xfail case must explain why")
        assert reason is not None
        _check("KI-011" in reason, f"{where}: the xfail reason must cite KI-011")
    else:
        _check(reason is None, f"{where}: only an xfail case may carry xfail_reason")
    v4_raw = raw.get("v4")
    native_raw = raw.get("native")
    trace_raw = raw.get("trace")
    v4 = None if v4_raw is None else _require_mapping(v4_raw, f"{where}: v4")
    native = None if native_raw is None else _require_mapping(native_raw, f"{where}: native")
    trace = None if trace_raw is None else _require_mapping(trace_raw, f"{where}: trace")
    if coverage is Coverage.COVERED:
        _check(v4 is not None, f"{where}: a covered case needs the v4 side")
        _check(native is not None, f"{where}: a covered case needs the native side")
    if v4 is not None:
        _require_mapping(v4.get("header"), f"{where}: v4.header")
        _require_sequence(v4.get("records"), f"{where}: v4.records")
    if native is not None:
        session_id = _require_text(native.get("session_id"), f"{where}: native.session_id")
        _check(
            re.fullmatch(SESSION_ID_PATTERN, session_id) is not None,
            f"{where}: native.session_id does not match schema 1",
        )
        _require_int(native.get("created_at"), f"{where}: native.created_at")
        _require_sequence(native.get("events"), f"{where}: native.events")
    if trace is not None:
        _require_sequence(trace.get("records"), f"{where}: trace.records")
    return SyntheticCase(
        case_id=case_id,
        capability=capability,
        title=_require_text(raw.get("title"), f"{where}: title"),
        coverage=coverage,
        limitations=limitations,
        xfail_reason=reason,
        v4=v4,
        native=native,
        trace=trace,
    )


def _require_mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DifferentialMismatch(f"{where}: expected a JSON object")
    return value


def _require_sequence(value: object, where: str) -> Sequence[object]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise DifferentialMismatch(f"{where}: expected a JSON array")
    return value


def _require_text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise DifferentialMismatch(f"{where}: expected a non-empty string")
    return value


def _require_int(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DifferentialMismatch(f"{where}: expected a non-negative integer")
    return value


def _optional_text(value: object, where: str) -> str | None:
    return None if value is None else _require_text(value, where)


def _optional_texts(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(_require_text(item, where) for item in _require_sequence(value, where))


def _check(condition: object, message: str) -> None:
    if not condition:
        raise DifferentialMismatch(message)


# ---------------------------------------------------------------------------
# Materializing both sides through the production parsers
# ---------------------------------------------------------------------------


def compare_case(case: SyntheticCase, tmp_path: Path) -> None:
    """Materialize one synthetic case and assert semantic native/v4 equivalence."""
    case.require_representation()
    v4_session = _materialize_v4(case, tmp_path)
    native_log = _materialize_native(case, tmp_path)
    view = fold(native_log.header, native_log.events)
    _check(view.issues == (), f"{case.case_id}: native fold reported issues {view.issues!r}")
    v4_messages = derive_messages(v4_session.records)
    _check_messages(case, v4_messages, view)
    _check_tool_pairing(case, v4_session.records, native_log.events)
    trace = None if case.trace is None else _materialize_trace(case, tmp_path)
    _check_usage(case, v4_session.records, native_log.events, trace)
    _check_capability(case, v4_session.records, native_log.events, view)


def _materialize_v4(case: SyntheticCase, tmp_path: Path) -> SessionRead:
    v4 = case.v4
    _check(v4 is not None, f"{case.case_id}: covered case has no v4 side")
    assert v4 is not None
    header = _require_mapping(v4["header"], f"{case.case_id}: v4.header")
    records = _require_sequence(v4["records"], f"{case.case_id}: v4.records")
    path = tmp_path / f"{case.case_id}.v4.jsonl"
    lines = [_json_line(header)]
    lines.extend(_json_line(_require_mapping(record, f"{case.case_id}: v4 record")) for record in records)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    session = read_session_file(path)
    _check(not session.truncated, f"{case.case_id}: v4 fixture was written truncated")
    _check(not session.trimmed, f"{case.case_id}: v4 fixture has a seq gap")
    return session


def _materialize_native(case: SyntheticCase, tmp_path: Path) -> SessionLog:
    native = case.native
    _check(native is not None, f"{case.case_id}: covered case has no native side")
    assert native is not None
    session_id = _require_text(native["session_id"], f"{case.case_id}: native.session_id")
    created_at = _require_int(native["created_at"], f"{case.case_id}: native.created_at")
    events = _require_sequence(native["events"], f"{case.case_id}: native.events")
    path = tmp_path / "native" / f"{session_id}.jsonl"
    session = NativeSession.create(
        path,
        cwd=tmp_path,
        preset=_optional_text(native.get("preset"), f"{case.case_id}: native.preset") or "standard",
        session_id=session_id,
        origin=_optional_text(native.get("origin"), f"{case.case_id}: native.origin") or "user",
        created_at=created_at,
    )
    try:
        previous = created_at
        for index, raw_event in enumerate(events):
            raw = _require_mapping(raw_event, f"{case.case_id}: native event")
            kind = _require_text(raw.get("type"), f"{case.case_id}: native event type")
            data_raw = raw.get("data")
            data = None if data_raw is None else _require_mapping(data_raw, f"{case.case_id}: event data")
            stamp = max(created_at + index + 1, previous)
            if data is not None:
                message = data.get("message")
                if isinstance(message, Mapping) and isinstance(message.get("created_at"), int):
                    stamp = max(stamp, int(message["created_at"]))
            session.append(
                kind,
                data,
                run_id=_optional_text(raw.get("run_id"), f"{case.case_id}: run_id"),
                turn_id=_optional_text(raw.get("turn_id"), f"{case.case_id}: turn_id"),
                step_id=_optional_text(raw.get("step_id"), f"{case.case_id}: step_id"),
                trace_id=_optional_text(raw.get("trace_id"), f"{case.case_id}: trace_id"),
                ts=stamp,
            )
            previous = stamp
    finally:
        session.close()
    return NativeCodec.read(path)


def _materialize_trace(case: SyntheticCase, tmp_path: Path) -> TraceLog:
    trace = case.trace
    _check(trace is not None, f"{case.case_id}: covered case has no trace side channel")
    assert trace is not None
    records = _require_sequence(trace["records"], f"{case.case_id}: trace.records")
    path = tmp_path / f"{case.case_id}.trace.jsonl"
    lines = [_json_line(_require_mapping(record, f"{case.case_id}: trace record")) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return read_trace(path)


def _json_line(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Message projection and comparison
# ---------------------------------------------------------------------------


def _check_messages(case: SyntheticCase, v4_messages: Sequence[WireMessage], view: SessionView) -> None:
    v4_system = [message for message in v4_messages if message.role == "system"]
    v4_rest = [message for message in v4_messages if message.role != "system"]
    if v4_system:
        _check(len(v4_system) == 1, f"{case.case_id}: v4 side has more than one system message")
        _check(view.system is not None, f"{case.case_id}: native side has no system message")
        assert view.system is not None
        _check(
            _normalize_native_message(view.system) == _normalize_v4_message(v4_system[0], case.case_id),
            f"{case.case_id}: system message projections differ",
        )
    else:
        _check(view.system is None, f"{case.case_id}: native side has a system message but v4 does not")
    expected = [_normalize_v4_message(message, case.case_id) for message in v4_rest]
    actual = [_normalize_native_message(message) for message in view.messages]
    _check(
        len(expected) == len(actual),
        f"{case.case_id}: message count differs (v4={len(expected)}, native={len(actual)})",
    )
    for index, (v4_message, native_message) in enumerate(zip(expected, actual, strict=True)):
        _check(
            v4_message == native_message,
            f"{case.case_id}: message {index} differs (v4={v4_message!r}, native={native_message!r})",
        )


def _normalize_v4_message(message: WireMessage, case_id: str) -> NormalizedMessage:
    parts = tuple(_normalize_v4_part(part, case_id) for part in message.content)
    source_raw = _require_mapping(message.source, f"{case_id}: v4 message source")
    source = _normalize_v4_source(source_raw, case_id)
    if source[0] == "compaction":
        parts = _strip_checkpoint_frame(parts, case_id)
    return NormalizedMessage(
        role=message.role,
        message_id=message.id,
        parts=parts,
        source=source,
        tool_call_id=message.toolCallId,
        is_error=bool(message.isError),
    )


def _normalize_v4_part(part: WireContentPart, case_id: str) -> tuple[str, ...]:
    if isinstance(part, WireTextPart):
        return ("text", part.text)
    if isinstance(part, WireReasoningPart):
        return ("reasoning", part.text)
    if isinstance(part, WireToolCallPart):
        return ("tool-call", part.id, part.name, _canonical_arguments(part.arguments, case_id))
    if isinstance(part, WireImagePart):
        attachment = part.attachment
        _check(
            isinstance(attachment, str),
            f"{case_id}: synthetic image fixtures use a string attachment, not a dict",
        )
        return ("image", str(attachment))
    raise DifferentialMismatch(f"{case_id}: unsupported v4 message part {type(part).__name__}")


def _normalize_v4_source(source: Mapping[str, object], case_id: str) -> tuple[str, ...]:
    kind = source.get("kind")
    if kind == "user":
        return ("user",)
    if kind == "model":
        return (
            "model",
            _require_text(source.get("provider"), f"{case_id}: v4 model provider"),
            _require_text(source.get("model"), f"{case_id}: v4 model name"),
        )
    if kind == "tool":
        return ("tool", _require_text(source.get("callId"), f"{case_id}: v4 tool callId"))
    if kind == "system-prompt":
        return ("system",)
    if kind == "compact-checkpoint":
        return (
            "compaction",
            _require_text(source.get("compactionId"), f"{case_id}: v4 checkpoint compactionId"),
        )
    if kind == "repair":
        return ("repair", _require_text(source.get("reason"), f"{case_id}: v4 repair reason"))
    raise DifferentialMismatch(f"{case_id}: unsupported v4 message source kind {kind!r}")


def _strip_checkpoint_frame(parts: tuple[tuple[str, ...], ...], case_id: str) -> tuple[tuple[str, ...], ...]:
    texts = [part[1] for part in parts if part[0] == "text"]
    _check(
        len(texts) == 3 and len(parts) == 3,
        f"{case_id}: v4 checkpoint message must be the three-part compact frame",
    )
    _check(
        texts[0] == CHECKPOINT_PREAMBLE + "\n\n" + SUMMARY_OPEN_TAG,
        f"{case_id}: v4 checkpoint frame preamble differs from compact/prompts.py",
    )
    _check(texts[2] == SUMMARY_CLOSE_TAG, f"{case_id}: v4 checkpoint frame lacks its closing tag")
    return (("text", texts[1]),)


def _normalize_native_message(message: Any) -> NormalizedMessage:
    return NormalizedMessage(
        role=message.role,
        message_id=message.id,
        parts=tuple(_normalize_native_part(part) for part in message.content),
        source=_normalize_native_source(message.source),
        tool_call_id=message.tool_call_id,
        is_error=bool(message.is_error),
    )


def _normalize_native_part(part: object) -> tuple[str, ...]:
    if isinstance(part, TextPart):
        return ("text", part.text)
    if isinstance(part, ReasoningPart):
        return ("reasoning", part.text)
    if isinstance(part, ToolCallPart):
        return ("tool-call", part.id, part.name, _canonical_arguments(part.arguments, "native"))
    if isinstance(part, ImagePart):
        return ("image", part.attachment)
    raise DifferentialMismatch(f"unsupported native message part {type(part).__name__}")


def _normalize_native_source(source: object) -> tuple[str, ...]:
    if isinstance(source, UserSource):
        return ("user",)
    if isinstance(source, ModelSource):
        return ("model", source.provider, source.model)
    if isinstance(source, ToolSource):
        return ("tool", source.call_id)
    if isinstance(source, SystemSource):
        return ("system",)
    if isinstance(source, CompactionSource):
        return ("compaction", source.compaction_id)
    if isinstance(source, RepairSource):
        return ("repair", source.reason)
    if isinstance(source, PluginSource):
        return ("plugin", source.plugin)
    raise DifferentialMismatch(f"unsupported native message source {type(source).__name__}")


def _canonical_arguments(arguments: str, where: str) -> str:
    try:
        parsed: object = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise DifferentialMismatch(f"{where}: tool call arguments are not JSON: {exc}") from exc
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool call / result pairing
# ---------------------------------------------------------------------------


def _check_tool_pairing(case: SyntheticCase, records: Sequence[Any], events: Sequence[Event]) -> None:
    v4_pairs = _v4_tool_pairs(case, records)
    native_pairs = _native_tool_pairs(case, events)
    _check(
        len(v4_pairs) == len(native_pairs),
        f"{case.case_id}: tool pair count differs (v4={len(v4_pairs)}, native={len(native_pairs)})",
    )
    for index, (v4_pair, native_pair) in enumerate(zip(v4_pairs, native_pairs, strict=True)):
        _check(
            v4_pair == native_pair,
            f"{case.case_id}: tool pair {index} differs (v4={v4_pair!r}, native={native_pair!r})",
        )
    v4_calls = {
        _require_text(record.data.get("callId"), f"{case.case_id}: v4 tool call id"): (
            _require_text(record.data.get("name"), f"{case.case_id}: v4 tool name"),
            _canonical_arguments(
                _require_text(record.data.get("arguments"), f"{case.case_id}: v4 tool arguments"),
                case.case_id,
            ),
        )
        for record in records
        if record.type == "tool/call"
    }
    assistant_calls = [
        (part.id, part.name, _canonical_arguments(part.arguments, case.case_id))
        for message in derive_messages(records)
        if message.role == "assistant"
        for part in message.content
        if isinstance(part, WireToolCallPart)
    ]
    _check(
        len(assistant_calls) == len(v4_calls),
        f"{case.case_id}: v4 assistant tool-call parts and tool/call records disagree",
    )
    for call_id, name, arguments in assistant_calls:
        _check(call_id in v4_calls, f"{case.case_id}: v4 tool/call record missing for {call_id}")
        _check(
            v4_calls[call_id] == (name, arguments),
            f"{case.case_id}: v4 tool/call record for {call_id} differs from the assistant message",
        )


def _v4_tool_pairs(case: SyntheticCase, records: Sequence[Any]) -> list[tuple[str, str, str, str, bool]]:
    calls: dict[str, tuple[str, str]] = {}
    pairs: list[tuple[str, str, str, str, bool]] = []
    for record in records:
        if record.type == "tool/call":
            call_id = _require_text(record.data.get("callId"), f"{case.case_id}: v4 tool call id")
            _check(call_id not in calls, f"{case.case_id}: duplicate v4 tool/call {call_id}")
            calls[call_id] = (
                _require_text(record.data.get("name"), f"{case.case_id}: v4 tool name"),
                _canonical_arguments(
                    _require_text(record.data.get("arguments"), f"{case.case_id}: v4 arguments"),
                    case.case_id,
                ),
            )
        elif record.type == "tool/result":
            message = _require_mapping(record.data.get("message"), f"{case.case_id}: v4 tool result")
            call_id = _require_text(message.get("toolCallId"), f"{case.case_id}: v4 result toolCallId")
            _check(call_id in calls, f"{case.case_id}: v4 tool/result has no matching tool/call")
            name, arguments = calls.pop(call_id)
            pairs.append(
                (call_id, name, arguments, _text_parts(message.get("content")), bool(message.get("isError")))
            )
    _check(not calls, f"{case.case_id}: v4 tool/call without tool/result: {sorted(calls)}")
    return pairs


def _native_tool_pairs(case: SyntheticCase, events: Sequence[Event]) -> list[tuple[str, str, str, str, bool]]:
    calls: dict[str, tuple[str, str]] = {}
    pairs: list[tuple[str, str, str, str, bool]] = []
    for event in events:
        if event.type == "tool.call":
            call_id = _require_text(event.data.get("call_id"), f"{case.case_id}: native call_id")
            _check(call_id not in calls, f"{case.case_id}: duplicate native tool.call {call_id}")
            calls[call_id] = (
                _require_text(event.data.get("name"), f"{case.case_id}: native tool name"),
                _canonical_arguments(
                    _require_text(event.data.get("arguments"), f"{case.case_id}: native arguments"),
                    case.case_id,
                ),
            )
        elif event.type == "tool.result":
            message = _require_mapping(event.data.get("message"), f"{case.case_id}: native tool result")
            call_id = _require_text(
                message.get("tool_call_id"), f"{case.case_id}: native result tool_call_id"
            )
            _check(call_id in calls, f"{case.case_id}: native tool.result has no matching tool.call")
            name, arguments = calls.pop(call_id)
            pairs.append(
                (
                    call_id,
                    name,
                    arguments,
                    _text_parts(message.get("content")),
                    bool(message.get("is_error")),
                )
            )
    _check(not calls, f"{case.case_id}: native tool.call without tool.result: {sorted(calls)}")
    return pairs


def _text_parts(content: object) -> str:
    parts = _require_sequence(content, "message content")
    texts: list[str] = []
    for part in parts:
        mapping = _require_mapping(part, "content part")
        if mapping.get("type") == "text":
            texts.append(_require_text(mapping.get("text"), "text part"))
    return "".join(texts)


# ---------------------------------------------------------------------------
# Usage: v4 carries it, native records it in the trace side channel only
# ---------------------------------------------------------------------------


def _check_usage(
    case: SyntheticCase,
    records: Sequence[Any],
    events: Sequence[Event],
    trace: TraceLog | None,
) -> None:
    for event in events:
        if event.type == "run.checkpoint":
            continue
        leaked = sorted(set(_walk_keys(event.data)) & FORBIDDEN_USAGE_KEYS)
        _check(
            not leaked,
            f"{case.case_id}: native session log carries usage facts {leaked} "
            "(schema 8.5: they belong in trace)",
        )
    usages = [
        _require_mapping(record.data.get("usage"), f"{case.case_id}: v4 usage")
        for record in records
        if record.type == "assistant/message" and record.data.get("usage") is not None
    ]
    if not usages:
        _check(trace is None, f"{case.case_id}: trace side channel without v4 usage would be fabricated")
        return
    _check(trace is not None, f"{case.case_id}: v4 records carry usage but the case has no trace")
    assert trace is not None
    joined = join_trace(trace, events)
    _check(
        len(usages) == len(joined.model_calls),
        f"{case.case_id}: v4 usage count differs from trace model_call count",
    )
    for index, (usage, call) in enumerate(zip(usages, joined.model_calls, strict=True)):
        _check(
            set(usage) == USAGE_FIELDS,
            f"{case.case_id}: v4 usage {index} lacks the five token fields ({sorted(usage)})",
        )
        _check(
            call.input_tokens == _require_int(usage.get("inputTokens"), f"{case.case_id}: inputTokens"),
            f"{case.case_id}: usage {index} input tokens differ",
        )
        _check(
            call.output_tokens == _require_int(usage.get("outputTokens"), f"{case.case_id}: outputTokens"),
            f"{case.case_id}: usage {index} output tokens differ",
        )
        _check(
            (call.cached_input_tokens or 0)
            == _require_int(usage.get("cacheReadTokens"), f"{case.case_id}: cacheReadTokens"),
            f"{case.case_id}: usage {index} cached tokens differ",
        )
        _check(
            _require_int(usage.get("totalTokens"), f"{case.case_id}: totalTokens")
            == call.input_tokens + call.output_tokens,
            f"{case.case_id}: usage {index} total tokens differ from input+output",
        )
    native_calls = {
        _require_text(event.data.get("call_id"), f"{case.case_id}: trace call id"): _require_text(
            event.data.get("name"), f"{case.case_id}: trace tool name"
        )
        for event in events
        if event.type == "tool.call"
    }
    for record in joined.tool_calls:
        _check(
            native_calls.get(record.call_id) == record.tool,
            f"{case.case_id}: trace tool_call {record.call_id} does not match the native tool.call",
        )


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _walk_keys(item)


# ---------------------------------------------------------------------------
# Capability-specific invariants
# ---------------------------------------------------------------------------


def _check_capability(
    case: SyntheticCase,
    records: Sequence[Any],
    events: Sequence[Event],
    view: SessionView,
) -> None:
    check = _CAPABILITY_CHECKS[case.capability]
    check(case, records, events, view)


def _check_image(
    case: SyntheticCase, records: Sequence[Any], events: Sequence[Event], view: SessionView
) -> None:
    v4_images = _v4_image_attachments(case, records)
    native_images = _native_image_attachments(case, events)
    _check(len(v4_images) == 1, f"{case.case_id}: v4 side must carry exactly one image part")
    _check(
        len(native_images) == 1,
        f"{case.case_id}: native side must carry exactly one image part",
    )
    _check(
        v4_images[0] == native_images[0],
        f"{case.case_id}: image attachment differs (v4={v4_images[0]!r}, native={native_images[0]!r})",
    )
    _check(
        native_images[0].endswith(".png"),
        f"{case.case_id}: the synthetic image fixture is a PNG",
    )
    _check(
        any(message.role == "user" and _message_has_image(message) for message in view.messages),
        f"{case.case_id}: fold dropped the image part from the user message",
    )


def _v4_image_attachments(case: SyntheticCase, records: Sequence[Any]) -> list[str]:
    attachments: list[str] = []
    for record in records:
        if record.type != "user/message":
            continue
        content = _require_sequence(record.data.get("content"), f"{case.case_id}: v4 user content")
        for part in content:
            mapping = _require_mapping(part, f"{case.case_id}: v4 user part")
            if mapping.get("type") == "image":
                attachments.append(_require_text(mapping.get("attachment"), f"{case.case_id}: attachment"))
    return attachments


def _native_image_attachments(case: SyntheticCase, events: Sequence[Event]) -> list[str]:
    attachments: list[str] = []
    for event in events:
        if event.type != "user.message":
            continue
        message = _require_mapping(event.data.get("message"), f"{case.case_id}: native user message")
        content = _require_sequence(message.get("content"), f"{case.case_id}: native user content")
        for part in content:
            mapping = _require_mapping(part, f"{case.case_id}: native user part")
            if mapping.get("type") == "image":
                attachments.append(
                    _require_text(mapping.get("attachment"), f"{case.case_id}: native attachment")
                )
    return attachments


def _message_has_image(message: object) -> bool:
    content = getattr(message, "content", ())
    return any(isinstance(part, ImagePart) for part in content)


def _check_approval(
    case: SyntheticCase, records: Sequence[Any], events: Sequence[Event], view: SessionView
) -> None:
    asked = [record for record in records if record.type == "approval/asked"]
    decided = [record for record in records if record.type == "approval/decided"]
    requested = [event for event in events if event.type == "approval.requested"]
    decided_events = [event for event in events if event.type == "approval.decided"]
    _check(len(asked) == len(requested) == 1, f"{case.case_id}: expected one approval request")
    _check(len(decided) == len(decided_events) == 1, f"{case.case_id}: expected one approval decision")
    asked_data = asked[0].data
    requested_data = requested[0].data
    _check(
        _require_text(asked_data.get("id"), f"{case.case_id}: v4 approval id")
        == _require_text(requested_data.get("approval_id"), f"{case.case_id}: native approval id"),
        f"{case.case_id}: approval ids differ",
    )
    _check(
        _require_text(asked_data.get("toolName"), f"{case.case_id}: v4 approval toolName")
        == _require_text(requested_data.get("tool_name"), f"{case.case_id}: native approval tool_name"),
        f"{case.case_id}: approval tool names differ",
    )
    _check(
        _require_text(asked_data.get("callId"), f"{case.case_id}: v4 approval callId")
        == _require_text(requested_data.get("tool_call_id"), f"{case.case_id}: native approval tool_call_id"),
        f"{case.case_id}: approval tool call ids differ",
    )
    _check(
        _optional_text(asked_data.get("reason"), f"{case.case_id}: v4 approval reason")
        == _optional_text(requested_data.get("reason"), f"{case.case_id}: native approval reason"),
        f"{case.case_id}: approval reasons differ",
    )
    decided_data = decided[0].data
    decided_native = decided_events[0].data
    v4_outcome = _require_text(decided_data.get("outcome"), f"{case.case_id}: v4 approval outcome")
    native_outcome = _require_text(decided_native.get("outcome"), f"{case.case_id}: native approval outcome")
    _check(
        NATIVE_APPROVAL_OUTCOMES.get(v4_outcome) == native_outcome,
        f"{case.case_id}: approval outcomes differ (v4={v4_outcome!r}, native={native_outcome!r})",
    )
    _check(
        not view.pending_approvals,
        f"{case.case_id}: a decided approval must not remain pending ({view.pending_approvals!r})",
    )


def _check_compaction(
    case: SyntheticCase, records: Sequence[Any], events: Sequence[Event], view: SessionView
) -> None:
    replacements = [
        record
        for record in records
        if record.type in V4_SURFACE_EVENT_TYPES and isinstance(record.surfaceOp, ReplaceOp)
    ]
    _check(
        len(replacements) == 1,
        f"{case.case_id}: expected exactly one surface replace record, got {len(replacements)}",
    )
    replacement = replacements[0]
    assert isinstance(replacement.surfaceOp, ReplaceOp)
    start, end = replacement.surfaceOp.startSeq, replacement.surfaceOp.endSeq
    earlier = list(fold_surface([record for record in records if record.seq < replacement.seq]))
    shadowed = [event_message(node) for node in earlier if start <= node.seq <= end]
    _check(
        all(message is not None for message in shadowed), f"{case.case_id}: shadowed node is not a message"
    )
    shadowed_ids = [message.id for message in shadowed if message is not None]
    done = [event for event in events if event.type == "compaction.done"]
    _check(len(done) == 1, f"{case.case_id}: expected one native compaction.done")
    data = done[0].data
    covers = _require_sequence(data.get("covers"), f"{case.case_id}: native compaction covers")
    _check(
        tuple(covers) == tuple(shadowed_ids),
        f"{case.case_id}: compaction covers differ (v4={shadowed_ids!r}, native={list(covers)!r})",
    )
    summary = _require_mapping(data.get("message"), f"{case.case_id}: native compaction summary")
    summary_text = _text_parts(summary.get("content"))
    v4_summary = _require_mapping(replacement.data, f"{case.case_id}: v4 checkpoint message")
    v4_texts = [
        _require_text(part.get("text"), f"{case.case_id}: v4 checkpoint part")
        for part in _require_sequence(v4_summary.get("content"), f"{case.case_id}: v4 checkpoint content")
        if _require_mapping(part, f"{case.case_id}: checkpoint part").get("type") == "text"
    ]
    _check(
        len(v4_texts) == 3 and v4_texts[1] == summary_text,
        f"{case.case_id}: compaction summary text differs",
    )
    _check(
        len(view.messages) == 1 and view.messages[0].id == summary.get("id"),
        f"{case.case_id}: native fold must leave only the compaction summary",
    )
    native_ids = {message.id for message in view.messages}
    _check(
        all(message_id not in native_ids for message_id in shadowed_ids),
        f"{case.case_id}: a compacted message survived in the native view",
    )
    v4_checkpoint = _require_mapping(replacement.data, f"{case.case_id}: compact message")
    _check(
        _require_text(v4_checkpoint.get("id"), f"{case.case_id}: checkpoint id")
        == _require_text(summary.get("id"), f"{case.case_id}: native summary id"),
        f"{case.case_id}: compaction summary message ids differ",
    )


def _check_mcp(
    case: SyntheticCase, records: Sequence[Any], events: Sequence[Event], view: SessionView
) -> None:
    del view
    v4_names = _tool_call_names(case, records)
    native_names = _native_tool_names(case, events)
    _check(
        any(name.startswith("mcp__") for name in v4_names),
        f"{case.case_id}: v4 side must call an mcp__<server>__<tool> tool",
    )
    _check(
        sorted(v4_names) == sorted(native_names),
        f"{case.case_id}: tool names differ (v4={v4_names!r}, native={native_names!r})",
    )
    _check(
        not any(record.type.startswith("mcp") for record in records),
        f"{case.case_id}: session v4 has no mcp/* record type, so the tool seam is all there is",
    )
    _check(
        not any(event.type.startswith("mcp") for event in events),
        f"{case.case_id}: native schema 1 has no MCP lifecycle event yet",
    )


def _tool_call_names(case: SyntheticCase, records: Sequence[Any]) -> list[str]:
    return [
        _require_text(record.data.get("name"), f"{case.case_id}: v4 tool name")
        for record in records
        if record.type == "tool/call"
    ]


def _native_tool_names(case: SyntheticCase, events: Sequence[Event]) -> list[str]:
    return [
        _require_text(event.data.get("name"), f"{case.case_id}: native tool name")
        for event in events
        if event.type == "tool.call"
    ]


def _check_ptc(
    case: SyntheticCase, records: Sequence[Any], events: Sequence[Event], view: SessionView
) -> None:
    del view
    dispatch_starts = [record for record in records if record.type == "tool/ptc-dispatch-start"]
    dispatches = [record for record in records if record.type == "tool/ptc-dispatch"]
    _check(bool(dispatch_starts), f"{case.case_id}: v4 side must record a tool/ptc-dispatch-start")
    _check(bool(dispatches), f"{case.case_id}: v4 side must record a tool/ptc-dispatch")
    for record in (*dispatch_starts, *dispatches):
        _check(
            record.surfaceOp is None and record.sourceEventSeqs is None,
            f"{case.case_id}: PTC dispatch records are non-surface log facts",
        )
    root_call = _require_text(dispatch_starts[0].data.get("rootCallId"), f"{case.case_id}: v4 root call id")
    sub_call = _require_text(dispatch_starts[0].data.get("subCallId"), f"{case.case_id}: v4 sub call id")
    _check(
        sub_call.startswith(f"{root_call}:ptc:"),
        f"{case.case_id}: nested dispatch ids do not follow <rootCallId>:ptc:<n>",
    )
    native_call_ids = [
        _require_text(event.data.get("call_id"), f"{case.case_id}: native call id")
        for event in events
        if event.type == "tool.call"
    ]
    _check(
        root_call in native_call_ids,
        f"{case.case_id}: native side must keep the outer root call",
    )
    _check(
        not any(":ptc:" in call_id for call_id in native_call_ids),
        f"{case.case_id}: native schema 1 has no nested PTC dispatch call ids yet",
    )
    _check(
        not any(event.type.startswith("ptc") for event in events),
        f"{case.case_id}: native schema 1 has no PTC lifecycle event yet",
    )


def _check_subagent(
    case: SyntheticCase, records: Sequence[Any], events: Sequence[Event], view: SessionView
) -> None:
    catalogs = [record for record in records if record.type == "subagent/catalog"]
    descriptors = [record for record in records if record.type == "subagent/descriptor"]
    _check(len(catalogs) == 1, f"{case.case_id}: v4 side must record one subagent/catalog")
    _check(len(descriptors) == 1, f"{case.case_id}: v4 side must record one subagent/descriptor")
    child_id = _require_text(catalogs[0].data.get("childId"), f"{case.case_id}: subagent child id")
    results = [
        _text_parts(
            _require_mapping(event.data.get("message"), f"{case.case_id}: tool result").get("content")
        )
        for event in events
        if event.type == "tool.result"
    ]
    _check(
        any(child_id in text for text in results),
        f"{case.case_id}: the subagent child id must surface in the tool result text",
    )
    native_kinds = {kind.value for kind in EventKind}
    reserved = [name for name in RESERVED_SUBAGENT_TYPES if name in native_kinds]
    _check(
        not reserved,
        f"{case.case_id}: native subagent lifecycle events landed; update the differential",
    )
    _check(
        bool(view.messages) and any(child_id in _message_text(message) for message in view.messages),
        f"{case.case_id}: the native message history must carry the child id",
    )


def _message_text(message: object) -> str:
    content = getattr(message, "content", ())
    return "".join(part.text for part in content if isinstance(part, TextPart))


def _check_handoff(
    case: SyntheticCase, records: Sequence[Any], events: Sequence[Event], view: SessionView
) -> None:
    task_states = [record for record in records if record.type == TASK_EVENT]
    handoffs = [record for record in records if record.type == HANDOFF_EVENT]
    results = [record for record in records if record.type == HANDOFF_RESULT_EVENT]
    native_states = [event for event in events if event.type == "task.state"]
    native_handoffs = [event for event in events if event.type == "task.handoff"]
    native_results = [event for event in events if event.type == "task.handoff.result"]
    _check(
        bool(task_states) and bool(handoffs) and bool(results),
        f"{case.case_id}: v4 side must record task/state, task/handoff and task/handoff/result",
    )
    _check(
        len(task_states) == len(native_states),
        f"{case.case_id}: task state count differs (v4={len(task_states)}, native={len(native_states)})",
    )
    _check(
        len(handoffs) == len(native_handoffs),
        f"{case.case_id}: handoff count differs (v4={len(handoffs)}, native={len(native_handoffs)})",
    )
    _check(
        len(results) == len(native_results),
        f"{case.case_id}: handoff result count differs (v4={len(results)}, native={len(native_results)})",
    )
    for record in (*task_states, *handoffs, *results):
        _check(
            record.ignorable is True,
            f"{case.case_id}: session v4 keeps {record.type!r} as an ignorable extension record",
        )
        _check(
            record.surfaceOp is None and record.sourceEventSeqs is None,
            f"{case.case_id}: {record.type!r} is a non-surface log fact",
        )
    for record, event in zip(task_states, native_states, strict=True):
        v4_data = record.data
        native_data = event.data
        _check(
            _require_text(v4_data.get("action"), f"{case.case_id}: v4 task action")
            == _require_text(native_data.get("action"), f"{case.case_id}: native task action"),
            f"{case.case_id}: task state actions differ",
        )
        _check(
            dict(_require_mapping(v4_data.get("task"), f"{case.case_id}: v4 task"))
            == dict(_require_mapping(native_data.get("task"), f"{case.case_id}: native task")),
            f"{case.case_id}: task projections differ",
        )
    last_task = _require_mapping(task_states[-1].data.get("task"), f"{case.case_id}: v4 last task")
    _check(
        _require_text(last_task.get("status"), f"{case.case_id}: v4 last task status") == "completed",
        f"{case.case_id}: v4 task must end completed",
    )
    v4_handoff = _require_mapping(handoffs[0].data, f"{case.case_id}: v4 handoff fact")
    v4_handoff_payload = _require_mapping(v4_handoff.get("handoff"), f"{case.case_id}: v4 handoff payload")
    v4_state = _require_mapping(v4_handoff_payload.get("state"), f"{case.case_id}: v4 handoff state")
    native_handoff = native_handoffs[0].data
    _check(
        _require_text(v4_handoff.get("runId"), f"{case.case_id}: v4 handoff run id")
        == _require_text(native_handoff.get("run_id"), f"{case.case_id}: native handoff run id"),
        f"{case.case_id}: handoff run ids differ",
    )
    for field in ("from", "to", "summary", "created_at"):
        _check(
            _require_text(v4_handoff_payload.get(field), f"{case.case_id}: v4 handoff {field}")
            == _require_text(native_handoff.get(field), f"{case.case_id}: native handoff {field}"),
            f"{case.case_id}: handoff {field} differs",
        )
    _check(
        _optional_text(v4_handoff_payload.get("task_id"), f"{case.case_id}: v4 handoff task id")
        == _optional_text(native_handoff.get("task_id"), f"{case.case_id}: native handoff task id"),
        f"{case.case_id}: handoff task ids differ",
    )
    _check(
        _optional_texts(v4_handoff_payload.get("open_items"), f"{case.case_id}: v4 open items")
        == _optional_texts(native_handoff.get("open_items"), f"{case.case_id}: native open items"),
        f"{case.case_id}: handoff open items differ",
    )
    _check(
        _require_text(v4_state.get("prompt"), f"{case.case_id}: v4 handoff prompt")
        == _require_text(native_handoff.get("prompt"), f"{case.case_id}: native handoff prompt"),
        f"{case.case_id}: handoff prompt differs",
    )
    _check(
        _require_int(v4_state.get("turn"), f"{case.case_id}: v4 handoff turn")
        == _require_int(native_handoff.get("turn"), f"{case.case_id}: native handoff turn"),
        f"{case.case_id}: handoff turns differ",
    )
    _check(
        _require_text(v4_state.get("callId"), f"{case.case_id}: v4 handoff call id")
        == _require_text(native_handoff.get("call_id"), f"{case.case_id}: native handoff call id"),
        f"{case.case_id}: handoff call ids differ",
    )
    _check(
        dict(_require_mapping(v4_state.get("budget"), f"{case.case_id}: v4 handoff budget"))
        == dict(_require_mapping(native_handoff.get("budget"), f"{case.case_id}: native handoff budget")),
        f"{case.case_id}: handoff budgets differ",
    )
    _check(
        "usage" in v4_state,
        f"{case.case_id}: session v4 keeps the parent usage snapshot in handoff state.usage",
    )
    leaked = sorted(set(_walk_keys(native_handoff)) & FORBIDDEN_USAGE_KEYS)
    _check(
        not leaked,
        f"{case.case_id}: native handoff payload carries usage facts {leaked} "
        "(schema 8.5: they belong in trace)",
    )
    v4_result = results[0].data
    native_result = native_results[0].data
    _check(
        _require_text(v4_result.get("runId"), f"{case.case_id}: v4 result run id")
        == _require_text(native_result.get("run_id"), f"{case.case_id}: native result run id"),
        f"{case.case_id}: handoff result run ids differ",
    )
    _check(
        _optional_text(v4_result.get("taskId"), f"{case.case_id}: v4 result task id")
        == _optional_text(native_result.get("task_id"), f"{case.case_id}: native result task id"),
        f"{case.case_id}: handoff result task ids differ",
    )
    _check(
        bool(v4_result.get("ok")) == bool(native_result.get("ok")),
        f"{case.case_id}: handoff result outcomes differ",
    )
    _check(
        _optional_text(v4_result.get("result"), f"{case.case_id}: v4 result text")
        == _optional_text(native_result.get("result"), f"{case.case_id}: native result text"),
        f"{case.case_id}: handoff result texts differ",
    )
    _check(
        _optional_result_text(v4_result.get("error"), f"{case.case_id}: v4 result error")
        == _optional_result_text(native_result.get("error"), f"{case.case_id}: native result error"),
        f"{case.case_id}: handoff result errors differ",
    )
    v4_usage = _require_mapping(v4_result.get("usage"), f"{case.case_id}: v4 result usage")
    _check(
        set(v4_usage) == USAGE_FIELDS,
        f"{case.case_id}: v4 handoff result usage lacks the five token fields ({sorted(v4_usage)})",
    )
    leaked = sorted(set(_walk_keys(native_result)) & FORBIDDEN_USAGE_KEYS)
    _check(
        not leaked,
        f"{case.case_id}: native handoff result carries usage facts {leaked} "
        "(schema 8.5: they belong in trace)",
    )
    _check(len(view.tasks) == 1, f"{case.case_id}: native fold must project one task")
    folded_task = view.tasks[0]
    _check(
        folded_task.task_id == _require_text(last_task.get("id"), f"{case.case_id}: v4 task id"),
        f"{case.case_id}: folded task id differs",
    )
    _check(
        folded_task.status == _require_text(last_task.get("status"), f"{case.case_id}: v4 task status"),
        f"{case.case_id}: folded task status differs",
    )
    _check(len(view.handoffs) == 1, f"{case.case_id}: native fold must project one handoff")
    folded_handoff = view.handoffs[0]
    _check(
        folded_handoff.run_id == _require_text(v4_handoff.get("runId"), f"{case.case_id}: v4 run id"),
        f"{case.case_id}: folded handoff run id differs",
    )
    _check(folded_handoff.result_ok is True, f"{case.case_id}: folded handoff result must be ok")
    _check(
        folded_handoff.result == _optional_text(v4_result.get("result"), f"{case.case_id}: v4 result text"),
        f"{case.case_id}: folded handoff result text differs",
    )
    _check(
        folded_handoff.task_id
        == _optional_text(v4_result.get("taskId"), f"{case.case_id}: v4 result task id"),
        f"{case.case_id}: folded handoff task id differs",
    )


def _optional_result_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DifferentialMismatch(f"{where}: expected a string")
    return value or None


_CAPABILITY_CHECKS: dict[
    Capability, Callable[[SyntheticCase, Sequence[Any], Sequence[Event], SessionView], None]
] = {
    Capability.IMAGE: _check_image,
    Capability.APPROVAL: _check_approval,
    Capability.COMPACTION: _check_compaction,
    Capability.MCP: _check_mcp,
    Capability.PTC: _check_ptc,
    Capability.SUBAGENT: _check_subagent,
    Capability.HANDOFF: _check_handoff,
}


# ---------------------------------------------------------------------------
# Covered/uncovered matrix
# ---------------------------------------------------------------------------


def matrix_rows(cases: Sequence[SyntheticCase]) -> list[MatrixRow]:
    """Project every case onto one matrix row."""
    return [
        MatrixRow(
            case_id=case.case_id,
            capability=case.capability,
            coverage=case.coverage,
            limitations=case.limitations,
        )
        for case in cases
    ]


def check_matrix(cases: Sequence[SyntheticCase]) -> None:
    """Assert the matrix has one case per capability and no silent gap."""
    rows = matrix_rows(cases)
    seen = {row.capability for row in rows}
    missing = [capability.value for capability in CAPABILITIES if capability not in seen]
    _check(not missing, f"capabilities without any synthetic case: {missing}")
    by_id = {case.case_id: case for case in cases}
    covered = {row.capability for row in rows if row.coverage is Coverage.COVERED}
    for row in rows:
        if row.coverage is Coverage.XFAIL:
            case = by_id[row.case_id]
            _check(
                case.xfail_reason is not None and "KI-011" in case.xfail_reason,
                f"{row.case_id}: an xfail row must cite KI-011",
            )
    uncovered = [capability.value for capability in CAPABILITIES if capability not in covered]
    _check(not uncovered, f"capabilities covered by no synthetic case: {uncovered}")


def render_matrix(cases: Sequence[SyntheticCase]) -> str:
    """Render the matrix as aligned text for CI logs."""
    rows = matrix_rows(cases)
    width = max(len(row.case_id) for row in rows)
    lines = ["native synthetic differential coverage (KI-011)"]
    lines.append(f"{'capability':<10}  {'case':<{width}}  {'status':<7}  limitations")
    for row in rows:
        limitations = "; ".join(row.limitations) if row.limitations else "-"
        lines.append(
            f"{row.capability.value:<10}  {row.case_id:<{width}}  {row.coverage.value:<7}  {limitations}"
        )
    covered = sorted({row.capability.value for row in rows if row.coverage is Coverage.COVERED})
    xfail = sorted({row.capability.value for row in rows if row.coverage is Coverage.XFAIL})
    lines.append(f"covered: {', '.join(covered)}")
    lines.append(f"xfail (KI-011): {', '.join(xfail) or '-'}")
    return "\n".join(lines)
