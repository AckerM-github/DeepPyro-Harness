"""Append-only run traces for native sessions.

The trace store is deliberately separate from the session log: a session is
durable, authoritative and replayable, while a trace is a short-lived
observability side channel that may be sampled. This module owns the M1 record
shapes, normalized hashing and the small validation surface needed to read a
trace back and join it to session events by run/turn/step ids.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from deeppyro.session.schema import Event

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RUN_ID_RE = re.compile(r"^run_[0-9a-f]{16}$")
TURN_ID_RE = re.compile(r"^turn_[0-9a-f]{16}$")
STEP_ID_RE = re.compile(r"^step_[0-9a-f]{16}$")
SPAN_ID_RE = re.compile(r"^span_[0-9a-f]{16}$")
CALL_ID_RE = re.compile(r"^call_[0-9a-f]{16}$")
MESSAGE_ID_RE = re.compile(r"^msg_(?:missing_)?[0-9a-f]{16}$")
SESSION_ID_RE = re.compile(r"^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FINISH_REASONS = frozenset({"stop", "tool_calls", "length", "error", "aborted", "content_filter"})

RUN_FIELDS = frozenset({"type", "run_id", "trace_id", "session_id", "started_at", "provider", "model"})
MODEL_CALL_FIELDS = frozenset(
    {
        "type",
        "trace_id",
        "span_id",
        "run_id",
        "turn_id",
        "step_id",
        "provider",
        "model",
        "prompt_hash",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "finish_reason",
        "retry_count",
        "ts",
        "cached_input_tokens",
        "reasoning_tokens",
        "cost_usd",
        "context_message_ids",
        "error_code",
    }
)
TOOL_CALL_FIELDS = frozenset(
    {
        "type",
        "trace_id",
        "span_id",
        "run_id",
        "turn_id",
        "step_id",
        "call_id",
        "tool",
        "args_hash",
        "result_size",
        "is_error",
        "retry_count",
        "latency_ms",
        "ts",
        "capability",
        "risk",
        "error_code",
        "idempotency_key",
    }
)
REQUIRED_MODEL_CALL = frozenset(
    {
        "type",
        "trace_id",
        "span_id",
        "run_id",
        "turn_id",
        "step_id",
        "provider",
        "model",
        "prompt_hash",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "finish_reason",
        "retry_count",
        "ts",
    }
)
REQUIRED_TOOL_CALL = frozenset(
    {
        "type",
        "trace_id",
        "span_id",
        "run_id",
        "turn_id",
        "step_id",
        "call_id",
        "tool",
        "args_hash",
        "result_size",
        "is_error",
        "retry_count",
        "latency_ms",
        "ts",
    }
)


class TraceFormatError(ValueError):
    """A trace file or record does not satisfy the M1 trace contract."""


def canonical_json(value: object) -> str:
    """Return the canonical JSON text used for hashing.

    Objects are sorted by key, tuples become arrays, whitespace is removed and
    non-finite numbers, non-string object keys and non-JSON values are refused.
    This is the single normalization rule for prompt and tool argument hashes.
    """
    normalized = _normalize(value, set())
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def hash_payload(value: object) -> str:
    """Hash one JSON value with the canonical normalization rule."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def args_hash(arguments: str | object) -> str:
    """Hash tool arguments, parsing a raw JSON string first when needed."""
    if isinstance(arguments, str):
        try:
            value = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise TraceFormatError(f"tool arguments are not valid JSON: {exc.msg}") from exc
    else:
        value = arguments
    return hash_payload(value)


def prompt_hash(prompt: object) -> str:
    """Hash a provider prompt payload with the canonical normalization rule."""
    return hash_payload(prompt)


def _normalize(value: object, stack: set[int]) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraceFormatError("canonical JSON refuses non-finite numbers")
        return value
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in stack:
            raise TraceFormatError("canonical JSON refuses cyclic objects")
        stack.add(marker)
        try:
            result: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TraceFormatError("canonical JSON requires string object keys")
                result[key] = _normalize(item, stack)
            return {key: result[key] for key in sorted(result)}
        finally:
            stack.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in stack:
            raise TraceFormatError("canonical JSON refuses cyclic objects")
        stack.add(marker)
        try:
            return [_normalize(item, stack) for item in value]
        finally:
            stack.remove(marker)
    raise TraceFormatError(f"canonical JSON refuses value of type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The first line of a trace file: Run identity and model selection."""

    run_id: str
    trace_id: str
    session_id: str
    started_at: int
    provider: str
    model: str
    type: Literal["run"] = field(default="run", init=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "run",
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "provider": self.provider,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> RunRecord:
        _only_keys(raw, RUN_FIELDS, "run")
        _require_keys(raw, RUN_FIELDS - {"type"}, "run")
        if raw.get("type") != "run":
            raise TraceFormatError("trace record 'type' must be 'run'")
        return cls(
            run_id=_run_id(raw["run_id"], "run_id"),
            trace_id=_trace_id(raw["trace_id"], "trace_id"),
            session_id=_session_id(raw["session_id"]),
            started_at=_int(raw["started_at"], "started_at"),
            provider=_text(raw["provider"], "provider"),
            model=_text(raw["model"], "model"),
        )


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    """One provider attempt or successful call made during a Run."""

    trace_id: str
    span_id: str
    run_id: str
    turn_id: str
    step_id: str
    provider: str
    model: str
    prompt_hash: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    finish_reason: str
    retry_count: int
    ts: int
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    context_message_ids: tuple[str, ...] = ()
    error_code: str | None = None
    type: Literal["model_call"] = field(default="model_call", init=False)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "type": "model_call",
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "provider": self.provider,
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "retry_count": self.retry_count,
            "ts": self.ts,
        }
        _put_optional(data, "cached_input_tokens", self.cached_input_tokens)
        _put_optional(data, "reasoning_tokens", self.reasoning_tokens)
        _put_optional(data, "cost_usd", self.cost_usd)
        if self.context_message_ids:
            data["context_message_ids"] = list(self.context_message_ids)
        _put_optional(data, "error_code", self.error_code)
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ModelCallRecord:
        _only_keys(raw, MODEL_CALL_FIELDS, "model_call")
        _require_keys(raw, REQUIRED_MODEL_CALL, "model_call")
        if raw.get("type") != "model_call":
            raise TraceFormatError("trace record 'type' must be 'model_call'")
        finish_reason = _text(raw["finish_reason"], "finish_reason")
        if finish_reason not in FINISH_REASONS:
            raise TraceFormatError(f"model_call finish_reason {finish_reason!r} is not supported")
        context = raw.get("context_message_ids")
        message_ids: tuple[str, ...] = ()
        if context is not None:
            if not isinstance(context, list):
                raise TraceFormatError("model_call context_message_ids must be an array")
            message_ids = tuple(_message_id(item, "context_message_ids") for item in context)
        cost = raw.get("cost_usd")
        if cost is not None and (not isinstance(cost, (int, float)) or isinstance(cost, bool)):
            raise TraceFormatError("model_call cost_usd must be a number")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            cost = float(cost)
            if not math.isfinite(cost) or cost < 0:
                raise TraceFormatError("model_call cost_usd must be a finite non-negative number")
        return cls(
            trace_id=_trace_id(raw["trace_id"], "trace_id"),
            span_id=_span_id(raw["span_id"]),
            run_id=_run_id(raw["run_id"], "run_id"),
            turn_id=_turn_id(raw["turn_id"], "turn_id"),
            step_id=_step_id(raw["step_id"], "step_id"),
            provider=_text(raw["provider"], "provider"),
            model=_text(raw["model"], "model"),
            prompt_hash=_hash(raw["prompt_hash"], "prompt_hash"),
            input_tokens=_int(raw["input_tokens"], "input_tokens"),
            output_tokens=_int(raw["output_tokens"], "output_tokens"),
            latency_ms=_int(raw["latency_ms"], "latency_ms"),
            finish_reason=finish_reason,
            retry_count=_int(raw["retry_count"], "retry_count"),
            ts=_int(raw["ts"], "ts"),
            cached_input_tokens=_optional_int(raw.get("cached_input_tokens"), "cached_input_tokens"),
            reasoning_tokens=_optional_int(raw.get("reasoning_tokens"), "reasoning_tokens"),
            cost_usd=cost,
            context_message_ids=message_ids,
            error_code=_optional_text(raw.get("error_code"), "error_code"),
        )


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One tool execution attempt made during a Run."""

    trace_id: str
    span_id: str
    run_id: str
    turn_id: str
    step_id: str
    call_id: str
    tool: str
    args_hash: str
    result_size: int
    is_error: bool
    retry_count: int
    latency_ms: int
    ts: int
    capability: str | None = None
    risk: str | None = None
    error_code: str | None = None
    idempotency_key: str | None = None
    type: Literal["tool_call"] = field(default="tool_call", init=False)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "type": "tool_call",
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "step_id": self.step_id,
            "call_id": self.call_id,
            "tool": self.tool,
            "args_hash": self.args_hash,
            "result_size": self.result_size,
            "is_error": self.is_error,
            "retry_count": self.retry_count,
            "latency_ms": self.latency_ms,
            "ts": self.ts,
        }
        _put_optional(data, "capability", self.capability)
        _put_optional(data, "risk", self.risk)
        _put_optional(data, "error_code", self.error_code)
        _put_optional(data, "idempotency_key", self.idempotency_key)
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ToolCallRecord:
        _only_keys(raw, TOOL_CALL_FIELDS, "tool_call")
        _require_keys(raw, REQUIRED_TOOL_CALL, "tool_call")
        if raw.get("type") != "tool_call":
            raise TraceFormatError("trace record 'type' must be 'tool_call'")
        is_error = raw["is_error"]
        if not isinstance(is_error, bool):
            raise TraceFormatError("tool_call is_error must be a boolean")
        return cls(
            trace_id=_trace_id(raw["trace_id"], "trace_id"),
            span_id=_span_id(raw["span_id"]),
            run_id=_run_id(raw["run_id"], "run_id"),
            turn_id=_turn_id(raw["turn_id"], "turn_id"),
            step_id=_step_id(raw["step_id"], "step_id"),
            call_id=_call_id(raw["call_id"]),
            tool=_text(raw["tool"], "tool"),
            args_hash=_hash(raw["args_hash"], "args_hash"),
            result_size=_int(raw["result_size"], "result_size"),
            is_error=is_error,
            retry_count=_int(raw["retry_count"], "retry_count"),
            latency_ms=_int(raw["latency_ms"], "latency_ms"),
            ts=_int(raw["ts"], "ts"),
            capability=_optional_text(raw.get("capability"), "capability"),
            risk=_optional_text(raw.get("risk"), "risk"),
            error_code=_optional_text(raw.get("error_code"), "error_code"),
            idempotency_key=_optional_text(raw.get("idempotency_key"), "idempotency_key"),
        )


type TraceRecord = RunRecord | ModelCallRecord | ToolCallRecord


def record_from_dict(raw: Mapping[str, object]) -> TraceRecord:
    """Parse one trace record object, dispatching on its type field."""
    record_type = raw.get("type")
    if record_type == "run":
        return RunRecord.from_dict(raw)
    if record_type == "model_call":
        return ModelCallRecord.from_dict(raw)
    if record_type == "tool_call":
        return ToolCallRecord.from_dict(raw)
    raise TraceFormatError(f"unknown trace record type {record_type!r}")


@dataclass(frozen=True, slots=True)
class TraceLog:
    """One parsed trace file."""

    path: Path
    run: RunRecord
    records: tuple[ModelCallRecord | ToolCallRecord, ...]


class TraceStore:
    """Filesystem store with one compact JSONL file per Run."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            configured = os.environ.get("DEEPPYRO_TRACES_DIR")
            root = configured if configured else Path.home() / ".dpro" / "traces"
        self.root = Path(root)

    def path_for(self, run_id: str) -> Path:
        return self.root / f"{_run_id(run_id, 'run_id')}.jsonl"

    def create_run(self, run: RunRecord) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(run.run_id)
        try:
            handle = path.open("x", encoding="utf-8", newline="\n")
        except FileExistsError as exc:
            raise TraceFormatError(f"trace for run {run.run_id!r} already exists") from exc
        with handle:
            handle.write(_json_line(run.to_dict()))
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def append(self, record: ModelCallRecord | ToolCallRecord) -> Path:
        if not isinstance(record, (ModelCallRecord, ToolCallRecord)):
            raise TraceFormatError("append requires a model_call or tool_call record")
        path = self.path_for(record.run_id)
        if not path.is_file():
            raise TraceFormatError(f"trace for run {record.run_id!r} does not exist")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(_json_line(record.to_dict()))
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def read(self, run_id: str) -> TraceLog:
        return read_trace(self.path_for(run_id))

    def append_run_record(self, record: ModelCallRecord | ToolCallRecord) -> Path:
        return self.append(record)


def read_trace(path: str | Path) -> TraceLog:
    """Parse and validate one trace file."""
    trace_path = Path(path)
    raw = trace_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise TraceFormatError(f"trace {trace_path} starts with a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TraceFormatError(f"trace {trace_path} is not valid UTF-8: {exc.reason}") from exc
    lines = text.splitlines()
    if not lines:
        raise TraceFormatError(f"trace {trace_path} is empty")
    run: RunRecord | None = None
    records: list[ModelCallRecord | ToolCallRecord] = []
    for number, line in enumerate(lines, 1):
        if not line:
            raise TraceFormatError(f"trace line {number} is blank")
        try:
            raw_record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TraceFormatError(f"trace line {number} is not valid JSON: {exc.msg}") from exc
        if not isinstance(raw_record, Mapping):
            raise TraceFormatError(f"trace line {number} is not an object")
        record = record_from_dict(raw_record)
        if number == 1:
            if not isinstance(record, RunRecord):
                raise TraceFormatError("trace first line must be a run record")
            run = record
            continue
        if isinstance(record, RunRecord):
            raise TraceFormatError("trace contains more than one run record")
        if run is None:
            raise TraceFormatError("trace is missing its run record")
        if record.run_id != run.run_id or record.trace_id != run.trace_id:
            raise TraceFormatError(f"trace line {number} does not belong to run {run.run_id!r}")
        records.append(record)
    assert run is not None
    return TraceLog(path=trace_path, run=run, records=tuple(records))


@dataclass(frozen=True, slots=True)
class TraceJoin:
    """A trace joined to the session events it describes."""

    run: RunRecord
    model_calls: tuple[ModelCallRecord, ...]
    tool_calls: tuple[ToolCallRecord, ...]


def join_trace(log: TraceLog, events: Sequence[Event]) -> TraceJoin:
    """Check that trace spans line up with session turn/step and tool calls."""
    run_events = [event for event in events if event.run_id == log.run.run_id]
    step_keys = {
        (event.turn_id, event.step_id)
        for event in run_events
        if event.turn_id is not None and event.step_id is not None
    }
    call_keys = {
        (event.turn_id, event.step_id, event.data.get("call_id"))
        for event in run_events
        if event.type == "tool.call"
    }
    model_calls: list[ModelCallRecord] = []
    tool_calls: list[ToolCallRecord] = []
    for record in log.records:
        if record.run_id != log.run.run_id or record.trace_id != log.run.trace_id:
            raise TraceFormatError("trace record does not match the run identity")
        if (record.turn_id, record.step_id) not in step_keys:
            raise TraceFormatError(f"trace {record.type} span {record.span_id} has no matching session step")
        if isinstance(record, ModelCallRecord):
            model_calls.append(record)
        else:
            if (record.turn_id, record.step_id, record.call_id) not in call_keys:
                raise TraceFormatError(
                    f"trace tool_call {record.call_id!r} has no matching session tool.call"
                )
            tool_calls.append(record)
    return TraceJoin(
        run=log.run,
        model_calls=tuple(model_calls),
        tool_calls=tuple(tool_calls),
    )


def _json_line(data: Mapping[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"


def _put_optional(data: dict[str, object], key: str, value: object | None) -> None:
    if value is not None:
        data[key] = value


def _only_keys(raw: Mapping[str, object], allowed: frozenset[str], subject: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise TraceFormatError(f"{subject} has unknown field {sorted(unknown)[0]!r}")


def _require_keys(raw: Mapping[str, object], required: frozenset[str], subject: str) -> None:
    for name in sorted(required):
        if name not in raw:
            raise TraceFormatError(f"{subject} lacks required field {name!r}")


def _text(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value:
        raise TraceFormatError(f"{subject} must be a non-empty string")
    return value


def _optional_text(value: object, subject: str) -> str | None:
    if value is None:
        return None
    return _text(value, subject)


def _int(value: object, subject: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TraceFormatError(f"{subject} must be a non-negative integer")
    return value


def _optional_int(value: object, subject: str) -> int | None:
    if value is None:
        return None
    return _int(value, subject)


def _run_id(value: object, subject: str) -> str:
    return _match(value, RUN_ID_RE, subject, "run_<16 hex>")


def _trace_id(value: object, subject: str) -> str:
    return _match(value, TRACE_ID_RE, subject, "32 hex characters")


def _turn_id(value: object, subject: str) -> str:
    return _match(value, TURN_ID_RE, subject, "turn_<16 hex>")


def _step_id(value: object, subject: str) -> str:
    return _match(value, STEP_ID_RE, subject, "step_<16 hex>")


def _span_id(value: object) -> str:
    return _match(value, SPAN_ID_RE, "span_id", "span_<16 hex>")


def _call_id(value: object) -> str:
    return _match(value, CALL_ID_RE, "call_id", "call_<16 hex>")


def _message_id(value: object, subject: str) -> str:
    return _match(value, MESSAGE_ID_RE, subject, "msg_<16 hex>")


def _session_id(value: object) -> str:
    return _match(value, SESSION_ID_RE, "session_id", "session-<uuid>")


def _hash(value: object, subject: str) -> str:
    return _match(value, HASH_RE, subject, "sha256:<64 hex>")


def _match(value: object, pattern: re.Pattern[str], subject: str, expected: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise TraceFormatError(f"{subject} must match {expected}")
    return value


__all__ = [
    "FINISH_REASONS",
    "ModelCallRecord",
    "RunRecord",
    "ToolCallRecord",
    "TraceFormatError",
    "TraceJoin",
    "TraceLog",
    "TraceRecord",
    "TraceStore",
    "args_hash",
    "canonical_json",
    "hash_payload",
    "join_trace",
    "prompt_hash",
    "read_trace",
    "record_from_dict",
]
