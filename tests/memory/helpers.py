"""Shared helpers for runtime memory tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from deeppyro.memory.store import MemoryStore

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class FakeClock:
    """A deterministic clock; advance() moves it forward."""

    def __init__(self, moment: datetime = FIXED_NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float = 1.0) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


def make_store(tmp_path: Path, *, clock: FakeClock | None = None) -> MemoryStore:
    return MemoryStore(tmp_path / "memory", clock=clock if clock is not None else FakeClock())
