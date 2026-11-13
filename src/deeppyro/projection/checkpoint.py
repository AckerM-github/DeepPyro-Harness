"""Durable projection checkpoints stored next to the session log.

The sidecar is a JSON object {"version": 1, "cells": {key: {ver, seq, val}}}
written to <session file>.projections.json.  Writes are atomic (temp file in
the same directory, fsync, os.replace) and write-behind: hosts persist only on
an explicit checkpoint or session close, never per event.

A checkpoint lets a fresh process restore projection cells without replaying
the whole log: restore installs every version-matching row and replays only the
records after it.  Any corrupt or unsupported sidecar degrades to "no
checkpoint" with a warning; it never blocks opening the session.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from deeppyro.projection.registry import ProjectionError, ProjectionRegistry

if TYPE_CHECKING:
    from deeppyro.session.session import Session

logger = logging.getLogger("deeppyro.projection.checkpoint")

SIDECAR_SUFFIX = ".projections.json"
SIDECAR_VERSION = 1


def sidecar_path(session: Session) -> Path:
    """Checkpoint sidecar path for session (adjacent to the session file)."""
    return Path(str(session.path) + SIDECAR_SUFFIX)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def encode_checkpoint(rows: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize registry checkpoint rows into the durable sidecar payload.

    Raises:
        ProjectionError: rows are not a mapping of well-formed checkpoints.
    """
    if not isinstance(rows, Mapping):
        raise ProjectionError("checkpoint rows must be a mapping")
    cells: dict[str, Any] = {}
    for key, row in rows.items():
        if not isinstance(key, str) or not key:
            raise ProjectionError("checkpoint keys must be non-empty strings")
        if not isinstance(row, Mapping):
            raise ProjectionError(f"checkpoint row for {key!r} must be a mapping")
        version = row.get("ver")
        seq = row.get("seq")
        if not _is_int(version) or version < 0:
            raise ProjectionError(f"checkpoint row for {key!r} has an invalid ver")
        if not _is_int(seq) or seq < -1:
            raise ProjectionError(f"checkpoint row for {key!r} has an invalid seq")
        if "val" not in row:
            raise ProjectionError(f"checkpoint row for {key!r} carries no val")
        cells[key] = {"ver": version, "seq": seq, "val": row["val"]}
    return {"version": SIDECAR_VERSION, "cells": cells}


def decode_checkpoint(payload: Any) -> dict[str, dict[str, Any]] | None:
    """Decode a sidecar payload, tolerating corrupt rows.

    Returns None (and logs a warning) when the payload as a whole is not a
    supported checkpoint; individually malformed rows are dropped with a
    warning so one bad unit cannot poison the rest.
    """
    if not isinstance(payload, Mapping):
        logger.warning("ignoring projection sidecar with a non-object payload")
        return None
    version = payload.get("version")
    if version != SIDECAR_VERSION:
        logger.warning("ignoring projection sidecar with version %r", version)
        return None
    cells = payload.get("cells")
    if not isinstance(cells, Mapping):
        logger.warning("ignoring projection sidecar without a cells object")
        return None
    rows: dict[str, dict[str, Any]] = {}
    for key, row in cells.items():
        if not isinstance(key, str) or not key:
            logger.warning("dropping projection sidecar row with an invalid key")
            continue
        if not isinstance(row, Mapping):
            logger.warning("dropping malformed projection sidecar row %s", key)
            continue
        item_version = row.get("ver")
        seq = row.get("seq")
        if not _is_int(item_version) or item_version < 0:
            logger.warning("dropping projection sidecar row %s with an invalid ver", key)
            continue
        if not _is_int(seq) or seq < -1:
            logger.warning("dropping projection sidecar row %s with an invalid seq", key)
            continue
        if "val" not in row:
            logger.warning("dropping projection sidecar row %s without a val", key)
            continue
        rows[key] = {"ver": item_version, "seq": seq, "val": row["val"]}
    return rows


def read_checkpoint(session: Session) -> dict[str, dict[str, Any]] | None:
    """Read the sidecar for session; None when absent or unusable."""
    path = sidecar_path(session)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("cannot read projection sidecar %s: %s", path, exc)
        return None
    try:
        payload = json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("cannot parse projection sidecar %s: %s", path, exc)
        return None
    return decode_checkpoint(payload)


def write_checkpoint(session: Session, rows: Mapping[str, Any]) -> Path:
    """Atomically write checkpoint rows to the session sidecar.

    The payload is serialized first (so a non-JSON value raises before any file
    is touched), written to a unique temp file in the same directory, flushed
    and fsynced, then replaced into place.

    Raises:
        ProjectionError: rows are malformed or not JSON serializable.
    """
    path = sidecar_path(session)
    payload = encode_checkpoint(rows)
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ProjectionError(f"cannot serialize projection checkpoint: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("cannot remove temporary projection sidecar %s", tmp)
        raise ProjectionError(f"cannot write projection sidecar {path}: {exc}") from exc
    return path


def save_checkpoint(registry: ProjectionRegistry, session: Session) -> Path:
    """Checkpoint registry state for session and persist it to disk."""
    return write_checkpoint(session, registry.checkpoint(session))


def load_checkpoint(registry: ProjectionRegistry, session: Session) -> dict[str, dict[str, Any]] | None:
    """Restore registry cells from the session sidecar, when one exists.

    Returns the rows that were applied (or None when there was no usable
    sidecar).  Restoring is always safe: rows that do not match the current log
    or state version fall back to a full replay.
    """
    rows = read_checkpoint(session)
    if rows is None:
        return None
    registry.restore(session, rows)
    return rows


@dataclass(slots=True)
class ProjectionCheckpoints:
    """Convenience wrapper binding a registry to a session sidecar policy.

    Hosts construct this once next to their registry, call load at session open
    and save on close or periodically; enabled=False makes every call a no-op
    (useful for tests and read-only hosts).
    """

    registry: ProjectionRegistry
    enabled: bool = True

    def path_for(self, session: Session) -> Path:
        """Sidecar path this wrapper reads and writes for session."""
        return sidecar_path(session)

    def save(self, session: Session, *, rows: Mapping[str, Any] | None = None) -> Path | None:
        """Persist a checkpoint (computed from the registry when rows is None)."""
        if not self.enabled:
            return None
        if rows is None:
            rows = self.registry.checkpoint(session)
        return write_checkpoint(session, rows)

    def load(self, session: Session) -> bool:
        """Restore cells if a usable sidecar exists; True when restored."""
        if not self.enabled:
            return False
        rows = read_checkpoint(session)
        if rows is None:
            return False
        self.registry.restore(session, rows)
        return True

    def restore_if_available(self, session: Session) -> bool:
        """Alias for load(), named for wiring code that reads like a fallback."""
        return self.load(session)


__all__ = [
    "SIDECAR_SUFFIX",
    "SIDECAR_VERSION",
    "ProjectionCheckpoints",
    "decode_checkpoint",
    "encode_checkpoint",
    "load_checkpoint",
    "read_checkpoint",
    "save_checkpoint",
    "sidecar_path",
    "write_checkpoint",
]
