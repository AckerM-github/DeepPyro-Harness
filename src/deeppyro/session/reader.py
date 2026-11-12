"""Reader for session v4 JSONL and zstd-framed JSONL logs.

The header is line 0; every following line is one compact JSON event.  Plain
.jsonl files contain one line per event.  .jsonl.zstd files contain a sequence
of independent zstd frames (header, first batch, then one frame per append) and
are decoded frame by frame.  A torn final frame or a final line without a
newline is treated as an interrupted write and dropped; a gap in seqs means the
file is a trimmed fixture and is reported as such rather than rejected.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

import zstandard

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord, SessionHeader
from deeppyro.session.events import normalize_event, validate_header

_ZSTD_MAGIC = 0xFD2FB528


@dataclass(slots=True)
class SessionRead:
    """Result of reading one session log file."""

    header: SessionHeader
    records: list[LogRecord] = field(default_factory=list)
    truncated: bool = False
    trimmed: bool = False


def decompress_zstd_frames(data: bytes) -> bytes:
    """Decode concatenated zstd frames, stopping at the first torn frame.

    Every frame is independent (DSH writes one frame per durable append), so a
    crash that truncated the last frame still leaves all earlier frames readable.
    """
    if not data:
        return b""
    decompressor = zstandard.ZstdDecompressor()
    output = bytearray()
    rest = data
    while rest:
        obj = decompressor.decompressobj()
        try:
            chunk = obj.decompress(rest)
        except zstandard.ZstdError:
            break
        output.extend(chunk)
        if not obj.eof:
            break
        rest = obj.unused_data
    return bytes(output)


def read_session_file(path: pathlib.Path) -> SessionRead:
    """Read and validate one session log file."""
    path = pathlib.Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SessionError(f"cannot read session log {path}: {exc}") from exc
    if path.name.endswith(".zstd"):
        data = decompress_zstd_frames(raw)
    else:
        data = raw
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    lines, torn = _split_lines(data)
    if not lines:
        raise SessionError(f"session log {path} has no header line")
    header = _parse_header(lines[0], path)
    records: list[LogRecord] = []
    expected = 0
    trimmed = False
    by_seq: dict[int, LogRecord] = {}
    for index, line in enumerate(lines[1:], start=2):
        record = _parse_event(line, path, index)
        if record.seq == expected:
            expected += 1
        elif record.seq > expected:
            trimmed = True
            expected = record.seq + 1
        else:
            raise SessionError(f"{path}:{index}: session event seq {record.seq} is not increasing")
        if record.seq in by_seq:
            raise SessionError(f"{path}:{index}: duplicate session event seq {record.seq}")
        by_seq[record.seq] = record
        records.append(record)
    return SessionRead(header=header, records=records, truncated=torn, trimmed=trimmed)


def read_header(path: pathlib.Path) -> SessionHeader:
    """Read only the header line of a session log (cheap for store discovery)."""
    path = pathlib.Path(path)
    try:
        if path.name.endswith(".zstd"):
            raw = path.read_bytes()
            if not raw:
                raise SessionError(f"session log {path} is empty")
            decompressor = zstandard.ZstdDecompressor()
            obj = decompressor.decompressobj()
            try:
                data = obj.decompress(raw)
            except zstandard.ZstdError as exc:
                raise SessionError(f"session log {path} has an unreadable zstd header frame") from exc
        else:
            with path.open("rb") as handle:
                data = handle.readline()
    except OSError as exc:
        raise SessionError(f"cannot read session log {path}: {exc}") from exc
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    line = data.split(b"\n", 1)[0]
    if not line.strip():
        raise SessionError(f"session log {path} has no header line")
    return _parse_header(line, path)


def _split_lines(data: bytes) -> tuple[list[bytes], bool]:
    """Split on newlines, dropping an unterminated tail line as a torn write."""
    truncated = False
    if data and not data.endswith(b"\n"):
        truncated = True
        boundary = data.rfind(b"\n")
        data = data[: boundary + 1] if boundary >= 0 else b""
    lines = [line for line in data.split(b"\n") if line.strip()]
    return lines, truncated


def _parse_header(line: bytes, path: pathlib.Path) -> SessionHeader:
    try:
        raw = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionError(f"session log {path}: invalid header JSON: {exc}") from exc
    try:
        validate_header(raw)
        if raw.get("cwd") is None:
            raw = {**raw, "cwd": ""}
        return SessionHeader.model_validate(raw)
    except SessionError:
        raise
    except ValueError as exc:
        raise SessionError(f"session log {path}: invalid header: {exc}") from exc


def _parse_event(line: bytes, path: pathlib.Path, lineno: int) -> LogRecord:
    try:
        raw = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionError(f"{path}:{lineno}: invalid session event JSON: {exc}") from exc
    try:
        normalized = normalize_event(raw, subject=f"{path.name}:{lineno}")
        return LogRecord.model_validate(normalized)
    except SessionError:
        raise
    except ValueError as exc:
        raise SessionError(f"{path.name}:{lineno}: invalid session event: {exc}") from exc
