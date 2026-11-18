"""Reading and atomically rewriting the user plugin manifest.

The manager only owns the patch layer of the layered plugin configuration
(see docs/spec/INTERFACES.md section 11). Entries are read with the kernel
loader so there is exactly one interpretation of the file; they are serialized
here because the project intentionally has no TOML writer dependency.

Writes are atomic: the new document is written to a temporary file in the same
directory and then moved over the target with os.replace.

Because the manifest is a hand-editable user file, `save_user_manifest` edits
the existing document line by line when its structure is understood: comments,
blank lines, unknown keys, and the order of existing `[[plugins]]` blocks are
preserved, and only values that actually change are rewritten. A file that
cannot be edited safely (invalid TOML, duplicate ids, unrelated tables inside a
block that must be dropped) falls back to a clean full rewrite, which is what
this module always used to do.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import tempfile
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from deeppyro.kernel.manifest import PluginSpec, load_manifest
from deeppyro.plugins.errors import ManifestWriteError

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

#: One `[[plugins]]` array-of-tables header line (optional trailing comment).
_HEADER_LINE = re.compile(r"^\s*\[\[\s*plugins\s*\]\]\s*(?:#.*)?$")

#: One `key = value` line inside a plugin block.
_KEY_LINE = re.compile(
    r'^(?P<indent>\s*)(?P<key>[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'
    r"\s*=\s*(?P<rest>.*)$"
)

#: Any regular table header, i.e. everything that is not `[[plugins]]`.
_TABLE_LINE = re.compile(r"^\s*\[(?!\[)")

#: Per-entry keys this module owns; everything else in a block is left alone.
_MANAGED_KEYS = ("id", "name", "disabled", "config", "before", "after")


def read_manifest(path: str | Path) -> tuple[PluginSpec, ...]:
    """Load the user patch manifest through the kernel loader.

    Returns an empty tuple when the file does not exist; raises
    deeppyro.kernel.manifest.ManifestError for invalid content (the caller
    decides whether that is fatal).
    """
    return load_manifest(path)


def _format_key(key: str) -> str:
    if _BARE_KEY.match(key):
        return key
    return json.dumps(key, ensure_ascii=False)


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a subset of TOML basic-string escapes, and
        # always produce a valid single-line TOML value.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        items = ", ".join(f"{_format_key(str(key))} = {_format_value(item)}" for key, item in value.items())
        return "{" + items + "}"
    raise ManifestWriteError(f"cannot serialize TOML value of type {type(value).__name__}")


def _render_block(spec: PluginSpec) -> str:
    """Render one `[[plugins]]` block (no trailing newline)."""
    lines = ["[[plugins]]", f"id = {_format_value(spec.id)}"]
    if spec.name is not None:
        lines.append(f"name = {_format_value(spec.name)}")
    if spec.disabled is not None:
        lines.append(f"disabled = {_format_value(spec.disabled)}")
    if spec.config:
        lines.append(f"config = {_format_value(dict(spec.config))}")
    if spec.before is not None:
        lines.append(f"before = {_format_value(spec.before)}")
    if spec.after is not None:
        lines.append(f"after = {_format_value(spec.after)}")
    return "\n".join(lines)


def dump_manifest(specs: Iterable[PluginSpec]) -> str:
    """Serialize patch entries to TOML text (empty string when there are none)."""
    blocks = [_render_block(spec) for spec in specs]
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


# -- line-preserving editing ---------------------------------------------


def _comment_start(text: str) -> int:
    """Index of an unquoted `#` in text, or -1 when there is none."""
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ('"', "'"):
            quote = char
        elif char == "#":
            return index
    return -1


def _expected_fields(spec: PluginSpec) -> dict[str, str]:
    fields: dict[str, str] = {"id": _format_value(spec.id)}
    if spec.name is not None:
        fields["name"] = _format_value(spec.name)
    if spec.disabled is not None:
        fields["disabled"] = _format_value(spec.disabled)
    if spec.config:
        fields["config"] = _format_value(dict(spec.config))
    if spec.before is not None:
        fields["before"] = _format_value(spec.before)
    if spec.after is not None:
        fields["after"] = _format_value(spec.after)
    return fields


def _replace_value(line: str, match: re.Match[str], value: str) -> str:
    """Replace the value of a single-line key assignment, keeping its comment."""
    rest = match.group("rest")
    comment = _comment_start(rest)
    value_part = rest if comment < 0 else rest[:comment]
    trailing = "" if comment < 0 else rest[comment:]
    stripped = value_part.rstrip()
    if not stripped:
        return line
    separator = value_part[len(stripped) :]
    return line[: match.start("rest")] + value + separator + trailing + line[match.end() :]


_NO_VALUE: Any = object()


def _parse_value(text: str) -> Any:
    """Parse one TOML value fragment, or _NO_VALUE when it is not a single value."""
    try:
        return tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError:
        return _NO_VALUE


def _same_value(existing: str, expected: str) -> bool:
    """Whether two TOML value fragments express the same value."""
    left = _parse_value(existing)
    if left is _NO_VALUE:
        return False
    right = _parse_value(expected)
    return right is not _NO_VALUE and left == right


def _drop_line(line: str, match: re.Match[str]) -> str:
    """Turn a removed assignment into a comment so its explanation survives."""
    rest = match.group("rest")
    comment = _comment_start(rest)
    if comment >= 0:
        return match.group("indent") + rest[comment:].rstrip() + "\n"
    return ""


def _entry_matches_dict(entry: Mapping[str, Any], spec: PluginSpec) -> bool:
    if entry.get("id") != spec.id:
        return False
    for key, expected in (
        ("name", spec.name),
        ("disabled", spec.disabled),
        ("before", spec.before),
        ("after", spec.after),
    ):
        if expected is None:
            if key in entry:
                return False
        elif entry.get(key) != expected:
            return False
    return entry.get("config", {}) == (dict(spec.config) if spec.config else {})


def _parse_block_entries(text: str) -> list[dict[str, Any]] | None:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    entries = document.get("plugins")
    if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
        return None
    return entries


def _entry_matches(text: str, spec: PluginSpec) -> bool:
    entries = _parse_block_entries(text)
    return entries is not None and len(entries) == 1 and _entry_matches_dict(entries[0], spec)


def _patch_block(lines: Sequence[str], spec: PluginSpec) -> str | None:
    """Patch one existing block in place; None when it cannot be edited safely."""
    managed = _expected_fields(spec)
    work = list(lines)
    header = next((position for position, line in enumerate(work) if _HEADER_LINE.match(line)), None)
    if header is None:
        return None
    seen: set[str] = set()
    index = header + 1
    limit = len(work)
    while index < limit:
        line = work[index]
        if _TABLE_LINE.match(line):
            limit = index
            break
        match = _KEY_LINE.match(line)
        if match is None:
            index += 1
            continue
        key = match.group("key")
        if key in managed:
            rest = match.group("rest")
            comment = _comment_start(rest)
            existing = rest if comment < 0 else rest[:comment]
            if not _same_value(existing, managed[key]):
                work[index] = _replace_value(line, match, managed[key])
            seen.add(key)
            index += 1
            continue
        if key in _MANAGED_KEYS:
            replacement = _drop_line(line, match)
            if replacement:
                work[index] = replacement
            else:
                del work[index]
                limit -= 1
            continue
        index += 1
    for key, value in managed.items():
        if key in seen:
            continue
        insert_at = limit
        while insert_at > 0 and not work[insert_at - 1].strip():
            insert_at -= 1
        work.insert(insert_at, f"{key} = {value}\n")
        limit += 1
    text = "".join(work)
    if not text.endswith("\n"):
        text += "\n"
    return text if _entry_matches(text, spec) else None


def _droppable(lines: Sequence[str]) -> bool:
    """Whether a block contains nothing but its entry (safe to move or delete)."""
    header = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _HEADER_LINE.match(line):
            if header:
                return False
            header = True
            continue
        if _KEY_LINE.match(line):
            continue
        return False
    return header


def _split_blocks(text: str) -> tuple[str, list[list[str]]] | None:
    lines = text.splitlines(keepends=True)
    headers = [index for index, line in enumerate(lines) if _HEADER_LINE.match(line)]
    if not headers:
        return None
    # Comments directly above a header describe that block, so they move with it.
    starts: list[int] = []
    for header in headers:
        start = header
        while start > 0:
            previous = lines[start - 1]
            stripped = previous.strip()
            if not stripped.startswith("#") or _HEADER_LINE.match(previous):
                break
            start -= 1
        starts.append(start)
    prefix = "".join(lines[: starts[0]])
    blocks: list[list[str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        blocks.append(lines[start:end])
    return prefix, blocks


def _edit_manifest(original: str, specs: Sequence[PluginSpec]) -> str | None:
    """Line-preserving rewrite; None when the file must be fully rewritten."""
    ids = [spec.id for spec in specs]
    if len(set(ids)) != len(ids):  # duplicate entries are not editable safely
        return None
    split = _split_blocks(original)
    if split is None:
        return None
    prefix, blocks = split
    try:
        document = tomllib.loads(original)
    except tomllib.TOMLDecodeError:
        return None
    entries = document.get("plugins")
    if not isinstance(entries, list) or len(entries) != len(blocks):
        return None
    file_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            return None
        file_ids.append(entry_id)
    if len(set(file_ids)) != len(file_ids):
        return None
    by_id = {spec.id: spec for spec in specs}
    movable = all(_droppable(block) for block in blocks)
    if not movable:
        # A block carries content beyond its own entry (nested tables, unknown
        # keys) and cannot be moved safely: only patch in place, in file order,
        # and refuse edits that would reorder or remove blocks.
        desired_kept = [spec.id for spec in specs if spec.id in set(file_ids)]
        if desired_kept != file_ids:
            return None
    blocks_by_id = {entry_id: block for entry_id, block in zip(file_ids, blocks, strict=True)}
    pieces: list[str] = []
    order: list[str] = []
    for spec in specs:
        block = blocks_by_id.get(spec.id)
        if block is None:
            pieces.append(_render_block(spec) + "\n")
        else:
            patched = _patch_block(block, spec)
            pieces.append(patched if patched is not None else _render_block(spec) + "\n")
        order.append(spec.id)
    out = prefix + "".join(pieces)
    if not out.endswith("\n"):
        out += "\n"
    parsed = _parse_block_entries(out)
    if parsed is None or len(parsed) != len(order):
        return None
    for entry, entry_id in zip(parsed, order, strict=True):
        if not _entry_matches_dict(entry, by_id[entry_id]):
            return None
    return out


# -- writing -------------------------------------------------------------


def _atomic_write(target: Path, text: str) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ManifestWriteError(f"cannot create manifest directory for {target}: {exc}") from exc

    handle = None
    tmp_path: Path | None = None
    try:
        file_descriptor, raw_path = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        tmp_path = Path(raw_path)
        handle = os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n")
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(tmp_path, target)
        tmp_path = None
    except OSError as exc:
        raise ManifestWriteError(f"cannot write manifest {target}: {exc}") from exc
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)


def save_user_manifest(path: str | Path, specs: Iterable[PluginSpec]) -> None:
    """Atomically write (or remove, when empty) the user patch manifest.

    Existing formatting is preserved when possible: comments, blank lines,
    unknown keys and the order of existing entries survive, and only changed
    values are rewritten. Otherwise the file is rewritten from scratch.
    """
    target = Path(path)
    items = list(specs)
    if not items:
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise ManifestWriteError(f"cannot remove empty manifest {target}: {exc}") from exc
        return

    original = ""
    try:
        if target.is_file():
            original = target.read_text(encoding="utf-8")
    except OSError:
        original = ""
    text = _edit_manifest(original, items) if original.strip() else None
    if text is None:
        text = dump_manifest(items)
    _atomic_write(target, text)


__all__ = ["dump_manifest", "read_manifest", "save_user_manifest"]
