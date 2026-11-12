"""Fixtures shared by the tool and sandbox tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """An isolated workspace directory for filesystem and sandbox tests."""
    root = tmp_path / "workspace"
    root.mkdir()
    return root
