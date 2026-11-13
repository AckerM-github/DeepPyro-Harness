"""Persistence for scheduled tasks (~/.dpro/schedules.json, version 1).

The store is deliberately small: it validates and shapes the file, while the
manager owns scheduling semantics.  Writes are atomic (temporary file in the
same directory, fsync, rename) so a crash never leaves a truncated catalog.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ..paths import deeppyro_home
from .types import (
    STORE_VERSION,
    ScheduleInputError,
    ScheduleRecord,
    ScheduleStoreError,
    record_from_dict,
    record_to_dict,
)

log = logging.getLogger(__name__)


def default_schedules_path() -> Path:
    """Location of the schedules file inside the DeepPyro home directory."""
    return deeppyro_home() / "schedules.json"


class ScheduleStore:
    """Load and atomically persist the schedule catalog."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_schedules_path()

    def load(self) -> dict[str, ScheduleRecord]:
        """Read all tasks; a missing file yields an empty catalog."""
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScheduleStoreError(f"cannot read schedule store {self.path}: {exc}") from exc
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise ScheduleStoreError(f"schedule store {self.path} is not valid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise ScheduleStoreError(f"schedule store {self.path} must contain a JSON object")
        version = document.get("version")
        if version != STORE_VERSION:
            raise ScheduleStoreError(
                f"schedule store {self.path} has unsupported version {version!r} (expected {STORE_VERSION})"
            )
        raw_tasks = document.get("tasks", {})
        if not isinstance(raw_tasks, dict):
            raise ScheduleStoreError(f"schedule store {self.path} tasks entry must be a JSON object")
        records: dict[str, ScheduleRecord] = {}
        for task_id, data in raw_tasks.items():
            if not isinstance(data, dict):
                raise ScheduleStoreError(
                    f"schedule store {self.path} entry {task_id!r} must be a JSON object"
                )
            try:
                record = record_from_dict(data)
            except ScheduleInputError as exc:
                raise ScheduleStoreError(
                    f"schedule store {self.path} entry {task_id!r} is invalid: {exc.message}"
                ) from exc
            records[record.id] = record
        return records

    def save(self, tasks: Mapping[str, ScheduleRecord]) -> None:
        """Write the catalog atomically (temporary file, fsync, rename)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": STORE_VERSION,
            "tasks": {task_id: record_to_dict(record) for task_id, record in tasks.items()},
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
                newline="\n",
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            raise ScheduleStoreError(f"cannot write schedule store {self.path}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def quarantine(self) -> Path | None:
        """Move a corrupt catalog aside so the manager can start from empty."""
        if not self.path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            self.path.replace(target)
        except OSError as exc:
            raise ScheduleStoreError(
                f"cannot quarantine corrupted schedule store {self.path}: {exc}"
            ) from exc
        log.warning("quarantined corrupted schedule store %s -> %s", self.path, target)
        return target


__all__ = ["ScheduleStore", "default_schedules_path"]
