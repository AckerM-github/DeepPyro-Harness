"""Durable writer for session v4 JSONL and zstd-framed JSONL logs.

Every append is one compact JSON line (UTF-8, no ASCII escaping) terminated by a
newline, flushed, and fsynced.  New files are materialized through a temporary
file plus an atomic replace so a partially written header never becomes
visible.  zstd files use one independent, checksummed frame per durable batch.
"""

from __future__ import annotations

import json
import os
import pathlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import zstandard

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord, ReplaceOp, SessionHeader
from deeppyro.session.events import encode_source_event_seqs, header_line_dict


def _compressor() -> zstandard.ZstdCompressor:
    return zstandard.ZstdCompressor(write_checksum=True)


def _dumps(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_header_line(header: SessionHeader | Mapping[str, Any]) -> bytes:
    """Serialize the first log line for *header*."""
    raw = header.model_dump(exclude_none=True) if isinstance(header, SessionHeader) else dict(header)
    return _dumps(header_line_dict(raw)) + b"\n"


def event_to_dict(record: LogRecord) -> dict[str, Any]:
    """Convert a LogRecord back to its canonical persisted envelope dict."""
    event: dict[str, Any] = {
        "type": record.type,
        "seq": record.seq,
        "time": record.time,
        "data": record.data,
    }
    surface_op = record.surfaceOp
    if surface_op is not None:
        event["surfaceOp"] = (
            {"op": "replace", "startSeq": surface_op.startSeq, "endSeq": surface_op.endSeq}
            if isinstance(surface_op, ReplaceOp)
            else surface_op
        )
    if record.sourceEventSeqs is not None:
        event["sourceEventSeqs"] = encode_source_event_seqs(record.sourceEventSeqs)
    if record.ignorable is True:
        event["ignorable"] = True
    return event


def encode_event_line(event: Mapping[str, Any]) -> bytes:
    """Serialize one event envelope as a compact single-line JSON record."""
    payload = dict(event)
    sources = payload.get("sourceEventSeqs")
    if isinstance(sources, list) and sources and all(isinstance(seq, int) for seq in sources):
        payload["sourceEventSeqs"] = encode_source_event_seqs(sources)
    surface_op = payload.get("surfaceOp")
    if isinstance(surface_op, ReplaceOp):
        payload["surfaceOp"] = {
            "op": "replace",
            "startSeq": surface_op.startSeq,
            "endSeq": surface_op.endSeq,
        }
    return _dumps(payload) + b"\n"


def _encode_events_block(events: Sequence[Mapping[str, Any]], *, zstd: bool) -> bytes:
    if not events:
        return b""
    payload = b"".join(encode_event_line(event) for event in events)
    if zstd:
        return _compressor().compress(payload)
    return payload


def write_new_session(
    path: pathlib.Path,
    header: SessionHeader | Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    zstd: bool = False,
) -> None:
    """Materialize a new log file atomically with *header* and *events*."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header_bytes = encode_header_line(header)
    if zstd:
        header_block = _compressor().compress(header_bytes)
    else:
        header_block = header_bytes
    events_block = _encode_events_block(events, zstd=zstd)
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("wb") as handle:
            handle.write(header_block)
            if events_block:
                handle.write(events_block)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise SessionError(f"cannot write session log {path}: {exc}") from exc


def append_events(
    path: pathlib.Path,
    events: Sequence[Mapping[str, Any]],
    *,
    zstd: bool = False,
) -> None:
    """Append *events* as one durable batch, rolling back a failed write."""
    if not events:
        return
    path = pathlib.Path(path)
    payload = _encode_events_block(events, zstd=zstd)
    try:
        original_size = path.stat().st_size
    except OSError as exc:
        raise SessionError(f"cannot append to session log {path}: {exc}") from exc
    try:
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            with path.open("r+b") as handle:
                handle.truncate(original_size)
        except OSError:  # pragma: no cover - best-effort rollback
            pass
        raise SessionError(f"cannot append to session log {path}: {exc}") from exc
