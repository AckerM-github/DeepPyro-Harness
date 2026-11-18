"""Migrate sessions between the DSH v4 log format and the native schema 1 format.

The module holds the pure conversion functions (v4_to_native, native_to_v4)
plus a batch orchestrator (run_migration) shared by the dpyro migrate CLI and
the session_migrate kernel service. Conversions never touch a session store
directly; the caller passes explicit roots and the orchestrator owns every
filesystem effect: atomic writes, backups and read-back verification.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from deeppyro import paths
from deeppyro.config import load_config, session_dir
from deeppyro.core.errors import ConfigError, SessionError
from deeppyro.core.messages import (
    TOOL_CALL_ID_RE,
    CompactionSource,
    GoalSource,
    ImagePart,
    Message,
    MessageValidationError,
    ModelSource,
    PluginSource,
    ReasoningPart,
    RepairSource,
    SystemSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
    missing_tool_result_message,
)
from deeppyro.core.wire import LogRecord
from deeppyro.core.wire import SessionHeader as V4Header
from deeppyro.session.derive import derive_messages, fold_surface
from deeppyro.session.events import SURFACE_EVENTS, normalize_event
from deeppyro.session.fold import fold
from deeppyro.session.layout import discover_session_files, session_file_path
from deeppyro.session.reader import SessionRead, read_header, read_session_file
from deeppyro.session.schema import (
    PRESET_RE,
    SANDBOX_MODES,
    SESSION_ID_RE,
    Event,
    NativeCodec,
    SessionHeader,
    SessionLog,
)
from deeppyro.session.writer import encode_event_line, encode_header_line, event_to_dict

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_PARTIAL = 2

ABORTED_BY = frozenset({"user", "provider", "disposed"})
V4_MESSAGE_ROLES = {
    "system/message": "system",
    "developer/message": "developer",
    "user/message": "user",
    "assistant/message": "assistant",
    "tool/result": "tool",
}


class MigrationError(SessionError):
    """Raised when a session cannot be migrated."""


class SessionFormat(StrEnum):
    NATIVE = "native"
    V4 = "v4"
    UNKNOWN = "unknown"


class MigrationStatus(StrEnum):
    CONVERTED = "converted"
    PLANNED = "planned"
    UP_TO_DATE = "up-to-date"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NativeDocument:
    """A complete native schema 1 session file held in memory."""

    header: SessionHeader
    events: tuple[Event, ...]
    warnings: tuple[str, ...] = ()

    def encode(self) -> bytes:
        lines = [NativeCodec.encode_header(self.header)]
        lines.extend(NativeCodec.encode_event(event) for event in self.events)
        return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class V4Document:
    """A complete v4 session log held in memory."""

    header: V4Header
    records: tuple[LogRecord, ...]
    warnings: tuple[str, ...] = ()

    def encode(self) -> bytes:
        chunks = [encode_header_line(self.header)]
        chunks.extend(encode_event_line(event_to_dict(record)) for record in self.records)
        return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class MigrationEntry:
    """The outcome for one scanned session file."""

    status: MigrationStatus
    source_format: SessionFormat
    target_format: SessionFormat
    session_id: str | None = None
    source: Path | None = None
    target: Path | None = None
    reason: str | None = None
    backup: Path | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "sourceFormat": str(self.source_format),
            "targetFormat": str(self.target_format),
            "sessionId": self.session_id,
            "source": str(self.source) if self.source is not None else None,
            "target": str(self.target) if self.target is not None else None,
            "reason": self.reason,
            "backup": str(self.backup) if self.backup is not None else None,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """The full result of one migration run."""

    entries: tuple[MigrationEntry, ...] = ()
    dry_run: bool = False
    backup: bool = True

    @property
    def converted(self) -> int:
        return sum(1 for entry in self.entries if entry.status is MigrationStatus.CONVERTED)

    @property
    def planned(self) -> int:
        return sum(1 for entry in self.entries if entry.status is MigrationStatus.PLANNED)

    @property
    def up_to_date(self) -> int:
        return sum(1 for entry in self.entries if entry.status is MigrationStatus.UP_TO_DATE)

    @property
    def skipped(self) -> int:
        return sum(1 for entry in self.entries if entry.status is MigrationStatus.SKIPPED)

    @property
    def failed(self) -> int:
        return sum(1 for entry in self.entries if entry.status is MigrationStatus.FAILED)

    @property
    def success(self) -> int:
        return self.converted + self.planned + self.up_to_date

    @property
    def exit_code(self) -> int:
        if self.failed and not self.success:
            return EXIT_FAILURE
        if self.failed or self.skipped:
            return EXIT_PARTIAL if self.success else EXIT_FAILURE
        return EXIT_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "dryRun": self.dry_run,
            "backup": self.backup,
            "converted": self.converted,
            "planned": self.planned,
            "upToDate": self.up_to_date,
            "skipped": self.skipped,
            "failed": self.failed,
            "exitCode": self.exit_code,
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class _Conversion:
    payload: bytes
    source_id: str
    target_id: str
    cwd: str
    warnings: tuple[str, ...]


def _digest(seed: str, length: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def _as_format(value: SessionFormat | str) -> SessionFormat:
    if isinstance(value, SessionFormat):
        return value
    try:
        return SessionFormat(value)
    except ValueError as exc:
        raise MigrationError(f"unknown session format {value!r}") from exc


def map_session_id(raw_id: str) -> str:
    """Return a schema 1 session id for an arbitrary v4 session id."""
    if SESSION_ID_RE.fullmatch(raw_id):
        return raw_id
    digest = _digest(f"v4-session:{raw_id}", 32)
    return f"session-{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


class _NativeIdFactory:
    """Deterministic schema 1 ids derived from one v4 log."""

    def __init__(self, namespace: str) -> None:
        self.namespace = namespace
        self.run_id = "run_" + _digest(f"run:{namespace}", 16)
        self.trace_id = _digest(f"trace:{namespace}", 32)
        self._used: set[str] = set()
        self._calls: dict[str, str] = {}

    def _unique(self, prefix: str, key: str) -> str:
        for attempt in range(1024):
            salt = key if attempt == 0 else f"{key}:{attempt}"
            candidate = prefix + _digest(f"{self.namespace}:{salt}", 16)
            if candidate not in self._used:
                self._used.add(candidate)
                return candidate
        raise MigrationError(f"cannot allocate a unique id for {key!r}")

    def turn_id(self, number: int) -> str:
        return "turn_" + _digest(f"{self.namespace}:turn:{number}", 16)

    def step_id(self, turn: int, step: int) -> str:
        return "step_" + _digest(f"{self.namespace}:step:{turn}:{step}", 16)

    def message_id(self, raw_id: str, seq: int) -> str:
        key = f"message:{raw_id if raw_id else f'seq-{seq}'}"
        return self._unique("msg_", key)

    def synthetic_message_id(self, key: str) -> str:
        return self._unique("msg_", f"synthetic:{key}")

    def call_id(self, raw_id: str) -> str:
        known = self._calls.get(raw_id)
        if known is not None:
            return known
        if TOOL_CALL_ID_RE.fullmatch(raw_id) and raw_id not in self._used:
            self._used.add(raw_id)
            self._calls[raw_id] = raw_id
            return raw_id
        native = self._unique("call_", f"call:{raw_id if raw_id else 'anonymous'}")
        self._calls[raw_id] = native
        return native


def _compact_json(value: object) -> str:
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _fallback_source(role: str) -> Any:
    if role == "assistant":
        return ModelSource(provider="unknown", model="unknown")
    if role in ("system", "developer"):
        return SystemSource()
    if role == "tool":
        return ToolSource(call_id="call_0000000000000000")
    return UserSource()


def _native_source(raw_source: Mapping[str, Any], *, role: str, ids: _NativeIdFactory) -> Any:
    kind = raw_source.get("kind")
    if kind == "model":
        provider = raw_source.get("provider")
        model = raw_source.get("model")
        return ModelSource(
            provider=provider if isinstance(provider, str) and provider else "unknown",
            model=model if isinstance(model, str) and model else "unknown",
        )
    if kind == "tool":
        call_id = raw_source.get("callId")
        return ToolSource(call_id=ids.call_id(call_id if isinstance(call_id, str) else ""))
    if kind in ("system-prompt", "system"):
        return SystemSource()
    if kind == "user":
        return UserSource()
    if kind == "goal":
        goal_id = raw_source.get("goal_id")
        revision = raw_source.get("revision")
        round_number = raw_source.get("round")
        try:
            return GoalSource(
                goal_id=str(goal_id),
                revision=int(revision),  # type: ignore[arg-type]
                round=int(round_number),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError):
            return _fallback_source(role)
    if kind == "compaction":
        compaction_id = raw_source.get("compaction_id")
        if isinstance(compaction_id, str) and compaction_id:
            return CompactionSource(compaction_id=compaction_id)
        return _fallback_source(role)
    if kind == "repair":
        reason = raw_source.get("reason")
        return RepairSource(reason=reason if isinstance(reason, str) and reason else "missing-tool-result")
    if kind == "plugin":
        plugin = raw_source.get("plugin")
        if isinstance(plugin, str) and plugin:
            return PluginSource(plugin=plugin)
        return _fallback_source(role)
    return _fallback_source(role)


def _convert_v4_message(
    ids: _NativeIdFactory,
    raw: Mapping[str, Any],
    *,
    role: str,
    seq: int,
    time_ms: int,
) -> tuple[Message | None, tuple[str, ...]]:
    warnings: list[str] = []
    raw_id = raw.get("id")
    message_id = ids.message_id(raw_id if isinstance(raw_id, str) else "", seq)
    source_raw = raw.get("source")
    source_map: Mapping[str, Any] = source_raw if isinstance(source_raw, Mapping) else {}
    content: list[Any] = []
    raw_parts = raw.get("content")
    parts: Sequence[Any] = raw_parts if isinstance(raw_parts, list) else ()
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                content.append(TextPart(text))
        elif part_type == "reasoning":
            text = part.get("text")
            if isinstance(text, str) and text:
                content.append(ReasoningPart(text))
        elif part_type == "tool-call":
            call_raw = part.get("id")
            call_id = ids.call_id(call_raw if isinstance(call_raw, str) else "")
            name = part.get("name")
            name = name if isinstance(name, str) and name else "tool"
            content.append(
                ToolCallPart(id=call_id, name=name, arguments=_compact_json(part.get("arguments")))
            )
        elif part_type == "image":
            warnings.append(
                f"message {raw_id or '?'}: image part dropped (native schema has no image attachments)"
            )
        else:
            warnings.append(f"message {raw_id or '?'}: content part {part_type!r} dropped")
    tool_call_id: str | None = None
    is_error = False
    source: Any
    if role == "tool":
        call_raw = raw.get("toolCallId")
        if not isinstance(call_raw, str) or not call_raw:
            call_raw = source_map.get("callId")
        if not isinstance(call_raw, str) or not call_raw:
            warnings.append(f"tool message {raw_id or '?'} has no tool call id; skipped")
            return None, tuple(warnings)
        tool_call_id = ids.call_id(call_raw)
        source = ToolSource(call_id=tool_call_id)
        is_error = raw.get("isError") is True
    else:
        source = _native_source(source_map, role=role, ids=ids)
    if not content:
        if role != "tool":
            warnings.append(f"{role} message {raw_id or '?'} has no text content; skipped")
            return None, tuple(warnings)
        content.append(TextPart("(empty tool result)"))
    try:
        message = Message(
            id=message_id,
            role=role,  # type: ignore[arg-type]
            content=tuple(content),
            created_at=time_ms,
            source=source,
            tool_call_id=tool_call_id,
            is_error=is_error,
        )
    except (MessageValidationError, ValueError) as exc:
        warnings.append(f"message {raw_id or '?'} cannot be converted: {exc}")
        return None, tuple(warnings)
    return message, tuple(warnings)


def _native_header_from_v4(header: V4Header) -> SessionHeader:
    raw_cwd = header.cwd or ""
    try:
        cwd = raw_cwd if Path(raw_cwd).is_absolute() else str(Path(raw_cwd or ".").resolve())
    except (OSError, ValueError):
        cwd = str(Path.cwd())
    preset = header.agentPreset if isinstance(header.agentPreset, str) else ""
    if not PRESET_RE.fullmatch(preset):
        preset = "standard"
    parent_session = header.parentSession
    parent_id = (
        parent_session
        if isinstance(parent_session, str) and SESSION_ID_RE.fullmatch(parent_session)
        else None
    )
    delegation_depth = int(header.delegationDepth) if isinstance(header.delegationDepth, int) else 0
    return SessionHeader(
        id=map_session_id(header.id),
        created_at=max(0, int(header.createdAt)),
        cwd=cwd,
        preset=preset,
        parent_id=parent_id,
        origin="subagent" if header.origin == "subagent" else "user",
        delegation_depth=max(0, delegation_depth),
    )


def _native_turn_reason(raw: object, *, step_count: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {"kind": "completed"}
    kind = raw.get("kind")
    if kind == "aborted":
        inner = raw.get("reason")
        by = inner.get("kind") if isinstance(inner, Mapping) else raw.get("by")
        return {"kind": "aborted", "by": by if by in ABORTED_BY else "user"}
    if kind == "error":
        error = raw.get("error")
        message = error.get("message") if isinstance(error, Mapping) else raw.get("message")
        return {
            "kind": "error",
            "message": message if isinstance(message, str) and message else "turn failed",
        }
    if kind == "max-steps":
        return {"kind": "max_steps", "limit": max(1, step_count)}
    if kind == "max-tokens":
        return {"kind": "max_tokens"}
    if kind == "blocked":
        reason = raw.get("reason")
        message = reason.get("message") if isinstance(reason, Mapping) else reason
        return {
            "kind": "blocked",
            "message": message if isinstance(message, str) and message else "turn blocked",
        }
    return {"kind": "completed"}


def _int_field(data: Mapping[str, Any], name: str) -> int | None:
    value = data.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _NativeBuilder:
    """Emit a schema 1 event stream from a v4 record stream."""

    def __init__(self, header: SessionHeader, ids: _NativeIdFactory) -> None:
        self.header = header
        self.ids = ids
        self.events: list[Event] = []
        self.warnings: list[str] = []
        self.turn_reasons: dict[int, dict[str, Any]] = {}
        self.seq = 0
        self.last_ts = header.created_at
        self.current_turn: int | None = None
        self.current_step: int | None = None
        self.last_turn = 0
        self.steps_by_turn: dict[int, list[int]] = {}
        self.calls: dict[str, dict[str, Any]] = {}

    def _stamp(self, ts: int) -> int:
        self.last_ts = max(self.last_ts, ts)
        return self.last_ts

    def emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        ts: int,
        run: bool = False,
        turn: bool = False,
        step: bool = False,
    ) -> Event:
        self.seq += 1
        turn_id = (
            self.ids.turn_id(self.current_turn) if (turn or step) and self.current_turn is not None else None
        )
        step_id: str | None = None
        if step and self.current_turn is not None and self.current_step is not None:
            step_id = self.ids.step_id(self.current_turn, self.current_step)
        event = Event(
            type=event_type,
            seq=self.seq,
            ts=self._stamp(ts),
            data=data,
            run_id=self.ids.run_id if run or turn or step else None,
            turn_id=turn_id,
            step_id=step_id,
            trace_id=self.ids.trace_id if run or turn or step else None,
        )
        self.events.append(event)
        return event

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def start(self, ts: int) -> None:
        self.emit("run.started", {}, ts=ts, run=True)

    def turn_started(self, number: int, ts: int) -> None:
        self.current_turn = number
        self.current_step = None
        self.steps_by_turn.setdefault(number, [])
        self.emit("turn.started", {}, ts=ts, turn=True)

    def ensure_turn(self, hint: int | None, ts: int) -> int:
        if self.current_turn is None:
            number = hint if isinstance(hint, int) and hint > self.last_turn else self.last_turn + 1
            self.turn_started(number, ts)
        turn = self.current_turn
        assert turn is not None
        return turn

    def step_started(self, turn: int, step: int, ts: int) -> None:
        self.current_turn = turn
        self.current_step = step
        self.steps_by_turn.setdefault(turn, []).append(step)
        self.emit("step.started", {}, ts=ts, step=True)

    def next_step(self, turn: int) -> int:
        return max(self.steps_by_turn.get(turn, []), default=0) + 1

    def ensure_step(self, turn_hint: int | None, step_hint: int | None, ts: int) -> tuple[int, int]:
        turn = self.ensure_turn(turn_hint, ts)
        if self.current_step is None:
            step = step_hint if isinstance(step_hint, int) and step_hint > 0 else self.next_step(turn)
            self.step_started(turn, step, ts)
        step_value = self.current_step
        assert step_value is not None
        return turn, step_value

    def end_step(self, ts: int, *, turn_reason: Mapping[str, Any] | None = None) -> None:
        if self.current_turn is None or self.current_step is None:
            return
        turn = self.current_turn
        step = self.current_step
        if isinstance(turn_reason, Mapping) and turn_reason.get("kind") == "completed":
            for call_id, info in self.calls.items():
                if info["turn"] != turn or info["step"] != step or info["result"]:
                    continue
                message = missing_tool_result_message(call_id, ts)
                self.emit("tool.result", {"call_id": call_id, "message": message.to_dict()}, ts=ts, step=True)
                info["result"] = True
                self.warn(
                    f"turn {turn} step {step}: tool call {call_id} had no result; "
                    "inserted a synthetic interrupted-tool-result"
                )
        self.emit("step.ended", {}, ts=ts, step=True)
        self.current_step = None

    def end_turn(self, reason: Mapping[str, Any], ts: int) -> None:
        if self.current_turn is None:
            return
        turn = self.current_turn
        self.end_step(ts, turn_reason=reason)
        self.emit("turn.ended", {"reason": dict(reason)}, ts=ts, turn=True)
        self.last_turn = turn
        self.current_turn = None
        self.current_step = None

    def finish(self, ts: int) -> None:
        while self.current_turn is not None:
            self.end_turn({"kind": "aborted", "by": "disposed"}, ts)
        self.emit("run.finished", {"status": "completed"}, ts=ts, run=True)

    def emit_message(self, message: Message, event_type: str, ts: int) -> None:
        if event_type != "assistant.message":
            self.emit(event_type, {"message": message.to_dict()}, ts=ts)
            return
        if self.current_turn is None or self.current_step is None:
            return
        has_calls = any(isinstance(part, ToolCallPart) for part in message.content)
        self.emit(
            event_type,
            {"message": message.to_dict(), "finish_reason": "tool_calls" if has_calls else "stop"},
            ts=ts,
            step=True,
        )
        for part in message.content:
            if not isinstance(part, ToolCallPart):
                continue
            if part.id in self.calls:
                self.warn(f"assistant tool call {part.id} appears more than once; kept the first call")
                continue
            self.emit(
                "tool.call",
                {"call_id": part.id, "name": part.name, "arguments": part.arguments},
                ts=ts,
                step=True,
            )
            self.calls[part.id] = {"turn": self.current_turn, "step": self.current_step, "result": False}

    def emit_tool_call(
        self,
        call_id: str,
        name: str,
        arguments: str,
        *,
        ts: int,
        turn_hint: int | None = None,
        step_hint: int | None = None,
    ) -> None:
        if call_id in self.calls:
            return
        turn, step = self.ensure_step(turn_hint, step_hint, ts)
        message = Message(
            id=self.ids.synthetic_message_id(f"tool-call:{call_id}"),
            role="assistant",  # type: ignore[arg-type]
            content=(ToolCallPart(id=call_id, name=name, arguments=arguments),),
            created_at=ts,
            source=ModelSource(provider="unknown", model="unknown"),
        )
        self.emit(
            "assistant.message",
            {"message": message.to_dict(), "finish_reason": "tool_calls"},
            ts=ts,
            step=True,
        )
        self.emit("tool.call", {"call_id": call_id, "name": name, "arguments": arguments}, ts=ts, step=True)
        self.calls[call_id] = {"turn": turn, "step": step, "result": False}
        self.warn(f"tool call {call_id} had no assistant message; inserted a synthetic one")

    def emit_tool_result(
        self,
        message: Message,
        *,
        ts: int,
        turn_hint: int | None = None,
        step_hint: int | None = None,
    ) -> None:
        call_id = message.tool_call_id
        if call_id is None:
            return
        info = self.calls.get(call_id)
        if info is None:
            self.emit_tool_call(call_id, "tool", "{}", ts=ts, turn_hint=turn_hint, step_hint=step_hint)
            info = self.calls.get(call_id)
        if info is None or info["result"]:
            return
        if self.current_turn != info["turn"] or self.current_step != info["step"]:
            self.warn(f"tool result for {call_id} is outside its call step; skipped")
            return
        self.emit("tool.result", {"call_id": call_id, "message": message.to_dict()}, ts=ts, step=True)
        info["result"] = True


def v4_to_native(read: SessionRead) -> NativeDocument:
    """Convert one parsed v4 session log into a native schema 1 document."""
    header = _native_header_from_v4(read.header)
    ids = _NativeIdFactory(header.id)
    builder = _NativeBuilder(header, ids)
    warnings: list[str] = []
    records = read.records
    try:
        nodes = fold_surface(records, strict=True)
    except SessionError as exc:
        warnings.append(f"surface fold used tolerant mode: {exc}")
        nodes = fold_surface(records, strict=False)
    visible = {node.seq for node in nodes}

    turn_steps: dict[int, int] = {}
    for record in records:
        if record.type != "step/start":
            continue
        turn_number = _int_field(record.data, "turn")
        if turn_number is not None:
            turn_steps[turn_number] = turn_steps.get(turn_number, 0) + 1
    reasons: dict[int, dict[str, Any]] = {}
    for record in records:
        if record.type != "turn/end":
            continue
        turn_number = _int_field(record.data, "turn")
        if turn_number is not None:
            reasons[turn_number] = _native_turn_reason(
                record.data.get("reason"),
                step_count=turn_steps.get(turn_number, 1),
            )
    builder.turn_reasons = reasons

    builder.start(header.created_at)
    for record in records:
        event_type = record.type
        ts = record.time
        if event_type in SURFACE_EVENTS:
            if record.seq not in visible:
                continue
            role = V4_MESSAGE_ROLES[event_type]
            raw_message = record.data if event_type == "user/message" else record.data.get("message")
            if not isinstance(raw_message, Mapping):
                warnings.append(f"{event_type} at seq {record.seq} has no message object; skipped")
                continue
            message, message_warnings = _convert_v4_message(
                ids, raw_message, role=role, seq=record.seq, time_ms=ts
            )
            warnings.extend(message_warnings)
            if message is None:
                continue
            if role == "assistant" and builder.current_step is None:
                builder.ensure_step(_int_field(record.data, "turn"), _int_field(record.data, "step"), ts)
            if role == "tool":
                builder.emit_tool_result(
                    message,
                    ts=ts,
                    turn_hint=_int_field(record.data, "turn"),
                    step_hint=_int_field(record.data, "step"),
                )
            else:
                builder.emit_message(message, event_type.replace("/", "."), ts)
        elif event_type == "turn/start":
            hint = _int_field(record.data, "turn")
            if builder.current_turn is not None:
                builder.end_turn({"kind": "aborted", "by": "disposed"}, ts)
            builder.turn_started(hint if hint is not None else builder.last_turn + 1, ts)
        elif event_type == "turn/end":
            hint = _int_field(record.data, "turn")
            reason = reasons.get(hint, {"kind": "completed"}) if hint is not None else {"kind": "completed"}
            builder.end_turn(reason, ts)
        elif event_type == "step/start":
            turn_hint = _int_field(record.data, "turn")
            step_hint = _int_field(record.data, "step")
            turn = builder.ensure_turn(turn_hint, ts)
            if builder.current_step is not None:
                builder.end_step(ts, turn_reason=reasons.get(turn))
            step = step_hint if step_hint is not None else builder.next_step(turn)
            builder.step_started(turn, step, ts)
        elif event_type == "step/end":
            turn_hint = _int_field(record.data, "turn")
            builder.end_step(ts, turn_reason=reasons.get(turn_hint) if turn_hint is not None else None)
        elif event_type == "tool/call":
            raw_call = record.data.get("callId")
            if not isinstance(raw_call, str):
                warnings.append(f"tool/call at seq {record.seq} has no call id; skipped")
                continue
            name = record.data.get("name")
            builder.emit_tool_call(
                ids.call_id(raw_call),
                name if isinstance(name, str) and name else "tool",
                _compact_json(record.data.get("arguments")),
                ts=ts,
                turn_hint=_int_field(record.data, "turn"),
                step_hint=_int_field(record.data, "step"),
            )
        elif event_type == "session/title":
            title = record.data.get("title")
            source = record.data.get("source")
            if isinstance(title, str) and source in ("user", "llm"):
                builder.emit("session.title", {"title": title, "source": source}, ts=ts)
        elif event_type == "model/selection":
            provider = record.data.get("provider")
            model = record.data.get("model")
            if isinstance(provider, str) and provider and isinstance(model, str) and model:
                builder.emit("model.selected", {"provider": provider, "model": model}, ts=ts)
        elif event_type == "sandbox/mode":
            mode = record.data.get("mode")
            if isinstance(mode, str) and mode in SANDBOX_MODES:
                builder.emit("sandbox.mode", {"mode": mode}, ts=ts)
        elif event_type == "working-directory/change":
            cwd = record.data.get("cwd")
            if isinstance(cwd, str) and cwd and Path(cwd).is_absolute():
                builder.emit("workspace.cwd_changed", {"cwd": cwd}, ts=ts)
    builder.finish(builder.last_ts)
    return NativeDocument(
        header=header,
        events=tuple(builder.events),
        warnings=tuple(warnings + builder.warnings),
    )


def _v4_source(message: Message) -> dict[str, Any]:
    source = message.source
    if isinstance(source, ToolSource):
        return {"kind": "tool", "callId": source.call_id}
    if isinstance(source, ModelSource):
        return {
            "kind": "model",
            "provider": source.provider or "unknown",
            "model": source.model or "unknown",
        }
    if isinstance(source, SystemSource):
        return {"kind": "system-prompt"}
    if isinstance(source, UserSource):
        return {"kind": "user"}
    if isinstance(source, GoalSource):
        return {"kind": "goal", "goal_id": source.goal_id, "revision": source.revision, "round": source.round}
    if isinstance(source, CompactionSource):
        return {"kind": "compaction", "compaction_id": source.compaction_id}
    if isinstance(source, RepairSource):
        return {"kind": "repair", "reason": source.reason}
    if isinstance(source, PluginSource):
        return {"kind": "plugin", "plugin": source.plugin}
    return {"kind": "user"}


def _v4_part(part: Any) -> dict[str, Any] | None:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    if isinstance(part, ReasoningPart):
        return {"type": "reasoning", "text": part.text}
    if isinstance(part, ToolCallPart):
        return {"type": "tool-call", "id": part.id, "name": part.name, "arguments": part.arguments}
    if isinstance(part, ImagePart):
        return {"type": "image", "attachment": part.attachment, "media_type": part.media_type}
    return None


def _v4_message(message: Message) -> dict[str, Any]:
    content = [item for item in (_v4_part(part) for part in message.content) if item is not None]
    if message.role == "system":
        source = {"kind": "system-prompt"}
    elif message.role == "assistant":
        source = _v4_source(message)
        if source.get("kind") != "model":
            source = {"kind": "model", "provider": "unknown", "model": "unknown"}
    elif message.role == "tool":
        call_id = message.tool_call_id or ""
        source = {"kind": "tool", "callId": call_id}
    else:
        source = _v4_source(message)
    raw: dict[str, Any] = {
        "id": message.id,
        "role": message.role,
        "content": content,
        "source": source,
    }
    if message.tool_call_id is not None:
        raw["toolCallId"] = message.tool_call_id
    if message.is_error:
        raw["isError"] = True
    return raw


def _v4_turn_reason(reason: Mapping[str, Any]) -> dict[str, Any]:
    kind = reason.get("kind")
    if kind == "aborted":
        by = reason.get("by")
        return {"kind": "aborted", "reason": {"kind": by if by in ABORTED_BY else "user"}}
    if kind == "error":
        message = reason.get("message")
        return {
            "kind": "error",
            "error": {"message": message if isinstance(message, str) else "turn failed"},
        }
    if kind == "max_steps":
        return {"kind": "max-steps"}
    if kind == "max_tokens":
        return {"kind": "max-tokens"}
    if kind == "blocked":
        message = reason.get("message")
        return {
            "kind": "blocked",
            "reason": {"message": message if isinstance(message, str) else "turn blocked"},
        }
    return {"kind": "completed"}


class _V4Builder:
    """Emit a v4 record stream from native schema 1 events."""

    def __init__(self, header: V4Header) -> None:
        self.header = header
        self.records: list[LogRecord] = []
        self.warnings: list[str] = []
        self.seq = 0
        self.turn = 0
        self.step = 0
        self.call_seqs: dict[str, int] = {}

    def add(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        time: int,
        source_seqs: Sequence[int] | None = None,
    ) -> int:
        raw: dict[str, Any] = {"type": event_type, "seq": self.seq, "time": max(0, time), "data": data}
        if event_type in SURFACE_EVENTS:
            raw["surfaceOp"] = "append"
        if source_seqs is not None:
            raw["sourceEventSeqs"] = list(source_seqs)
        record = LogRecord.model_validate(normalize_event(raw))
        self.records.append(record)
        self.seq += 1
        return self.seq - 1

    def emit_tool_call(self, call_id: str, name: str, arguments: str, *, time: int) -> int:
        return self.add(
            "tool/call",
            {
                "turn": self.turn,
                "step": self.step,
                "callId": call_id,
                "name": name,
                "arguments": arguments,
            },
            time=time,
        )

    def ensure_call(self, call_id: str, *, time: int) -> int:
        known = self.call_seqs.get(call_id)
        if known is not None:
            return known
        message = {
            "id": "msg_" + _digest(f"v4:call:{call_id}", 16),
            "role": "assistant",
            "content": [{"type": "tool-call", "id": call_id, "name": "tool", "arguments": "{}"}],
            "source": {"kind": "model", "provider": "unknown", "model": "unknown"},
        }
        self.add("assistant/message", {"turn": self.turn, "step": self.step, "message": message}, time=time)
        seq = self.emit_tool_call(call_id, "tool", "{}", time=time)
        self.call_seqs[call_id] = seq
        self.warnings.append(f"tool call {call_id} had no assistant message; inserted a synthetic one")
        return seq


def native_to_v4(log: SessionLog) -> V4Document:
    """Convert one parsed native schema 1 session log into a v4 document."""
    native = log.header
    header = V4Header(
        type="session",
        version=4,
        id=native.id,
        createdAt=max(0, int(native.created_at)),
        cwd=native.cwd,
        isSeeded=False,
        delegationDepth=max(0, int(native.delegation_depth)),
        agentPreset=native.preset,
        parentSession=native.parent_id,
        origin="subagent" if native.origin == "subagent" else None,
    )
    builder = _V4Builder(header)
    for event in log.events:
        time = event.ts
        if event.type == "turn.started":
            builder.turn += 1
            builder.step = 0
            builder.add("turn/start", {"turn": builder.turn}, time=time)
        elif event.type == "turn.ended":
            reason = event.data.get("reason")
            builder.add(
                "turn/end",
                {
                    "turn": builder.turn,
                    "reason": _v4_turn_reason(reason if isinstance(reason, Mapping) else {}),
                },
                time=time,
            )
        elif event.type == "step.started":
            builder.step += 1
            builder.add("step/start", {"turn": builder.turn, "step": builder.step}, time=time)
        elif event.type == "step.ended":
            builder.add("step/end", {"turn": builder.turn, "step": builder.step}, time=time)
        elif event.type == "user.message":
            message = _native_message(event, builder)
            if message is not None:
                builder.add("user/message", _v4_message(message), time=time)
        elif event.type in ("system.message", "developer.message"):
            message = _native_message(event, builder)
            if message is not None:
                builder.add(event.type.replace(".", "/"), {"message": _v4_message(message)}, time=time)
        elif event.type == "assistant.message":
            message = _native_message(event, builder)
            if message is None:
                continue
            builder.add(
                "assistant/message",
                {"turn": builder.turn, "step": builder.step, "message": _v4_message(message)},
                time=time,
            )
            for part in message.content:
                if not isinstance(part, ToolCallPart):
                    continue
                builder.call_seqs[part.id] = builder.emit_tool_call(
                    part.id, part.name, part.arguments, time=time
                )
        elif event.type == "tool.call":
            call_id = event.data.get("call_id")
            if not isinstance(call_id, str) or not call_id or call_id in builder.call_seqs:
                continue
            name = event.data.get("name")
            builder.call_seqs[call_id] = builder.emit_tool_call(
                call_id,
                name if isinstance(name, str) and name else "tool",
                _compact_json(event.data.get("arguments")),
                time=time,
            )
        elif event.type == "tool.result":
            raw_message = event.data.get("message")
            if not isinstance(raw_message, Mapping):
                continue
            try:
                message = Message.from_dict(raw_message)
            except (MessageValidationError, ValueError) as exc:
                builder.warnings.append(f"tool result message skipped: {exc}")
                continue
            call_id = message.tool_call_id
            if call_id is None:
                continue
            seq = builder.ensure_call(call_id, time=time)
            builder.add(
                "tool/result",
                {"turn": builder.turn, "step": builder.step, "message": _v4_message(message)},
                time=time,
                source_seqs=[seq],
            )
        elif event.type == "model.selected":
            provider = event.data.get("provider")
            model = event.data.get("model")
            if isinstance(provider, str) and isinstance(model, str):
                builder.add("model/selection", {"provider": provider, "model": model}, time=time)
        elif event.type == "session.title":
            title = event.data.get("title")
            source = event.data.get("source")
            if isinstance(title, str) and isinstance(source, str):
                builder.add("session/title", {"title": title, "source": source}, time=time)
        elif event.type == "sandbox.mode":
            mode = event.data.get("mode")
            if isinstance(mode, str):
                builder.add("sandbox/mode", {"mode": mode}, time=time)
        elif event.type == "workspace.cwd_changed":
            cwd = event.data.get("cwd")
            if isinstance(cwd, str):
                builder.add("working-directory/change", {"cwd": cwd}, time=time)
    return V4Document(header=header, records=tuple(builder.records), warnings=tuple(builder.warnings))


def _native_message(event: Event, builder: _V4Builder) -> Message | None:
    raw = event.data.get("message")
    if not isinstance(raw, Mapping):
        builder.warnings.append(f"{event.type} at seq {event.seq} has no message object; skipped")
        return None
    try:
        return Message.from_dict(raw)
    except (MessageValidationError, ValueError) as exc:
        builder.warnings.append(f"{event.type} at seq {event.seq} message skipped: {exc}")
        return None


def detect_format(path: str | Path) -> SessionFormat:
    """Detect the on-disk format of one session file."""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise MigrationError(f"cannot read session file {target}: {exc}") from exc
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            read_header(target)
        except (SessionError, ValueError):
            return SessionFormat.UNKNOWN
        return SessionFormat.V4
    first_line = data.split(b"\n", 1)[0].lstrip(b"\xef\xbb\xbf")
    if not first_line:
        return SessionFormat.UNKNOWN
    try:
        raw = json.loads(first_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SessionFormat.UNKNOWN
    if not isinstance(raw, Mapping):
        return SessionFormat.UNKNOWN
    if raw.get("version") == 4:
        return SessionFormat.V4
    if raw.get("type") == "session" and raw.get("schema") == 1:
        return SessionFormat.NATIVE
    return SessionFormat.UNKNOWN


def _session_header_of(path: Path, session_format: SessionFormat) -> SessionHeader | V4Header:
    if session_format is SessionFormat.V4:
        return read_header(path)
    raw = path.read_bytes().split(b"\n", 1)[0]
    return NativeCodec.decode_header(json.loads(raw.decode("utf-8")))


def _convert_file(path: Path, source_format: SessionFormat) -> _Conversion:
    if source_format is SessionFormat.V4:
        read = read_session_file(path)
        document = v4_to_native(read)
        source_id = read.header.id
        target_id = document.header.id
        cwd = document.header.cwd
        payload = document.encode()
        warnings = document.warnings
    else:
        log = NativeCodec.read(path)
        document = native_to_v4(log)
        source_id = log.header.id
        target_id = document.header.id
        cwd = document.header.cwd
        payload = document.encode()
        warnings = document.warnings
    return _Conversion(
        payload=payload,
        source_id=source_id,
        target_id=target_id,
        cwd=cwd,
        warnings=warnings,
    )


def _target_path(
    target_format: SessionFormat,
    *,
    session_id: str,
    cwd: str,
    native_root: Path,
    v4_root: Path,
) -> Path:
    if target_format is SessionFormat.NATIVE:
        return native_root / f"{session_id}.jsonl"
    return session_file_path(v4_root, session_id, cwd)


def _verify_payload(payload: bytes, target_format: SessionFormat, target: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="dpyro-migrate-verify-") as directory:
        check = Path(directory) / target.name
        check.write_bytes(payload)
        if target_format is SessionFormat.NATIVE:
            log = NativeCodec.read(check)
            fold(log.header, log.events)
        else:
            read = read_session_file(check)
            derive_messages(read.records)


def _write_atomic(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise MigrationError(f"cannot write session {target}: {exc}") from exc


def _backup_target(target: Path) -> Path:
    candidate = target.with_name(target.name + ".bak")
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = target.with_name(f"{target.name}.bak.{counter}")
    shutil.copy2(target, candidate)
    return candidate


def _resolve_native_root(root: Path | None) -> Path:
    return Path(root) if root is not None else paths.sessions_dir()


def _resolve_v4_root(root: Path | None) -> Path:
    if root is not None:
        return Path(root)
    try:
        return session_dir(load_config(create=False))
    except ConfigError:
        return paths.sessions_dir()


def _candidate_paths(native_root: Path, v4_root: Path) -> list[Path]:
    seen: set[Path] = set()
    candidates: list[Path] = []
    for root in (v4_root, native_root):
        for path in discover_session_files(root):
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(path)
    return sorted(candidates, key=lambda item: str(item))


def run_migration(
    *,
    to: SessionFormat | str,
    session: str | None = None,
    all: bool = False,
    dry_run: bool = False,
    backup: bool = True,
    yes: bool = False,
    native_root: Path | None = None,
    v4_root: Path | None = None,
) -> MigrationReport:
    """Convert every selected session file from one format to the other."""
    target_format = _as_format(to)
    if target_format is SessionFormat.UNKNOWN:
        raise MigrationError("target format must be 'native' or 'v4'")
    if session is not None and all:
        raise MigrationError("choose either a single session or all sessions")
    if session is None and not all:
        raise MigrationError("pass a session id or select all sessions")
    native = _resolve_native_root(native_root)
    v4 = _resolve_v4_root(v4_root)
    entries: list[MigrationEntry] = []
    matched = False
    for path in _candidate_paths(native, v4):
        try:
            source_format = detect_format(path)
        except MigrationError as exc:
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.SKIPPED,
                    source_format=SessionFormat.UNKNOWN,
                    target_format=target_format,
                    source=path,
                    reason=str(exc),
                )
            )
            continue
        if source_format is SessionFormat.UNKNOWN:
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.SKIPPED,
                    source_format=source_format,
                    target_format=target_format,
                    source=path,
                    reason="unrecognised session format",
                )
            )
            continue
        if source_format is target_format:
            source_id: str | None = None
            try:
                source_id = _session_header_of(path, source_format).id
            except (SessionError, ValueError, OSError, KeyError, TypeError):
                source_id = None
            if session is not None and session not in {source_id, path.stem}:
                continue
            matched = True
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.UP_TO_DATE,
                    source_format=source_format,
                    target_format=target_format,
                    session_id=source_id,
                    source=path,
                    target=path,
                )
            )
            continue
        try:
            conversion = _convert_file(path, source_format)
        except (MigrationError, SessionError, ValueError, OSError, KeyError, TypeError) as exc:
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.SKIPPED,
                    source_format=source_format,
                    target_format=target_format,
                    source=path,
                    reason=str(exc),
                )
            )
            continue
        target = _target_path(
            target_format,
            session_id=conversion.target_id,
            cwd=conversion.cwd,
            native_root=native,
            v4_root=v4,
        )
        if session is not None and session not in {
            conversion.source_id,
            conversion.target_id,
            path.stem,
            target.stem,
        }:
            continue
        matched = True
        if target.resolve() == path.resolve():
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.SKIPPED,
                    source_format=source_format,
                    target_format=target_format,
                    session_id=conversion.target_id,
                    source=path,
                    target=target,
                    reason="source and target are the same file",
                )
            )
            continue
        existing: bytes | None = None
        if target.exists():
            try:
                existing = target.read_bytes()
            except OSError as exc:
                entries.append(
                    MigrationEntry(
                        status=MigrationStatus.FAILED,
                        source_format=source_format,
                        target_format=target_format,
                        session_id=conversion.target_id,
                        source=path,
                        target=target,
                        reason=str(exc),
                    )
                )
                continue
        if existing is not None and existing == conversion.payload:
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.UP_TO_DATE,
                    source_format=source_format,
                    target_format=target_format,
                    session_id=conversion.target_id,
                    source=path,
                    target=target,
                    warnings=conversion.warnings,
                )
            )
            continue
        if existing is not None and (dry_run or not yes):
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.SKIPPED,
                    source_format=source_format,
                    target_format=target_format,
                    session_id=conversion.target_id,
                    source=path,
                    target=target,
                    reason="target exists with different content; pass --yes to overwrite",
                    warnings=conversion.warnings,
                )
            )
            continue
        if dry_run:
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.PLANNED,
                    source_format=source_format,
                    target_format=target_format,
                    session_id=conversion.target_id,
                    source=path,
                    target=target,
                    warnings=conversion.warnings,
                )
            )
            continue
        saved_backup: Path | None = None
        try:
            _verify_payload(conversion.payload, target_format, target)
            if backup and target.exists():
                saved_backup = _backup_target(target)
            _write_atomic(target, conversion.payload)
        except (MigrationError, SessionError, OSError, ValueError) as exc:
            entries.append(
                MigrationEntry(
                    status=MigrationStatus.FAILED,
                    source_format=source_format,
                    target_format=target_format,
                    session_id=conversion.target_id,
                    source=path,
                    target=target,
                    reason=str(exc),
                    warnings=conversion.warnings,
                )
            )
            continue
        entries.append(
            MigrationEntry(
                status=MigrationStatus.CONVERTED,
                source_format=source_format,
                target_format=target_format,
                session_id=conversion.target_id,
                source=path,
                target=target,
                backup=saved_backup,
                warnings=conversion.warnings,
            )
        )
    if session is not None and not matched:
        entries.append(
            MigrationEntry(
                status=MigrationStatus.FAILED,
                source_format=SessionFormat.UNKNOWN,
                target_format=target_format,
                session_id=session,
                reason=f"session {session!r} not found",
            )
        )
    return MigrationReport(entries=tuple(entries), dry_run=dry_run, backup=backup)


__all__ = [
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "MigrationEntry",
    "MigrationError",
    "MigrationReport",
    "MigrationStatus",
    "NativeDocument",
    "SessionFormat",
    "V4Document",
    "detect_format",
    "map_session_id",
    "native_to_v4",
    "run_migration",
    "v4_to_native",
]
