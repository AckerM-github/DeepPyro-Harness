"""Shared fixtures for runtime memory tests.

Every test gets an isolated DEEPPYRO_HOME so the plugin never reads or writes
the real user's ~/.dpro directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DEEPPYRO_HOME", str(home))
    return home
