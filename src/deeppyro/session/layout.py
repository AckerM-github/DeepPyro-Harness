"""On-disk layout for session v4 logs.

DSH stores each session under
<root>/<projectKey(cwd)>/<encodeSegment(sessionId)>/session.v4.<ext>.
This module owns those encodings plus discovery of existing log files.  The
store also writes a flat <root>/<encodeSegment(sessionId)>.jsonl layout for
locally created sessions; the reader accepts both.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

from deeppyro.core.errors import SessionError

LOG_BASENAME = "session.v4"
LOG_SUFFIXES = (".jsonl", ".jsonl.zstd")

_SAFE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
_SEPARATORS = frozenset("/\\:")


def _utf16_units(value: str) -> list[int]:
    """Return the UTF-16 code units of *value* (DSH encodes UTF-16 units)."""
    encoded = value.encode("utf-16-le")
    return [encoded[index] | (encoded[index + 1] << 8) for index in range(0, len(encoded), 2)]


def _escape_unit(unit: int) -> str:
    return f"~{unit:04X}"


def encode_segment(value: str) -> str:
    """Encode one path segment the way DSH's encodeSegment does.

    Dot segments are escaped explicitly, safe characters pass through, and every
    other UTF-16 code unit becomes a ~XXXX escape (upper-case hex).
    """
    if not isinstance(value, str) or not value:
        raise SessionError("session path segment must be a non-empty string")
    if value == ".":
        return "~002E"
    if value == "..":
        return "~002E~002E"
    parts: list[str] = []
    for unit in _utf16_units(value):
        char = chr(unit)
        if unit < 0x80 and char in _SAFE_CHARS and char != "~":
            parts.append(char)
        else:
            parts.append(_escape_unit(unit))
    return "".join(parts)


def project_key(cwd: str) -> str:
    """Encode a working directory into DSH's project directory key."""
    if not isinstance(cwd, str) or not cwd:
        raise SessionError("cannot derive a project key from an empty cwd")
    parts: list[str] = []
    previous_separator = False
    for unit in _utf16_units(cwd):
        char = chr(unit)
        if unit < 0x80 and char in _SEPARATORS:
            if not previous_separator:
                parts.append("-")
            previous_separator = True
            continue
        previous_separator = False
        if unit < 0x80 and char in _SAFE_CHARS and char != "~":
            parts.append(char)
        else:
            parts.append(_escape_unit(unit))
    readable = "".join(parts).lstrip("-")
    if not readable:
        readable = "root"
    readable = readable[:251]
    return f"--{readable}--"


def log_filename(*, zstd: bool = False) -> str:
    """Return the canonical file name for a v4 session log."""
    return LOG_BASENAME + (LOG_SUFFIXES[1] if zstd else LOG_SUFFIXES[0])


def session_file_path(root: pathlib.Path, session_id: str, cwd: str, *, zstd: bool = False) -> pathlib.Path:
    """Return the canonical nested path for one session log."""
    return pathlib.Path(root) / project_key(cwd) / encode_segment(session_id) / log_filename(zstd=zstd)


def flat_session_path(root: pathlib.Path, session_id: str, *, zstd: bool = False) -> pathlib.Path:
    """Return the flat layout path used for locally created sessions."""
    return pathlib.Path(root) / (encode_segment(session_id) + (LOG_SUFFIXES[1] if zstd else LOG_SUFFIXES[0]))


def is_log_file(path: pathlib.Path) -> bool:
    """Whether *path* looks like a session v4 log (any supported compression)."""
    return path.name.endswith(LOG_SUFFIXES)


def discover_session_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    """Yield every plausible session log under *root* in deterministic order.

    Handles the flat layout and the DSH nested layout without a recursive full
    filesystem walk: root/*.jsonl[.zstd], root/<project>/session.v4.* and
    root/<project>/<session>/session.v4.*.  Canonical session.v4 names win over
    any stray *.jsonl file in the same directory.
    """
    base = pathlib.Path(root)
    if not base.is_dir():
        return
    flat: list[pathlib.Path] = []
    nested: list[pathlib.Path] = []
    for child in sorted(base.iterdir()):
        if child.is_file():
            if is_log_file(child):
                flat.append(child)
            continue
        if not child.is_dir():
            continue
        for grandchild in sorted(child.iterdir()):
            if grandchild.is_file() and grandchild.name.startswith(LOG_BASENAME):
                nested.append(grandchild)
            elif grandchild.is_dir():
                for candidate in sorted(grandchild.iterdir()):
                    if candidate.is_file() and candidate.name.startswith(LOG_BASENAME):
                        nested.append(candidate)
    yield from sorted(flat)
    yield from sorted(nested)
