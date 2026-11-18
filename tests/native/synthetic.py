"""Synthetic coverage cases for the protocol surfaces the recorded baseline
cannot reach (KI-011).

The recorded fixtures under fixtures/native only carry real text and tool
traffic. Each JSON file under tests/fixtures/native/synthetic is a hand-authored,
offline schema 1 event stream for one capability, optionally plus the trace side
channel that carries per-call usage.

Running a case writes the native events into a temporary directory, reads them
back through NativeCodec, folds them with session.fold, and asserts the native
shape invariants: message round-trip, tool pairing, compaction replacement,
approval lifecycle, trace usage outside the session log, and one
capability-specific invariant. The cases are regression fixtures for the native
protocol surfaces; they do not compare against a second vocabulary.

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

from deeppyro.core.messages import CompactionSource, ImagePart, Message, TextPart
from deeppyro.observability.trace import (
    TraceFormatError,
    TraceJoin,
    TraceLog,
    join_trace,
    read_trace,
)
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.schema import (
    APPROVAL_OUTCOMES,
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

HANDOFF_NATIVE_TYPES = ("task.state", "task.handoff", "task.handoff.result")
RESERVED_SUBAGENT_TYPES = (
    "subagent.started",
    "subagent.finished",
    "team.member.changed",
    "team.task.changed",
)
NATIVE_KINDS = frozenset(kind.value for kind in EventKind)
MESSAGE_EVENT_TYPES = frozenset(
    {
        "user.message",
        "system.message",
        "developer.message",
        "assistant.message",
        "tool.result",
    }
)
MCP_TOOL_RE = re.compile(r"^mcp__([A-Za-z0-9_.-]+)__([A-Za-z0-9_.-]+)$")
CHILD_ID_RE = re.compile(r"child-[0-9a-f]+")
TASK_ID_RE = re.compile(r"^task_[0-9a-f]{16}$")
CASE_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
ALLOWED_CASE_FIELDS = frozenset(
    {
        "case_id",
        "capability",
        "coverage",
        "title",
        "limitations",
        "xfail_reason",
        "native",
        "trace",
    }
)


class DifferentialMismatch(AssertionError):
    """A synthetic case does not satisfy the native shape contract."""


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
    """How a synthetic case participates in the coverage matrix."""

    COVERED = "covered"
    XFAIL = "xfail"


@dataclass(frozen=True, slots=True)
class SyntheticCase:
    """One hand-authored native case exercising a single protocol surface."""

    case_id: str
    capability: Capability
    title: str
    coverage: Coverage
    limitations: tuple[str, ...]
    xfail_reason: str | None
    native: Mapping[str, object] | None
    trace: Mapping[str, object] | None

    def require_representation(self) -> None:
        """Raise when schema 1 cannot record the handoff task facts."""
        if self.capability is not Capability.HANDOFF:
            return
        missing = [name for name in HANDOFF_NATIVE_TYPES if name not in NATIVE_KINDS]
        if missing:
            raise DifferentialMismatch(
                f"{self.case_id}: schema 1 lacks the handoff task kinds {missing}; KI-011 tracks the gap"
            )


@dataclass(frozen=True, slots=True)
class MatrixRow:
    """One line of the covered/uncovered matrix."""

    case_id: str
    capability: Capability
    coverage: Coverage
    limitations: tuple[str, ...]


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def load_cases(root: Path | None = None) -> list[SyntheticCase]:
    """Load every synthetic case from root (defaults to the checked-in set)."""
    base = FIXTURE_ROOT if root is None else root
    if not base.is_dir():
        raise DifferentialMismatch(f"synthetic fixture directory is missing: {base}")
    cases = [_parse_case(_read_json_object(path), path) for path in sorted(base.glob("*.json"))]
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
    unknown = sorted(set(raw) - ALLOWED_CASE_FIELDS)
    if unknown:
        raise DifferentialMismatch(f"{where}: unknown synthetic case field {unknown[0]!r}")
    case_id = _require_text(raw.get("case_id"), f"{where}: case_id")
    _check(
        CASE_ID_RE.fullmatch(case_id) is not None,
        f"{where}: case_id must be a lowercase slug",
    )
    _check(path.stem == case_id, f"{where}: case_id {case_id!r} does not match the file name")
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
    native_raw = raw.get("native")
    trace_raw = raw.get("trace")
    native = None if native_raw is None else _require_mapping(native_raw, f"{where}: native")
    trace = None if trace_raw is None else _require_mapping(trace_raw, f"{where}: trace")
    if coverage is Coverage.COVERED:
        _check(native is not None, f"{where}: covered case needs the native side")
    if native is not None:
        _validate_native(where, native)
    if trace is not None:
        records = _require_sequence(trace.get("records"), f"{where}: trace.records")
        _check(len(records) > 0, f"{where}: trace.records must not be empty")
        for record in records:
            _require_mapping(record, f"{where}: trace record")
    return SyntheticCase(
        case_id=case_id,
        capability=capability,
        title=_require_text(raw.get("title"), f"{where}: title"),
        coverage=coverage,
        limitations=limitations,
        xfail_reason=reason,
        native=native,
        trace=trace,
    )


def _validate_native(where: str, native: Mapping[str, object]) -> None:
    session_id = _require_text(native.get("session_id"), f"{where}: native.session_id")
    _check(
        re.fullmatch(SESSION_ID_PATTERN, session_id) is not None,
        f"{where}: native.session_id does not match schema 1",
    )
    _require_int(native.get("created_at"), f"{where}: native.created_at")
    for name in ("preset", "origin"):
        value = native.get(name)
        if value is not None:
            _require_text(value, f"{where}: native.{name}")
    events = _require_sequence(native.get("events"), f"{where}: native.events")
    _check(len(events) > 0, f"{where}: native.events must not be empty")
    for raw_event in events:
        event = _require_mapping(raw_event, f"{where}: native event")
        kind = _require_text(event.get("type"), f"{where}: native event type")
        _check(kind in NATIVE_KINDS, f"{where}: native event type {kind!r} is not a schema 1 kind")
        data = event.get("data")
        if data is not None:
            _require_mapping(data, f"{where}: native event data")
        for name in ("run_id", "turn_id", "step_id", "trace_id"):
            value = event.get(name)
            if value is not None:
                _require_text(value, f"{where}: native event {name}")


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


def _walk_keys(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from _walk_keys(item)


# ---------------------------------------------------------------------------
# Materializing the native side through the production codec
# ---------------------------------------------------------------------------


def compare_case(case: SyntheticCase, tmp_path: Path) -> None:
    """Materialize one synthetic case and assert its native shape invariants."""
    case.require_representation()
    log = _materialize_native(case, tmp_path)
    view = fold(log.header, log.events)
    _check(view.issues == (), f"{case.case_id}: native fold reported issues {view.issues!r}")
    _check_header(case, log)
    _check_events(case, log.events)
    expected, system = _expected_messages(case, log.events)
    _check_messages(case, view, expected, system)
    _check_tool_pairing(case, log.events, expected)
    trace = None if case.trace is None else _materialize_trace(case, tmp_path)
    usage = _check_usage(case, log, trace)
    _CAPABILITY_CHECKS[case.capability](case, log.events, view, usage)


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
    try:
        return read_trace(path)
    except TraceFormatError as exc:
        raise DifferentialMismatch(f"{case.case_id}: trace does not parse: {exc}") from exc


def _json_line(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Native shape invariants
# ---------------------------------------------------------------------------


def _check_header(case: SyntheticCase, log: SessionLog) -> None:
    native = case.native
    assert native is not None
    _check(
        log.header.id == _require_text(native.get("session_id"), f"{case.case_id}: native.session_id"),
        f"{case.case_id}: header id differs from the fixture",
    )
    _check(
        log.header.created_at == _require_int(native.get("created_at"), f"{case.case_id}: native.created_at"),
        f"{case.case_id}: header created_at differs from the fixture",
    )
    _check(log.header.schema == 1, f"{case.case_id}: fixture must be a schema 1 session")


def _check_events(case: SyntheticCase, events: Sequence[Event]) -> None:
    _check(
        [event.seq for event in events] == list(range(1, len(events) + 1)),
        f"{case.case_id}: event seq is not contiguous",
    )
    previous = 0
    for event in events:
        _check(event.kind is not None, f"{case.case_id}: non-core event {event.type!r}")
        if event.type != EventKind.RUN_CHECKPOINT.value:
            leaked = sorted(set(_walk_keys(event.data)) & FORBIDDEN_USAGE_KEYS)
            _check(
                not leaked,
                f"{case.case_id}: session log carries usage facts {leaked} "
                "(schema 8.5: they belong in the trace side channel)",
            )
        _check(event.ts >= previous, f"{case.case_id}: event ts went backwards at seq {event.seq}")
        previous = event.ts


def _event_message(case: SyntheticCase, event: Event) -> Message:
    raw = _require_mapping(event.data.get("message"), f"{case.case_id}: {event.type} message")
    return Message.from_dict(raw)


def _expected_messages(
    case: SyntheticCase, events: Sequence[Event]
) -> tuple[tuple[Message, ...], Message | None]:
    messages: list[Message] = []
    system: Message | None = None
    for event in events:
        if event.type in MESSAGE_EVENT_TYPES:
            message = _event_message(case, event)
            if message.role == "system":
                _check(system is None, f"{case.case_id}: more than one system message")
                system = message
            else:
                messages.append(message)
        elif event.type == EventKind.COMPACTION_DONE.value:
            messages = _apply_compaction(case, event, messages)
    return tuple(messages), system


def _apply_compaction(case: SyntheticCase, event: Event, messages: list[Message]) -> list[Message]:
    covers = tuple(
        _require_text(item, f"{case.case_id}: compaction cover")
        for item in _require_sequence(event.data.get("covers"), f"{case.case_id}: compaction covers")
    )
    _check(bool(covers), f"{case.case_id}: compaction.done must cover at least one message")
    summary = _event_message(case, event)
    positions = [index for index, message in enumerate(messages) if message.id in covers]
    _check(bool(positions), f"{case.case_id}: compaction covers no visible message")
    first = min(positions)
    window = tuple(message.id for message in messages[first : first + len(covers)])
    _check(
        window == covers,
        f"{case.case_id}: compaction covers are not a contiguous message span "
        f"(covered={covers!r}, visible={window!r})",
    )
    remaining = [message for message in messages if message.id not in covers]
    remaining.insert(first, summary)
    return remaining


def _check_messages(
    case: SyntheticCase,
    view: SessionView,
    expected: tuple[Message, ...],
    system: Message | None,
) -> None:
    _check(view.system == system, f"{case.case_id}: folded system message differs")
    _check(
        view.messages == expected,
        f"{case.case_id}: folded messages differ from the event stream "
        f"(folded={[message.id for message in view.messages]!r}, "
        f"events={[message.id for message in expected]!r})",
    )
    ids = [message.id for message in view.messages]
    _check(len(set(ids)) == len(ids), f"{case.case_id}: folded messages carry duplicate ids")


def _check_tool_pairing(case: SyntheticCase, events: Sequence[Event], messages: tuple[Message, ...]) -> None:
    calls: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.type != EventKind.TOOL_CALL.value:
            continue
        call_id = _require_text(event.data.get("call_id"), f"{case.case_id}: tool.call call_id")
        _check(call_id not in calls, f"{case.case_id}: duplicate tool.call {call_id}")
        calls[call_id] = (
            _require_text(event.data.get("name"), f"{case.case_id}: tool.call name"),
            _canonical_arguments(
                _require_text(event.data.get("arguments"), f"{case.case_id}: tool.call arguments"),
                case.case_id,
            ),
        )
    parts = 0
    for message in messages:
        if message.role != "assistant":
            continue
        for part in message.tool_calls():
            parts += 1
            _check(part.id in calls, f"{case.case_id}: assistant tool call {part.id} has no tool.call")
            name, arguments = calls[part.id]
            _check(name == part.name, f"{case.case_id}: tool.call {part.id} name differs")
            _check(
                _canonical_arguments(part.arguments, case.case_id) == arguments,
                f"{case.case_id}: tool.call {part.id} arguments differ",
            )
    _check(
        parts == len(calls),
        f"{case.case_id}: assistant tool-call parts and tool.call events disagree "
        f"(parts={parts}, calls={len(calls)})",
    )
    results = []
    for event in events:
        if event.type != EventKind.TOOL_RESULT.value:
            continue
        message = _event_message(case, event)
        call_id = _require_text(message.tool_call_id, f"{case.case_id}: tool.result tool_call_id")
        _check(call_id in calls, f"{case.case_id}: tool.result {call_id} has no tool.call")
        results.append(call_id)
    _check(
        sorted(results) == sorted(calls),
        f"{case.case_id}: every tool.call needs exactly one tool.result "
        f"(calls={sorted(calls)!r}, results={sorted(results)!r})",
    )


def _check_usage(case: SyntheticCase, log: SessionLog, trace: TraceLog | None) -> TraceJoin | None:
    for event in log.events:
        if event.type == EventKind.RUN_CHECKPOINT.value:
            continue
        leaked = sorted(set(_walk_keys(event.data)) & FORBIDDEN_USAGE_KEYS)
        _check(
            not leaked,
            f"{case.case_id}: session log carries usage facts {leaked} "
            "(schema 8.5: they belong in the trace side channel)",
        )
    if trace is None:
        return None
    try:
        joined = join_trace(trace, log.events)
    except TraceFormatError as exc:
        raise DifferentialMismatch(f"{case.case_id}: trace does not join the session: {exc}") from exc
    _check(
        joined.run.session_id == log.header.id,
        f"{case.case_id}: trace run session id differs from the session header",
    )
    trace_ids = {event.trace_id for event in log.events if event.trace_id is not None}
    _check(
        trace_ids <= {joined.run.trace_id},
        f"{case.case_id}: session events reference a different trace id {sorted(trace_ids)!r}",
    )
    for call in joined.model_calls:
        _check(call.input_tokens >= 0, f"{case.case_id}: model call input tokens must be non-negative")
        _check(call.output_tokens >= 0, f"{case.case_id}: model call output tokens must be non-negative")
        _check(call.latency_ms >= 0, f"{case.case_id}: model call latency must be non-negative")
        _check(call.prompt_hash.startswith("sha256:"), f"{case.case_id}: prompt hash must be sha256")
    names = {
        _require_text(event.data.get("call_id"), f"{case.case_id}: tool.call call_id"): _require_text(
            event.data.get("name"), f"{case.case_id}: tool.call name"
        )
        for event in log.events
        if event.type == EventKind.TOOL_CALL.value
    }
    for record in joined.tool_calls:
        _check(
            names.get(record.call_id) == record.tool,
            f"{case.case_id}: trace tool_call {record.call_id} does not match the session tool.call",
        )
    return joined


# ---------------------------------------------------------------------------
# Argument and text helpers
# ---------------------------------------------------------------------------


def _canonical_arguments(arguments: str, where: str) -> str:
    try:
        parsed: object = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise DifferentialMismatch(f"{where}: tool call arguments are not JSON: {exc}") from exc
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _arguments_object(case: SyntheticCase, arguments: str) -> Mapping[str, object]:
    try:
        return _require_mapping(json.loads(arguments), f"{case.case_id}: tool arguments")
    except json.JSONDecodeError as exc:
        raise DifferentialMismatch(f"{case.case_id}: tool arguments are not JSON: {exc}") from exc


def _message_text(message: Message) -> str:
    return "".join(part.text for part in message.content if isinstance(part, TextPart))


# ---------------------------------------------------------------------------
# Capability-specific invariants
# ---------------------------------------------------------------------------


def _check_image(
    case: SyntheticCase, events: Sequence[Event], view: SessionView, usage: TraceJoin | None
) -> None:
    del usage
    images: list[tuple[str, str]] = []
    for event in events:
        if event.type == EventKind.USER_MESSAGE.value:
            for part in _event_message(case, event).content:
                if isinstance(part, ImagePart):
                    images.append((part.attachment, part.media_type))
    _check(len(images) == 1, f"{case.case_id}: expected exactly one native image part")
    attachment, media_type = images[0]
    _check(media_type == "image/png", f"{case.case_id}: the synthetic image fixture is a PNG")
    _check(attachment.endswith(".png"), f"{case.case_id}: the image attachment must name a png")
    _check(
        any(
            isinstance(part, ImagePart) and part.attachment == attachment
            for message in view.messages
            for part in message.content
        ),
        f"{case.case_id}: fold dropped the image part from the message history",
    )


def _check_approval(
    case: SyntheticCase, events: Sequence[Event], view: SessionView, usage: TraceJoin | None
) -> None:
    del usage
    requested = [event for event in events if event.type == EventKind.APPROVAL_REQUESTED.value]
    decided = [event for event in events if event.type == EventKind.APPROVAL_DECIDED.value]
    _check(len(requested) == 1, f"{case.case_id}: expected one approval.requested")
    _check(len(decided) == 1, f"{case.case_id}: expected one approval.decided")
    request = requested[0].data
    approval_id = _require_text(request.get("approval_id"), f"{case.case_id}: approval_id")
    _check(
        _require_text(decided[0].data.get("approval_id"), f"{case.case_id}: decided approval_id")
        == approval_id,
        f"{case.case_id}: approval request and decision ids differ",
    )
    outcome = _require_text(decided[0].data.get("outcome"), f"{case.case_id}: approval outcome")
    _check(
        outcome in APPROVAL_OUTCOMES,
        f"{case.case_id}: approval outcome {outcome!r} is not a schema 1 outcome",
    )
    tool_call_id = _require_text(request.get("tool_call_id"), f"{case.case_id}: approval tool_call_id")
    calls = {
        _require_text(event.data.get("call_id"), f"{case.case_id}: tool.call call_id"): _require_text(
            event.data.get("name"), f"{case.case_id}: tool.call name"
        )
        for event in events
        if event.type == EventKind.TOOL_CALL.value
    }
    _check(
        tool_call_id in calls,
        f"{case.case_id}: approval request must reference a recorded tool.call",
    )
    _check(
        calls[tool_call_id] == _require_text(request.get("tool_name"), f"{case.case_id}: approval tool_name"),
        f"{case.case_id}: approval tool name differs from the tool.call",
    )
    _check(
        view.pending_approvals == (),
        f"{case.case_id}: a decided approval must not remain pending ({view.pending_approvals!r})",
    )


def _check_compaction(
    case: SyntheticCase, events: Sequence[Event], view: SessionView, usage: TraceJoin | None
) -> None:
    del usage
    done = [event for event in events if event.type == EventKind.COMPACTION_DONE.value]
    _check(len(done) == 1, f"{case.case_id}: expected one compaction.done")
    data = done[0].data
    covers = tuple(
        _require_text(item, f"{case.case_id}: compaction cover")
        for item in _require_sequence(data.get("covers"), f"{case.case_id}: compaction covers")
    )
    summary = _event_message(case, done[0])
    _check(
        isinstance(summary.source, CompactionSource),
        f"{case.case_id}: compaction summary must carry a compaction source",
    )
    assert isinstance(summary.source, CompactionSource)
    _check(
        summary.source.compaction_id
        == _require_text(data.get("compaction_id"), f"{case.case_id}: compaction_id"),
        f"{case.case_id}: compaction summary source id differs from compaction.done",
    )
    _check(
        summary.text().strip() != "",
        f"{case.case_id}: compaction summary must carry summary text",
    )
    _check(
        summary.id in {message.id for message in view.messages},
        f"{case.case_id}: compaction summary is missing from the folded view",
    )
    _check(
        not (set(covers) & {message.id for message in view.messages}),
        f"{case.case_id}: a compacted message survived in the folded view",
    )


def _check_mcp(
    case: SyntheticCase, events: Sequence[Event], view: SessionView, usage: TraceJoin | None
) -> None:
    del view
    names = [
        _require_text(event.data.get("name"), f"{case.case_id}: tool.call name")
        for event in events
        if event.type == EventKind.TOOL_CALL.value
    ]
    matches = [MCP_TOOL_RE.fullmatch(name) for name in names]
    _check(
        any(match is not None for match in matches), f"{case.case_id}: expected an mcp__<server>__<tool> call"
    )
    _check(
        not any(event.type.startswith("mcp") for event in events),
        f"{case.case_id}: schema 1 has no MCP lifecycle event; only the tool seam is recorded",
    )
    if usage is not None:
        _check(bool(usage.model_calls), f"{case.case_id}: the MCP trace must carry model calls")


def _check_ptc(
    case: SyntheticCase, events: Sequence[Event], view: SessionView, usage: TraceJoin | None
) -> None:
    del view, usage
    calls = [
        (
            _require_text(event.data.get("call_id"), f"{case.case_id}: tool.call call_id"),
            _require_text(event.data.get("name"), f"{case.case_id}: tool.call name"),
            _require_text(event.data.get("arguments"), f"{case.case_id}: tool.call arguments"),
        )
        for event in events
        if event.type == EventKind.TOOL_CALL.value
    ]
    ptc = [call for call in calls if call[1] == "ptc"]
    _check(len(ptc) == 1, f"{case.case_id}: expected exactly one ptc tool.call")
    arguments = _arguments_object(case, ptc[0][2])
    code = arguments.get("code")
    _check(isinstance(code, str) and code.strip() != "", f"{case.case_id}: ptc arguments need a code string")
    _check(
        not any(call[1] == "read_file" for call in calls),
        f"{case.case_id}: the nested ptc dispatch must stay inside the tool result",
    )
    _check(
        not any(event.type.startswith("ptc") for event in events),
        f"{case.case_id}: schema 1 has no PTC lifecycle event",
    )


def _check_subagent(
    case: SyntheticCase, events: Sequence[Event], view: SessionView, usage: TraceJoin | None
) -> None:
    del usage
    calls = [
        (
            _require_text(event.data.get("call_id"), f"{case.case_id}: tool.call call_id"),
            _require_text(event.data.get("name"), f"{case.case_id}: tool.call name"),
        )
        for event in events
        if event.type == EventKind.TOOL_CALL.value
    ]
    spawns = [call for call in calls if call[1] == "subagent"]
    _check(len(spawns) == 1, f"{case.case_id}: expected exactly one subagent tool.call")
    call_id = spawns[0][0]
    result_texts = [
        _message_text(_event_message(case, event))
        for event in events
        if event.type == EventKind.TOOL_RESULT.value and _event_message(case, event).tool_call_id == call_id
    ]
    _check(len(result_texts) == 1, f"{case.case_id}: the subagent call needs one tool.result")
    result_text = result_texts[0]
    child = CHILD_ID_RE.search(result_text)
    _check(child is not None, f"{case.case_id}: the subagent result must name the child id")
    _check("spawned" in result_text, f"{case.case_id}: the subagent result must report the spawn")
    assert child is not None
    _check(
        any(child.group(0) in _message_text(message) for message in view.messages),
        f"{case.case_id}: the folded message history must carry the child id",
    )
    _check(
        not any(event.type in RESERVED_SUBAGENT_TYPES for event in events),
        f"{case.case_id}: schema 1 has no subagent lifecycle event",
    )


def _check_handoff(
    case: SyntheticCase, events: Sequence[Event], view: SessionView, usage: TraceJoin | None
) -> None:
    del usage
    types = {event.type for event in events}
    for native_type in HANDOFF_NATIVE_TYPES:
        _check(native_type in types, f"{case.case_id}: the fixture must record {native_type!r}")
    states = [event for event in events if event.type == "task.state"]
    handoffs = [event for event in events if event.type == "task.handoff"]
    results = [event for event in events if event.type == "task.handoff.result"]
    _check(len(states) >= 2, f"{case.case_id}: expected task.state create and update facts")
    _check(len(handoffs) == 1, f"{case.case_id}: expected one task.handoff")
    _check(len(results) == 1, f"{case.case_id}: expected one task.handoff.result")
    task = _require_mapping(states[-1].data.get("task"), f"{case.case_id}: task.state task")
    task_id = _require_text(task.get("id"), f"{case.case_id}: task id")
    _check(TASK_ID_RE.fullmatch(task_id) is not None, f"{case.case_id}: task id does not match schema 1")
    _check(
        _require_text(task.get("status"), f"{case.case_id}: final task status") == "completed",
        f"{case.case_id}: the task must end completed",
    )
    handoff = handoffs[0].data
    run_id = _require_text(handoff.get("run_id"), f"{case.case_id}: handoff run_id")
    from_owner = _require_text(handoff.get("from"), f"{case.case_id}: handoff from")
    to_owner = _require_text(handoff.get("to"), f"{case.case_id}: handoff to")
    _check(to_owner.startswith("subagent:"), f"{case.case_id}: handoff target must name the subagent")
    summary = _require_text(handoff.get("summary"), f"{case.case_id}: handoff summary")
    open_items = tuple(
        _require_text(item, f"{case.case_id}: handoff open item")
        for item in _require_sequence(handoff.get("open_items"), f"{case.case_id}: handoff open_items")
    )
    budget = _require_mapping(handoff.get("budget"), f"{case.case_id}: handoff budget")
    max_steps = budget.get("max_steps")
    _check(
        isinstance(max_steps, int) and not isinstance(max_steps, bool) and max_steps > 0,
        f"{case.case_id}: handoff budget needs a positive max_steps",
    )
    leaked = sorted(set(_walk_keys(handoff)) & FORBIDDEN_USAGE_KEYS)
    _check(
        not leaked,
        f"{case.case_id}: handoff payload carries usage facts {leaked} "
        "(schema 8.5: they belong in the trace side channel)",
    )
    result = results[0].data
    _check(result.get("ok") is True, f"{case.case_id}: the handoff result must be ok")
    _check(
        _require_text(result.get("run_id"), f"{case.case_id}: handoff result run_id") == run_id,
        f"{case.case_id}: handoff result run id differs",
    )
    _check(
        _require_text(result.get("task_id"), f"{case.case_id}: handoff result task_id") == task_id,
        f"{case.case_id}: handoff result task id differs",
    )
    _check(len(view.tasks) == 1, f"{case.case_id}: the fold must project one task")
    _check(
        view.tasks[0].task_id == task_id and view.tasks[0].status == "completed",
        f"{case.case_id}: the folded task differs from the task.state facts",
    )
    _check(len(view.handoffs) == 1, f"{case.case_id}: the fold must project one handoff")
    fact = view.handoffs[0]
    _check(fact.run_id == run_id, f"{case.case_id}: the folded handoff run id differs")
    _check(fact.from_owner == from_owner, f"{case.case_id}: the folded handoff sender differs")
    _check(fact.to_owner == to_owner, f"{case.case_id}: the folded handoff target differs")
    _check(fact.summary == summary, f"{case.case_id}: the folded handoff summary differs")
    _check(fact.task_id == task_id, f"{case.case_id}: the folded handoff task id differs")
    _check(fact.open_items == open_items, f"{case.case_id}: the folded handoff open items differ")
    _check(fact.result_ok is True, f"{case.case_id}: the folded handoff must record the ok result")
    _check(
        fact.result == _optional_text(result.get("result"), f"{case.case_id}: handoff result text"),
        f"{case.case_id}: the folded handoff result text differs",
    )


_CAPABILITY_CHECKS: dict[
    Capability, Callable[[SyntheticCase, Sequence[Event], SessionView, TraceJoin | None], None]
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
        for case in sorted(cases, key=lambda case: (case.capability.value, case.case_id))
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
    lines = ["native synthetic coverage matrix (KI-011)"]
    lines.append(f"{'capability':<10}  {'case':<{width}}  {'status':<7}  limitations")
    for row in rows:
        limitations = "; ".join(row.limitations) if row.limitations else "-"
        lines.append(
            f"{row.capability.value:<10}  {row.case_id:<{width}}  {row.coverage.value:<7}  {limitations}"
        )
    covered = sorted({row.capability.value for row in rows if row.coverage is Coverage.COVERED})
    xfail = sorted({row.capability.value for row in rows if row.coverage is Coverage.XFAIL})
    lines.append(f"cases: {len(rows)} hand-authored offline schema-1 sessions")
    lines.append(f"covered: {', '.join(covered)}")
    lines.append(f"xfail (KI-011): {', '.join(xfail) or '-'}")
    return "\n".join(lines)
