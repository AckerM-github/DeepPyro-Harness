"""Shared test isolation for user-level state.

Tests must never touch the real ~/.dpro directory: prompt history is redirected
to a per-test temporary file. Tests that exercise path resolution remove this
override explicitly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_prompt_history(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("DEEPPYRO_HISTORY", str(tmp_path / "history.jsonl"))
