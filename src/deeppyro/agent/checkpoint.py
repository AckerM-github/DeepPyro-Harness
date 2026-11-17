"""Atomic run checkpoints for crash resume (M2 task 3).

The checkpoint file is the sidecar to the append-only session log: the log is
the source of truth for messages and events, the checkpoint records the last
completed step plus cumulative budgets so a new process can resume without
replaying side-effectful tool calls.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from deeppyro.agent.run_state import RunState, ToolCallCheckpoint
from deeppyro.core.ids import now_ms
from deeppyro.paths import deeppyro_home
from deeppyro.session.errors import SessionError
from deeppyro.session.schema import validate_event_data

CHECKPOINT_VERSION = 1
CHECKPOINTS_ENV = "DEEPPYRO_CHECKPOINTS_DIR"

_RECORD_FIELDS = frozenset(
    {"version", "session_id", "run_id", "trace_id", "written_at", "state", "checkpoint"}
)


class CheckpointError(RuntimeError):
    """A checkpoint file is missing, corrupt or inconsistent with its session."""


def _validate_payload(data: Mapping[str, object]) -> None:
    """Validate one run.checkpoint payload against the frozen M2 schema."""
    last_seq = data.get("last_seq")
    seq = last_seq if type(last_seq) is int else 0
    try:
        validate_event_data("run.checkpoint", dict(data), seq=seq)
    except SessionError as exc:
        raise CheckpointError(f"checkpoint payload is invalid: {exc}") from exc


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """One parsed checkpoint file."""

    session_id: str
    run_id: str
    trace_id: str
    state: RunState
    data: Mapping[str, object]
    written_at: int = 0

    def to_dict(self) -> dict[str, object]:
        """Serialize the record for the on-disk JSON envelope."""
        return {
            "version": CHECKPOINT_VERSION,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "written_at": self.written_at,
            "state": self.state.to_dict(),
            "checkpoint": dict(self.data),
        }

    @classmethod
    def from_dict(cls, raw: object) -> CheckpointRecord:
        """Parse and validate a checkpoint envelope."""
        if not isinstance(raw, Mapping):
            raise CheckpointError("checkpoint must be a JSON object")
        unknown = set(raw) - _RECORD_FIELDS
        if unknown:
            raise CheckpointError(f"checkpoint has an unknown field {sorted(unknown)[0]!r}")
        missing = _RECORD_FIELDS - set(raw)
        if missing:
            raise CheckpointError(f"checkpoint is missing field {sorted(missing)[0]!r}")
        if raw["version"] != CHECKPOINT_VERSION:
            raise CheckpointError(f"checkpoint has unsupported version {raw['version']!r}")
        for name in ("session_id", "run_id", "trace_id"):
            value = raw[name]
            if not isinstance(value, str) or not value:
                raise CheckpointError(f"checkpoint field {name!r} must be a non-empty string")
        written_at = raw["written_at"]
        if not isinstance(written_at, int) or isinstance(written_at, bool) or written_at < 0:
            raise CheckpointError("checkpoint field 'written_at' must be a non-negative integer")
        try:
            state = RunState.from_dict(raw["state"])
        except ValueError as exc:
            raise CheckpointError(f"checkpoint state is invalid: {exc}") from exc
        checkpoint = raw["checkpoint"]
        if not isinstance(checkpoint, Mapping):
            raise CheckpointError("checkpoint field 'checkpoint' must be an object")
        data = dict(checkpoint)
        _validate_payload(data)
        if state.session_id != raw["session_id"]:
            raise CheckpointError("checkpoint state and envelope disagree on session_id")
        if state.run_id != raw["run_id"]:
            raise CheckpointError("checkpoint state and envelope disagree on run_id")
        if state.trace_id != raw["trace_id"]:
            raise CheckpointError("checkpoint state and envelope disagree on trace_id")
        if data["last_seq"] != state.last_seq:
            raise CheckpointError("checkpoint payload and state disagree on last_seq")
        for name in ("turn_id", "step_id", "last_message_id"):
            if name in data and data[name] != getattr(state, name):
                raise CheckpointError(f"checkpoint payload and state disagree on {name}")
        return cls(
            session_id=raw["session_id"],  # type: ignore[arg-type]
            run_id=raw["run_id"],  # type: ignore[arg-type]
            trace_id=raw["trace_id"],  # type: ignore[arg-type]
            state=state,
            data=data,
            written_at=written_at,
        )


def _safe_filename(session_id: object) -> str:
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if (
        session_id in {".", ".."}
        or Path(session_id).name != session_id
        or "/" in session_id
        or "\\" in session_id
    ):
        raise ValueError("session_id must not contain path separators")
    return session_id


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Write JSON atomically: sibling temp file, fsync, os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return


class CheckpointStore:
    """Read and atomically replace one checkpoint file per session."""

    def __init__(self, root: str | Path | None = None) -> None:
        if root is None:
            configured = os.environ.get(CHECKPOINTS_ENV)
            root = Path(configured) if configured else deeppyro_home() / "checkpoints"
        self.root = Path(root)

    def path_for(self, session_id: str) -> Path:
        """Return the checkpoint path for one session."""
        return self.root / f"{_safe_filename(session_id)}.json"

    def write(self, record: CheckpointRecord) -> Path:
        """Atomically replace the session's checkpoint file."""
        path = self.path_for(record.session_id)
        _atomic_write_json(path, record.to_dict())
        return path

    def read(self, session_id: str) -> CheckpointRecord | None:
        """Read the session's checkpoint, or return None when absent."""
        path = self.path_for(session_id)
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"checkpoint file is unreadable: {exc}") from exc
        record = CheckpointRecord.from_dict(raw)
        if record.session_id != session_id:
            raise CheckpointError("checkpoint file belongs to a different session")
        return record

    def delete(self, session_id: str) -> bool:
        """Delete the session's checkpoint file if present; return whether it existed."""
        path = self.path_for(session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def make_record(
        self,
        state: RunState,
        *,
        tool_calls: Sequence[ToolCallCheckpoint] = (),
        approvals: Sequence[Mapping[str, object]] = (),
        written_at: int | None = None,
    ) -> CheckpointRecord:
        """Build a record from run state, validating the checkpoint payload."""
        data = state.checkpoint_data(tool_calls=tool_calls, approvals=approvals)
        _validate_payload(data)
        return CheckpointRecord(
            session_id=state.session_id,
            run_id=state.run_id,
            trace_id=state.trace_id,
            state=state,
            data=data,
            written_at=now_ms() if written_at is None else written_at,
        )


__all__ = [
    "CHECKPOINTS_ENV",
    "CHECKPOINT_VERSION",
    "CheckpointError",
    "CheckpointRecord",
    "CheckpointStore",
]
