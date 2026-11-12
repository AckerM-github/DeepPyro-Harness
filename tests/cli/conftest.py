"""Pytest fixtures for the CLI test-suite."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tests.cli.fakes import FakeApp, Installer, LineReader, make_terminal_frontend


@pytest.fixture
def runner() -> CliRunner:
    """typer/click runner that captures stdout and stderr separately."""
    return CliRunner()


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Installer:
    """Replace build_app/load_config/open_session_store with fake-backed seams."""
    return Installer(monkeypatch).install()


@pytest.fixture
def fake_app(fakes: Installer) -> FakeApp:
    """The FakeApp wired by the fakes fixture."""
    return fakes.app


__all__ = ["LineReader", "make_terminal_frontend"]
