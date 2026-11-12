"""Persistent prompt history for the TUI.

History is stored as JSON Lines (one object per submitted prompt) in the
DeepPyro home directory, capped at MAX_HISTORY entries. Read and write
failures are ignored on purpose: history is a convenience and must never stop
the user from submitting input.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from deeppyro import paths

MAX_HISTORY = 200
APP_DIR_NAME = paths.HOME_DIR_NAME
HISTORY_FILE_NAME = "history.jsonl"
HISTORY_PATH_ENV = paths.HISTORY_ENV


def default_history_path() -> Path:
    """Return the history file path, honouring test and portable overrides.

    Priority: DEEPPYRO_HISTORY, then <DEEPPYRO_HOME or ~/.dpro>/history.jsonl.
    The file name is always history.jsonl.
    """
    return paths.history_path()


class History:
    """An append-only list of prompts mirrored to a JSONL file.

    Entries are stored oldest first; the last entry is the most recent prompt.
    """

    def __init__(self, path: Path | None = None, limit: int = MAX_HISTORY) -> None:
        self.path = default_history_path() if path is None else Path(path)
        self.limit = max(1, limit)
        self.entries: list[str] = []

    def __len__(self) -> int:
        return len(self.entries)

    def load(self) -> None:
        """Read existing entries, ignoring malformed lines and I/O errors."""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        entries: list[str] = []
        for line in lines:
            text = self._decode_line(line)
            if text is not None:
                entries.append(text)
        self.entries = entries[-self.limit :]

    @staticmethod
    def _decode_line(line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # Tolerate a plain text file (for example one written by hand).
            return line or None
        if isinstance(value, str):
            text = value
        elif isinstance(value, dict):
            text = value.get("text")
        else:
            return None
        if not isinstance(text, str):
            return None
        text = text.rstrip()
        return text or None

    def append(self, text: str) -> None:
        """Record one submitted prompt and persist the trimmed history."""
        text = text.rstrip()
        if not text.strip():
            return
        if self.entries and self.entries[-1] == text:
            return
        self.entries.append(text)
        if len(self.entries) > self.limit:
            del self.entries[: len(self.entries) - self.limit]
        self.save()

    def save(self) -> None:
        """Write every entry to disk; silently give up on any OS error."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).isoformat(timespec="seconds")
            payload = "".join(
                json.dumps({"text": entry, "at": stamp}, ensure_ascii=False) + "\n" for entry in self.entries
            )
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            return


__all__ = [
    "APP_DIR_NAME",
    "HISTORY_FILE_NAME",
    "HISTORY_PATH_ENV",
    "MAX_HISTORY",
    "History",
    "default_history_path",
]
