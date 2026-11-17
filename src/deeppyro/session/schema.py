"""Schema 1 native session codec, validator, reader, and writer.

The codec is the single source of truth for the physical format documented in
docs/native/schema.md: a header line, compact JSON event lines, strict sequence
and timestamp checks, and a closed data shape per EventKind. Reading and
writing deliberately share the same validators so an appended event can never
be one the reader would reject.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from deeppyro.core.ids import now_ms
from deeppyro.core.messages import (
    MESSAGE_ID_RE,
    MISSING_MESSAGE_ID_RE,
    CompactionSource,
    Message,
    MessageValidationError,
)
from deeppyro.session.errors import (
    CorruptLineError,
    EventFieldError,
    HeaderError,
    LineTooLargeError,
    MessageError,
    OrderingError,
    PluginNamespaceError,
    SequenceError,
    SessionLockedError,
    SessionV4Error,
    TimestampError,
    UnknownEventError,
    UnsupportedSchemaError,
)

SCHEMA_VERSION = 1
MAX_LINE_BYTES = 32 * 1024 * 1024
SESSION_ID_PATTERN = r"^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
PRESET_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"
RUN_ID_PATTERN = r"^run_[0-9a-f]{16}$"
TURN_ID_PATTERN = r"^turn_[0-9a-f]{16}$"
STEP_ID_PATTERN = r"^step_[0-9a-f]{16}$"
TRACE_ID_PATTERN = r"^[0-9a-f]{32}$"
PLUGIN_EVENT_PATTERN = r"^plugin\.[a-z][a-z0-9-]{0,31}\.[a-z][a-z0-9._-]{0,63}$"
SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)
PRESET_RE = re.compile(PRESET_PATTERN)
RUN_ID_RE = re.compile(RUN_ID_PATTERN)
TURN_ID_RE = re.compile(TURN_ID_PATTERN)
STEP_ID_RE = re.compile(STEP_ID_PATTERN)
TRACE_ID_RE = re.compile(TRACE_ID_PATTERN)
PLUGIN_EVENT_RE = re.compile(PLUGIN_EVENT_PATTERN)
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

ORIGINS: frozenset[str] = frozenset({"user", "headless", "subagent", "fork"})
FINISH_REASONS: frozenset[str] = frozenset(
    {"stop", "tool_calls", "length", "error", "aborted", "content_filter"}
)
APPROVAL_OUTCOMES: frozenset[str] = frozenset({"allowed", "rejected", "cancelled", "unavailable"})
PERMISSION_POLICIES: frozenset[str] = frozenset({"ask", "never"})
SANDBOX_MODES: frozenset[str] = frozenset({"read-only", "workspace-write", "danger-full-access"})
TITLE_SOURCES: frozenset[str] = frozenset({"user", "llm"})
PRESET_MODES: frozenset[str] = frozenset({"default", "team"})
TODO_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "completed"})
GOAL_PHASES: frozenset[str] = frozenset({"active", "paused", "blocked", "complete"})
ABORTED_BY: frozenset[str] = frozenset({"user", "provider", "disposed"})
RUN_FINISH_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
CHECKPOINT_STATUSES: frozenset[str] = frozenset({"running", "completed", "failed", "cancelled"})
TOOL_CALL_STATES: frozenset[str] = frozenset({"pending", "running", "completed", "failed"})
IDEMPOTENCY_KEY_PATTERN = r"^idem:[0-9a-f]{64}$"
IDEMPOTENCY_KEY_RE = re.compile(IDEMPOTENCY_KEY_PATTERN)

HEADER_FIELDS = {
    "type",
    "schema",
    "id",
    "created_at",
    "cwd",
    "preset",
    "parent_id",
    "origin",
    "delegation_depth",
}
ENVELOPE_FIELDS = {"type", "seq", "ts", "data", "run_id", "turn_id", "step_id", "trace_id"}


class EventKind(StrEnum):
    """Core event types: the 24 M1 kinds plus the 3 M2 run kinds (schema 5.2 and 5.5)."""

    TURN_STARTED = "turn.started"
    TURN_ENDED = "turn.ended"
    STEP_STARTED = "step.started"
    STEP_ENDED = "step.ended"
    USER_MESSAGE = "user.message"
    SYSTEM_MESSAGE = "system.message"
    DEVELOPER_MESSAGE = "developer.message"
    ASSISTANT_MESSAGE = "assistant.message"
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    MESSAGE_REPLACED = "message.replaced"
    COMPACTION_DONE = "compaction.done"
    MODEL_SELECTED = "model.selected"
    PRESET_SELECTED = "preset.selected"
    PERMISSION_POLICY = "permission.policy"
    SANDBOX_MODE = "sandbox.mode"
    WORKSPACE_CWD_CHANGED = "workspace.cwd_changed"
    SESSION_TITLE = "session.title"
    GOAL_CHANGED = "goal.changed"
    PLAN_MODE = "plan.mode"
    TODO_WRITTEN = "todo.written"
    DELIVERABLE_PRESENTED = "deliverable.presented"
    RUN_STARTED = "run.started"
    RUN_CHECKPOINT = "run.checkpoint"
    RUN_FINISHED = "run.finished"


CORE_EVENT_TYPES: frozenset[str] = frozenset(kind.value for kind in EventKind)


@dataclass(frozen=True, slots=True)
class SessionHeader:
    id: str
    created_at: int
    cwd: str
    preset: str
    parent_id: str | None
    origin: str
    delegation_depth: int
    type: str = "session"
    schema: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "session",
            "schema": SCHEMA_VERSION,
            "id": self.id,
            "created_at": self.created_at,
            "cwd": self.cwd,
            "preset": self.preset,
            "parent_id": self.parent_id,
            "origin": self.origin,
            "delegation_depth": self.delegation_depth,
        }

    @classmethod
    def from_dict(cls, raw: object, *, expected_id: str | None = None) -> SessionHeader:
        data = _header_object(raw)
        if "version" in data:
            raise SessionV4Error("v4 header")
        if "schema" not in data:
            raise HeaderError("session header lacks required field 'schema'")
        schema = data["schema"]
        if type(schema) is not int or schema != SCHEMA_VERSION:
            raise UnsupportedSchemaError(
                f"unsupported session schema {schema!r}: this build reads schema 1 only"
            )
        for name in sorted(HEADER_FIELDS - set(data)):
            raise HeaderError(f"session header lacks required field {name!r}")
        unknown = set(data) - HEADER_FIELDS
        if unknown:
            raise HeaderError(f"session header has unknown field {sorted(unknown)[0]!r}")
        if data["type"] != "session":
            raise _header_invalid("type", data["type"])
        session_id = data["id"]
        if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
            raise _header_invalid("id", session_id)
        if expected_id is not None and session_id != expected_id:
            raise HeaderError(f"session id {session_id!r} does not match file name {expected_id!r}")
        created_at = data["created_at"]
        if type(created_at) is not int or created_at < 0:
            raise _header_invalid("created_at", created_at)
        cwd = data["cwd"]
        if not isinstance(cwd, str) or not _is_absolute_path(cwd):
            raise _header_invalid("cwd", cwd)
        preset = data["preset"]
        if not isinstance(preset, str) or not PRESET_RE.fullmatch(preset):
            raise _header_invalid("preset", preset)
        parent_id = data["parent_id"]
        if parent_id is not None and (
            not isinstance(parent_id, str) or not SESSION_ID_RE.fullmatch(parent_id)
        ):
            raise _header_invalid("parent_id", parent_id)
        origin = data["origin"]
        if origin not in ORIGINS:
            raise _header_invalid("origin", origin)
        depth = data["delegation_depth"]
        if type(depth) is not int or depth < 0:
            raise _header_invalid("delegation_depth", depth)
        return cls(
            id=session_id,
            created_at=created_at,
            cwd=cwd,
            preset=preset,
            parent_id=parent_id,
            origin=origin,
            delegation_depth=depth,
        )


@dataclass(frozen=True, slots=True)
class Event:
    type: str
    seq: int
    ts: int
    data: dict[str, Any]
    run_id: str | None = None
    turn_id: str | None = None
    step_id: str | None = None
    trace_id: str | None = None

    @property
    def kind(self) -> EventKind | None:
        try:
            return EventKind(self.type)
        except ValueError:
            return None

    @property
    def is_plugin(self) -> bool:
        return self.type.startswith("plugin.")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "seq": self.seq, "ts": self.ts}
        if self.run_id is not None:
            data["run_id"] = self.run_id
        if self.turn_id is not None:
            data["turn_id"] = self.turn_id
        if self.step_id is not None:
            data["step_id"] = self.step_id
        if self.trace_id is not None:
            data["trace_id"] = self.trace_id
        data["data"] = self.data
        return data


@dataclass(frozen=True, slots=True)
class SessionLog:
    path: Path
    header: SessionHeader
    events: tuple[Event, ...]


class NativeCodec:
    """Stateless JSON codec plus the strict reader."""

    @staticmethod
    def dumps(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def encode_header(cls, header: SessionHeader) -> str:
        return cls.dumps(header.to_dict())

    @classmethod
    def encode_event(cls, event: Event) -> str:
        return cls.dumps(event.to_dict())

    @classmethod
    def decode_header(cls, raw: object, *, expected_id: str | None = None) -> SessionHeader:
        return SessionHeader.from_dict(raw, expected_id=expected_id)

    @classmethod
    def decode_event(
        cls,
        raw: object,
        *,
        expected_seq: int,
        previous_ts: int,
        header_created_at: int,
        line_number: int = 0,
    ) -> Event:
        if not isinstance(raw, Mapping):
            raise EventFieldError(f"session line {line_number} is not an event object")
        unknown = set(raw) - ENVELOPE_FIELDS
        if unknown:
            name = sorted(unknown)[0]
            raise EventFieldError(
                f"event {raw.get('type')!r} at seq {raw.get('seq')} has unknown field {name!r}"
            )
        for name in ("type", "seq", "ts", "data"):
            if name not in raw:
                raise EventFieldError(
                    f"event {raw.get('type')!r} at seq {raw.get('seq')} requires field {name!r}"
                )
        event_type = raw["type"]
        seq = raw["seq"]
        ts = raw["ts"]
        if not isinstance(event_type, str) or not event_type:
            raise EventFieldError(f"event type at seq {seq} must be a non-empty string")
        if type(seq) is not int or seq < 1:
            raise EventFieldError(f"event {event_type!r} has invalid seq {seq!r}")
        if type(ts) is not int or ts < 0:
            raise EventFieldError(f"event {event_type!r} at seq {seq} has invalid ts {ts!r}")
        if seq != expected_seq:
            raise SequenceError(f"event seq {seq} is out of order: expected {expected_seq}")
        if ts < header_created_at:
            raise TimestampError(f"event seq {seq} has ts {ts} before the previous ts {header_created_at}")
        if ts < previous_ts:
            raise TimestampError(f"event seq {seq} has ts {ts} before the previous ts {previous_ts}")
        data = raw["data"]
        if not isinstance(data, Mapping):
            raise EventFieldError(
                f"event {event_type!r} at seq {seq} has data of type {type(data).__name__}, expected object"
            )
        _check_event_type(event_type, seq)
        association = _association(event_type, raw, seq)
        validate_event_data(event_type, dict(data), seq=seq)
        return Event(
            type=event_type,
            seq=seq,
            ts=ts,
            data=dict(data),
            run_id=association.get("run_id"),
            turn_id=association.get("turn_id"),
            step_id=association.get("step_id"),
            trace_id=association.get("trace_id"),
        )

    @classmethod
    def read(cls, path: str | Path) -> SessionLog:
        target = Path(path)
        raw_bytes = target.read_bytes()
        if raw_bytes.startswith(ZSTD_MAGIC):
            raise SessionV4Error(
                f"session format v4 is not supported; export it with the standalone migration script "
                f"and start a schema 1 session: {target}"
            )
        if not raw_bytes:
            raise HeaderError("session does not start with a header line")
        if not raw_bytes.endswith(b"\n"):
            line_count = raw_bytes.count(b"\n") + 1
            raise CorruptLineError(f"session line {line_count} is not valid JSON: incomplete last line")
        if raw_bytes.count(b"\n") == 0:
            raise HeaderError("session does not start with a header line")
        raw_lines = raw_bytes.split(b"\n")[:-1]
        lines: list[dict[str, Any]] = []
        for index, raw_line in enumerate(raw_lines, start=1):
            if len(raw_line) > MAX_LINE_BYTES:
                raise LineTooLargeError(f"session line {index} is larger than {MAX_LINE_BYTES} bytes")
            if not raw_line:
                raise CorruptLineError(f"session line {index} is blank")
            if index == 1 and raw_line.startswith(b"\xef\xbb\xbf"):
                raise CorruptLineError("session line 1 is not valid JSON: UTF-8 BOM is not allowed")
            try:
                text = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CorruptLineError(f"session line {index} is not valid JSON: {exc}") from exc
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CorruptLineError(f"session line {index} is not valid JSON: {exc.msg}") from exc
            if not isinstance(parsed, dict):
                raise CorruptLineError(f"session line {index} is not valid JSON: expected an object")
            lines.append(parsed)

        first = lines[0]
        if "version" in first:
            raise SessionV4Error(
                f"session format v4 is not supported; export it with the standalone migration script "
                f"and start a schema 1 session: {target}"
            )
        if first.get("type") != "session":
            raise HeaderError("session does not start with a header line")
        header = SessionHeader.from_dict(first, expected_id=target.stem)
        events: list[Event] = []
        tracker = _OrderTracker()
        previous_ts = header.created_at
        expected_seq = 1
        for index, parsed in enumerate(lines[1:], start=2):
            if parsed.get("type") == "session":
                raise HeaderError("session has more than one header line")
            event = cls.decode_event(
                parsed,
                expected_seq=expected_seq,
                previous_ts=previous_ts,
                header_created_at=header.created_at,
                line_number=index,
            )
            tracker.observe(event)
            events.append(event)
            previous_ts = event.ts
            expected_seq += 1
        return SessionLog(path=target, header=header, events=tuple(events))


class NativeSession:
    """A one-writer append-only schema 1 session file."""

    def __init__(
        self,
        path: Path,
        header: SessionHeader,
        events: Sequence[Event],
        lock_path: Path,
        handle: Any,
    ) -> None:
        self.path = path
        self.header = header
        self._events = list(events)
        self._lock_path = lock_path
        self._handle = handle
        self._closed = False
        self._tracker = _OrderTracker()
        for event in self._events:
            self._tracker.observe(event)
        self._last_seq = self._events[-1].seq if self._events else 0
        self._last_ts = self._events[-1].ts if self._events else header.created_at

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def last_ts(self) -> int:
        return self._last_ts

    @property
    def closed(self) -> bool:
        return self._closed

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        cwd: str | Path,
        preset: str = "standard",
        session_id: str | None = None,
        parent_id: str | None = None,
        origin: str = "user",
        delegation_depth: int = 0,
        created_at: int | None = None,
    ) -> NativeSession:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        header = SessionHeader(
            id=session_id or target.stem,
            created_at=now_ms() if created_at is None else created_at,
            cwd=str(Path(cwd).resolve()),
            preset=preset,
            parent_id=parent_id,
            origin=origin,
            delegation_depth=delegation_depth,
        )
        _validate_header(header, expected_id=target.stem)
        lock_path = _lock_path_for(target)
        _acquire_lock(lock_path, header.id)
        temp = target.parent / f".{target.name}.tmp"
        try:
            encoded = (NativeCodec.encode_header(header) + "\n").encode("utf-8")
            with temp.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
            _fsync_directory(target.parent)
            handle = target.open("ab")
            return cls(target, header, [], lock_path, handle)
        except BaseException:
            with contextlib.suppress(OSError):
                temp.unlink()
            _release_lock(lock_path)
            raise

    @classmethod
    def open(cls, path: str | Path) -> NativeSession:
        target = Path(path)
        log = NativeCodec.read(target)
        lock_path = _lock_path_for(target)
        _acquire_lock(lock_path, log.header.id)
        try:
            handle = target.open("ab")
        except BaseException:
            _release_lock(lock_path)
            raise
        return cls(target, log.header, log.events, lock_path, handle)

    def append(
        self,
        kind: EventKind | str,
        data: Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
        turn_id: str | None = None,
        step_id: str | None = None,
        trace_id: str | None = None,
        ts: int | None = None,
    ) -> Event:
        if self._closed:
            raise ValueError("session is closed")
        event_type = kind.value if isinstance(kind, EventKind) else kind
        seq = self._last_seq + 1
        stamp = max(now_ms(), self._last_ts) if ts is None else ts
        if stamp < self._last_ts:
            raise TimestampError(f"event seq {seq} has ts {stamp} before the previous ts {self._last_ts}")
        event = Event(
            type=event_type,
            seq=seq,
            ts=stamp,
            data=dict(data or {}),
            run_id=run_id,
            turn_id=turn_id,
            step_id=step_id,
            trace_id=trace_id,
        )
        _check_event_type(event_type, seq)
        association = _association(event_type, event.to_dict(), seq)
        if association.get("run_id") != run_id:
            raise EventFieldError(f"event {event_type!r} at seq {seq} requires field 'run_id'")
        validate_event_data(event_type, event.data, seq=seq)
        trial = self._tracker.clone()
        trial.observe(event)
        line = (NativeCodec.encode_event(event) + "\n").encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            raise LineTooLargeError(f"session line {seq + 1} is larger than {MAX_LINE_BYTES} bytes")
        offset = self._handle.tell()
        try:
            self._handle.write(line)
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                self._handle.truncate(offset)
                self._handle.seek(offset)
            raise
        self._events.append(event)
        self._tracker = trial
        self._last_seq = seq
        self._last_ts = stamp
        return event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            self._handle.close()
        _release_lock(self._lock_path)

    def __enter__(self) -> NativeSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class _OrderTracker:
    """Incremental turn/step nesting check shared by reader and writer."""

    def __init__(self) -> None:
        self.open_turn: str | None = None
        self.open_step: str | None = None

    def clone(self) -> _OrderTracker:
        clone = _OrderTracker()
        clone.open_turn = self.open_turn
        clone.open_step = self.open_step
        return clone

    def observe(self, event: Event) -> None:
        kind = event.type
        if kind == "turn.started":
            if self.open_turn is not None:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed inside turn {self.open_turn!r}: "
                    "a turn is already open"
                )
            self.open_turn = event.turn_id
            return
        if kind == "turn.ended":
            if self.open_turn is None:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed outside a turn: no turn is open"
                )
            if event.turn_id != self.open_turn:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed inside turn {self.open_turn!r}: "
                    f"turn_id {event.turn_id!r} does not match"
                )
            if self.open_step is not None:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed inside step {self.open_step!r}: "
                    "the step is still open"
                )
            self.open_turn = None
            return
        if kind == "step.started":
            if self.open_turn is None or event.turn_id != self.open_turn:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed outside turn {event.turn_id!r}"
                )
            if self.open_step is not None:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed inside step {self.open_step!r}: "
                    "a step is already open"
                )
            self.open_step = event.step_id
            return
        if kind == "step.ended":
            if self.open_step is None or event.step_id != self.open_step:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed without a matching open step"
                )
            self.open_step = None
            return
        if event.turn_id is not None:
            if self.open_turn is None:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed outside a turn: no turn is open"
                )
            if event.turn_id != self.open_turn:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed inside turn {self.open_turn!r}: "
                    f"turn_id {event.turn_id!r} does not match"
                )
        if event.step_id is not None:
            if self.open_step is None:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed outside a step: no step is open"
                )
            if event.step_id != self.open_step:
                raise OrderingError(
                    f"event {kind!r} at seq {event.seq} is not allowed inside step {self.open_step!r}: "
                    f"step_id {event.step_id!r} does not match"
                )


def validate_event_data(event_type: str, data: dict[str, Any], *, seq: int) -> None:
    """Validate the closed data shape for one event type."""
    ctx = f"event {event_type!r} at seq {seq}"
    if PLUGIN_EVENT_RE.fullmatch(event_type):
        return
    if event_type in ("turn.started", "step.started", "step.ended"):
        _object(data, set(), set(), ctx)
        return
    if event_type == "turn.ended":
        _object(data, {"reason"}, {"reason"}, ctx)
        _validate_turn_reason(data["reason"], ctx)
        return
    if event_type in ("user.message", "system.message", "developer.message"):
        _object(data, {"message"}, {"message"}, ctx)
        message = _message(data["message"], ctx)
        expected_role = event_type.split(".", 1)[0]
        if message.role != expected_role:
            raise MessageError(
                f"{ctx} message is invalid: role {message.role!r} does not match {expected_role!r}"
            )
        return
    if event_type == "assistant.message":
        _object(data, {"message", "finish_reason"}, {"message", "finish_reason"}, ctx)
        message = _message(data["message"], ctx)
        if message.role != "assistant":
            raise MessageError(f"{ctx} message is invalid: role {message.role!r} must be 'assistant'")
        _enum(data["finish_reason"], FINISH_REASONS, "finish_reason", ctx)
        return
    if event_type == "tool.call":
        _object(data, {"call_id", "name", "arguments"}, {"call_id", "name", "arguments"}, ctx)
        _string(data["call_id"], "call_id", ctx)
        _string(data["name"], "name", ctx)
        _string(data["arguments"], "arguments", ctx, allow_empty=True)
        return
    if event_type == "tool.result":
        _object(data, {"call_id", "message", "error_code"}, {"call_id", "message"}, ctx)
        call_id = _string(data["call_id"], "call_id", ctx)
        message = _message(data["message"], ctx)
        if message.role != "tool" or message.tool_call_id != call_id:
            raise MessageError(f"{ctx} message is invalid: tool message must reference call_id {call_id!r}")
        if "error_code" in data:
            _string(data["error_code"], "error_code", ctx)
        return
    if event_type == "approval.requested":
        _object(
            data,
            {"approval_id", "tool_call_id", "tool_name", "reason"},
            {"approval_id", "tool_call_id", "tool_name"},
            ctx,
        )
        for name in ("approval_id", "tool_call_id", "tool_name"):
            _string(data[name], name, ctx)
        if "reason" in data:
            _string(data["reason"], "reason", ctx, allow_empty=True)
        return
    if event_type == "approval.decided":
        _object(data, {"approval_id", "outcome"}, {"approval_id", "outcome"}, ctx)
        _string(data["approval_id"], "approval_id", ctx)
        _enum(data["outcome"], APPROVAL_OUTCOMES, "outcome", ctx)
        return
    if event_type == "message.replaced":
        _object(data, {"message_id", "message"}, {"message_id", "message"}, ctx)
        message_id = _string(data["message_id"], "message_id", ctx)
        message = _message(data["message"], ctx)
        if message.id != message_id:
            raise MessageError(f"{ctx} message is invalid: message id does not match message_id")
        return
    if event_type == "compaction.done":
        _object(data, {"compaction_id", "covers", "message"}, {"compaction_id", "covers", "message"}, ctx)
        compaction_id = _string(data["compaction_id"], "compaction_id", ctx)
        covers = data["covers"]
        if not isinstance(covers, list) or not covers:
            raise EventFieldError(f"{ctx} field 'covers' must be a non-empty array")
        for item in covers:
            if not isinstance(item, str) or not item:
                raise EventFieldError(f"{ctx} field 'covers' must contain message ids")
        message = _message(data["message"], ctx)
        if message.role != "user":
            raise MessageError(f"{ctx} message is invalid: compaction message role must be 'user'")
        if not isinstance(message.source, CompactionSource) or message.source.compaction_id != compaction_id:
            raise MessageError(
                f"{ctx} message is invalid: compaction source must reference {compaction_id!r}"
            )
        return
    if event_type == "model.selected":
        _object(data, {"provider", "model"}, {"provider", "model"}, ctx)
        _string(data["provider"], "provider", ctx)
        _string(data["model"], "model", ctx)
        return
    if event_type == "preset.selected":
        _object(data, {"preset", "mode", "revision", "previous"}, {"preset", "mode"}, ctx)
        preset = _string(data["preset"], "preset", ctx)
        if not PRESET_RE.fullmatch(preset):
            raise EventFieldError(f"{ctx} field 'preset' has invalid value {preset!r}")
        _enum(data["mode"], PRESET_MODES, "mode", ctx)
        if "revision" in data:
            _integer(data["revision"], "revision", ctx, minimum=0)
        if "previous" in data:
            previous = _string(data["previous"], "previous", ctx)
            if not PRESET_RE.fullmatch(previous):
                raise EventFieldError(f"{ctx} field 'previous' has invalid value {previous!r}")
        return
    if event_type == "permission.policy":
        _object(data, {"policy"}, {"policy"}, ctx)
        _enum(data["policy"], PERMISSION_POLICIES, "policy", ctx)
        return
    if event_type == "sandbox.mode":
        _object(data, {"mode"}, {"mode"}, ctx)
        _enum(data["mode"], SANDBOX_MODES, "mode", ctx)
        return
    if event_type == "workspace.cwd_changed":
        _object(data, {"cwd"}, {"cwd"}, ctx)
        cwd = _string(data["cwd"], "cwd", ctx)
        if not _is_absolute_path(cwd):
            raise EventFieldError(f"{ctx} field 'cwd' has invalid value {cwd!r}")
        return
    if event_type == "session.title":
        _object(data, {"title", "source"}, {"title", "source"}, ctx)
        _string(data["title"], "title", ctx, allow_empty=True)
        _enum(data["source"], TITLE_SOURCES, "source", ctx)
        return
    if event_type == "goal.changed":
        _object(data, {"goal", "rounds_started"}, {"goal", "rounds_started"}, ctx)
        if data["goal"] is not None:
            _validate_goal(data["goal"], ctx)
        _integer(data["rounds_started"], "rounds_started", ctx, minimum=0)
        return
    if event_type == "plan.mode":
        _object(data, {"mode"}, {"mode"}, ctx)
        _string(data["mode"], "mode", ctx)
        return
    if event_type == "todo.written":
        _object(data, {"todos"}, {"todos"}, ctx)
        todos = data["todos"]
        if not isinstance(todos, list):
            raise EventFieldError(f"{ctx} field 'todos' must be an array")
        for item in todos:
            todo = _object(item, {"content", "status"}, {"content", "status"}, f"{ctx} todo")
            _string(todo["content"], "content", f"{ctx} todo", allow_empty=True)
            _enum(todo["status"], TODO_STATUSES, "status", f"{ctx} todo")
        return
    if event_type == "deliverable.presented":
        _object(data, {"files"}, {"files"}, ctx)
        files = data["files"]
        if not isinstance(files, list):
            raise EventFieldError(f"{ctx} field 'files' must be an array")
        for item in files:
            entry = _object(item, {"path", "description"}, {"path"}, f"{ctx} file")
            _string(entry["path"], "path", f"{ctx} file")
            if "description" in entry:
                _string(entry["description"], "description", f"{ctx} file", allow_empty=True)
        return
    if event_type == "run.started":
        _object(data, set(), set(), ctx)
        return
    if event_type == "run.checkpoint":
        _validate_run_checkpoint(data, ctx)
        return
    if event_type == "run.finished":
        _object(data, {"status", "reason"}, {"status"}, ctx)
        _enum(data["status"], RUN_FINISH_STATUSES, "status", ctx)
        if "reason" in data:
            _validate_turn_reason(data["reason"], ctx)
        return
    raise UnknownEventError(
        f"unknown event type {event_type!r} at seq {seq}: plugin events must use the plugin.<id>.<event> namespace"
    )


def _validate_turn_reason(raw: object, ctx: str) -> None:
    reason = _object(raw, {"kind", "by", "message", "code", "limit"}, {"kind"}, f"{ctx} reason")
    kind = _string(reason["kind"], "kind", f"{ctx} reason")
    expected: dict[str, set[str]] = {
        "completed": set(),
        "aborted": {"by"},
        "error": {"message"},
        "max_steps": {"limit"},
        "max_tokens": set(),
        "blocked": {"message"},
    }
    if kind not in expected:
        raise EventFieldError(f"{ctx} field 'reason' has invalid value {raw!r}")
    allowed = {"kind"} | expected[kind]
    if kind == "aborted":
        allowed.add("message")
    elif kind == "error":
        allowed.add("code")
    elif kind == "max_tokens":
        allowed.add("limit")
    unknown = set(reason) - allowed
    if unknown:
        raise EventFieldError(f"{ctx} reason has unknown field {sorted(unknown)[0]!r}")
    for name in expected[kind]:
        if name not in reason:
            raise EventFieldError(f"{ctx} reason requires field {name!r}")
    if kind == "aborted":
        _enum(reason["by"], ABORTED_BY, "by", f"{ctx} reason")
        if "message" in reason:
            _string(reason["message"], "message", f"{ctx} reason", allow_empty=True)
    elif kind == "error":
        _string(reason["message"], "message", f"{ctx} reason", allow_empty=True)
        if "code" in reason:
            _string(reason["code"], "code", f"{ctx} reason")
    elif kind in ("max_steps", "max_tokens") and (kind == "max_steps" or "limit" in reason):
        _integer(reason["limit"], "limit", f"{ctx} reason", minimum=1)
    elif kind == "blocked":
        _string(reason["message"], "message", f"{ctx} reason", allow_empty=True)


def _validate_goal(raw: object, ctx: str) -> None:
    goal = _object(
        raw,
        {"id", "revision", "objective", "phase", "max_goal_rounds", "blocked_reason"},
        {"id", "revision", "objective", "phase", "max_goal_rounds"},
        f"{ctx} goal",
    )
    _string(goal["id"], "id", f"{ctx} goal")
    _integer(goal["revision"], "revision", f"{ctx} goal", minimum=1)
    _string(goal["objective"], "objective", f"{ctx} goal", allow_empty=True)
    phase = _enum(goal["phase"], GOAL_PHASES, "phase", f"{ctx} goal")
    _integer(goal["max_goal_rounds"], "max_goal_rounds", f"{ctx} goal", minimum=1)
    if "blocked_reason" in goal:
        if phase != "blocked":
            raise EventFieldError(
                f"{ctx} goal has invalid value {goal['blocked_reason']!r}: phase is not 'blocked'"
            )
        blocked = _object(
            goal["blocked_reason"], {"code", "message"}, {"code", "message"}, f"{ctx} goal blocked_reason"
        )
        _string(blocked["code"], "code", f"{ctx} goal blocked_reason")
        _string(blocked["message"], "message", f"{ctx} goal blocked_reason", allow_empty=True)


def _validate_run_checkpoint(raw: object, ctx: str) -> None:
    checkpoint = _object(
        raw,
        {
            "last_seq",
            "last_message_id",
            "turn_id",
            "step_id",
            "status",
            "budgets",
            "tool_calls",
            "approvals",
        },
        {"last_seq", "status", "budgets", "tool_calls", "approvals"},
        ctx,
    )
    _integer(checkpoint["last_seq"], "last_seq", ctx)
    if "last_message_id" in checkpoint:
        _message_id(checkpoint["last_message_id"], "last_message_id", ctx)
    if "turn_id" in checkpoint:
        _identifier(checkpoint["turn_id"], "turn_id", ctx, TURN_ID_RE)
    if "step_id" in checkpoint:
        _identifier(checkpoint["step_id"], "step_id", ctx, STEP_ID_RE)
    _enum(checkpoint["status"], CHECKPOINT_STATUSES, "status", ctx)
    budget_ctx = f"{ctx} budgets"
    budgets = _object(
        checkpoint["budgets"],
        {"input_tokens", "output_tokens", "cost_usd", "tool_calls", "wall_ms"},
        {"input_tokens", "output_tokens", "cost_usd", "tool_calls", "wall_ms"},
        budget_ctx,
    )
    for name in ("input_tokens", "output_tokens", "tool_calls", "wall_ms"):
        _integer(budgets[name], name, budget_ctx)
    _number(budgets["cost_usd"], "cost_usd", budget_ctx)
    tool_calls = checkpoint["tool_calls"]
    if not isinstance(tool_calls, list):
        raise EventFieldError(f"{ctx} field 'tool_calls' must be an array")
    for item in tool_calls:
        tool_ctx = f"{ctx} tool_call"
        record = _object(
            item, {"call_id", "name", "state", "idempotency_key"}, {"call_id", "name", "state"}, tool_ctx
        )
        _string(record["call_id"], "call_id", tool_ctx)
        _string(record["name"], "name", tool_ctx)
        _enum(record["state"], TOOL_CALL_STATES, "state", tool_ctx)
        if "idempotency_key" in record:
            _identifier(record["idempotency_key"], "idempotency_key", tool_ctx, IDEMPOTENCY_KEY_RE)
    approvals = checkpoint["approvals"]
    if not isinstance(approvals, list):
        raise EventFieldError(f"{ctx} field 'approvals' must be an array")
    for item in approvals:
        approval_ctx = f"{ctx} approval"
        decision = _object(
            item,
            {"approval_id", "tool_call_id", "state"},
            {"approval_id", "tool_call_id", "state"},
            approval_ctx,
        )
        _string(decision["approval_id"], "approval_id", approval_ctx)
        _string(decision["tool_call_id"], "tool_call_id", approval_ctx)
        _string(decision["state"], "state", approval_ctx)


def _association(event_type: str, raw: Mapping[str, Any], seq: int) -> dict[str, str | None]:
    ctx = f"event {event_type!r} at seq {seq}"
    fields = ("run_id", "turn_id", "step_id", "trace_id")
    values: dict[str, str | None] = {}
    for name in fields:
        if name not in raw:
            values[name] = None
            continue
        value = raw[name]
        if value is None:
            raise EventFieldError(f"{ctx} has invalid value None for field {name!r}: null is omitted")
        if not isinstance(value, str):
            raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
        pattern = {
            "run_id": RUN_ID_RE,
            "turn_id": TURN_ID_RE,
            "step_id": STEP_ID_RE,
            "trace_id": TRACE_ID_RE,
        }[name]
        if not pattern.fullmatch(value):
            raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
        values[name] = value

    if event_type.startswith("turn."):
        required = ("run_id", "turn_id", "trace_id")
        forbidden = ("step_id",)
    elif event_type.startswith("step.") or event_type in (
        "assistant.message",
        "tool.call",
        "tool.result",
        "approval.requested",
        "approval.decided",
    ):
        required = ("run_id", "turn_id", "step_id", "trace_id")
        forbidden = ()
    elif event_type in ("user.message", "system.message", "developer.message"):
        required = ()
        forbidden = ("step_id",)
    elif event_type.startswith("run."):
        required = ("run_id", "trace_id")
        forbidden = ("turn_id", "step_id")
    else:
        required = ()
        forbidden = ()
    for name in required:
        if values[name] is None:
            raise EventFieldError(f"{ctx} requires field {name!r}")
    for name in forbidden:
        if values[name] is not None:
            raise EventFieldError(f"{ctx} does not allow field {name!r}")
    return values


def _check_event_type(event_type: str, seq: int) -> None:
    if event_type in CORE_EVENT_TYPES:
        return
    if event_type.startswith("plugin."):
        if not PLUGIN_EVENT_RE.fullmatch(event_type):
            raise PluginNamespaceError(
                f"invalid plugin event type {event_type!r}: expected plugin.<id>.<event>"
            )
        return
    raise UnknownEventError(
        f"unknown event type {event_type!r} at seq {seq}: plugin events must use the plugin.<id>.<event> namespace"
    )


def _message(raw: object, ctx: str) -> Message:
    if not isinstance(raw, Mapping):
        raise MessageError(f"{ctx} message is invalid: message must be an object")
    try:
        return Message.from_dict(raw)
    except MessageValidationError as exc:
        raise MessageError(f"{ctx} message is invalid: {exc}") from exc


def _object(raw: object, allowed: set[str], required: set[str], ctx: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise EventFieldError(f"{ctx} has data of type {type(raw).__name__}, expected object")
    data = dict(raw)
    unknown = set(data) - allowed
    if unknown:
        raise EventFieldError(f"{ctx} has unknown field {sorted(unknown)[0]!r}")
    for name in sorted(required - set(data)):
        raise EventFieldError(f"{ctx} requires field {name!r}")
    return data


def _string(value: object, name: str, ctx: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
    return value


def _integer(value: object, name: str, ctx: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
    return value


def _number(value: object, name: str, ctx: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
    return number


def _identifier(value: object, name: str, ctx: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
    return value


def _message_id(value: object, name: str, ctx: str) -> str:
    if not isinstance(value, str) or not (
        MESSAGE_ID_RE.fullmatch(value) or MISSING_MESSAGE_ID_RE.fullmatch(value)
    ):
        raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
    return value


def _enum(value: object, allowed: frozenset[str], name: str, ctx: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise EventFieldError(f"{ctx} has invalid value {value!r} for field {name!r}")
    return value


def _header_object(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise HeaderError(f"session header field 'type' has invalid value {raw!r}")
    if "version" in raw:
        raise SessionV4Error("v4 header")
    if "schema" in raw and (type(raw["schema"]) is not int or raw["schema"] != SCHEMA_VERSION):
        raise UnsupportedSchemaError(
            f"unsupported session schema {raw['schema']!r}: this build reads schema 1 only"
        )
    data = dict(raw)
    unknown = set(data) - HEADER_FIELDS
    if unknown:
        raise HeaderError(f"session header has unknown field {sorted(unknown)[0]!r}")
    for name in sorted(HEADER_FIELDS - set(data)):
        raise HeaderError(f"session header lacks required field {name!r}")
    return data


def _header_invalid(name: str, value: object) -> HeaderError:
    return HeaderError(f"session header field {name!r} has invalid value {value!r}")


def _validate_header(header: SessionHeader, *, expected_id: str | None) -> None:
    SessionHeader.from_dict(header.to_dict(), expected_id=expected_id)


def _is_absolute_path(value: str) -> bool:
    try:
        return Path(value).is_absolute()
    except (OSError, ValueError):
        return False


def _lock_path_for(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _acquire_lock(lock_path: Path, session_id: str) -> None:
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pid = _lock_pid(lock_path)
        raise SessionLockedError(f"session {session_id!r} is locked by pid {pid}") from None
    try:
        os.write(descriptor, f"{os.getpid()} {now_ms()}".encode("ascii"))
    finally:
        os.close(descriptor)


def _lock_pid(lock_path: Path) -> str:
    try:
        return lock_path.read_text(encoding="ascii").split(" ", 1)[0]
    except (OSError, UnicodeDecodeError):
        return "unknown"


def _release_lock(lock_path: Path) -> None:
    with contextlib.suppress(OSError):
        lock_path.unlink()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sessions_path_for(session_id: str, directory: Path | None = None) -> Path:
    """Resolve the flat schema 1 path for a session id."""
    base = directory if directory is not None else _default_sessions_dir()
    return base / f"{session_id}.jsonl"


def _default_sessions_dir() -> Path:
    from deeppyro.paths import sessions_dir

    return sessions_dir()


def iter_session_paths(directory: Path | None = None) -> Iterator[Path]:
    """Yield schema 1 .jsonl files, newest first, ignoring locks and temp files."""
    base = directory if directory is not None else _default_sessions_dir()
    if not base.is_dir():
        return
    paths = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    yield from paths


__all__ = [
    "APPROVAL_OUTCOMES",
    "CORE_EVENT_TYPES",
    "FINISH_REASONS",
    "HEADER_FIELDS",
    "MAX_LINE_BYTES",
    "ORIGINS",
    "PLUGIN_EVENT_PATTERN",
    "SCHEMA_VERSION",
    "SESSION_ID_PATTERN",
    "Event",
    "EventKind",
    "NativeCodec",
    "NativeSession",
    "SessionHeader",
    "SessionLog",
    "iter_session_paths",
    "sessions_path_for",
    "validate_event_data",
]
