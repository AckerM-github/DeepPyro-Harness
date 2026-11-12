"""Reading and atomically rewriting the user plugin manifest.

The manager only owns the patch layer of the layered plugin configuration
(see docs/spec/INTERFACES.md section 11). Entries are read with the kernel
loader so there is exactly one interpretation of the file; they are serialized
here because the project intentionally has no TOML writer dependency.

Writes are atomic: the new document is written to a temporary file in the same
directory and then moved over the target with os.replace.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from deeppyro.kernel.manifest import PluginSpec, load_manifest
from deeppyro.plugins.errors import ManifestWriteError

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


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


def dump_manifest(specs: Iterable[PluginSpec]) -> str:
    """Serialize patch entries to TOML text (empty string when there are none).

    Comments and unknown top-level keys are not preserved: the manager owns
    this file, and only the [[plugins]] array is part of the contract.
    """
    blocks: list[str] = []
    for spec in specs:
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
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def save_user_manifest(path: str | Path, specs: Iterable[PluginSpec]) -> None:
    """Atomically write (or remove, when empty) the user patch manifest."""
    target = Path(path)
    text = dump_manifest(specs)
    if not text:
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise ManifestWriteError(f"cannot remove empty manifest {target}: {exc}") from exc
        return

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
            try:
                handle.close()
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["dump_manifest", "read_manifest", "save_user_manifest"]
