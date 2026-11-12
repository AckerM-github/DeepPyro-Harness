"""dpyro sessions: ordering, --limit, empty store and error exit codes."""

from __future__ import annotations

from typer.testing import CliRunner

from deeppyro.cli import main as cli_main
from deeppyro.cli.frontend import EXIT_ERROR, EXIT_OK, EXIT_USAGE
from deeppyro.core.errors import ConfigError, DeeppyroError
from tests.cli.fakes import FakeInfo

SESSIONS = [
    FakeInfo("session-aaaaaaaa-0000-0000-0000-000000000000", "fix parser bug", 3),
    FakeInfo("session-bbbbbbbb-0000-0000-0000-000000000000", "add cli tests", 5),
    FakeInfo("session-cccccccc-0000-0000-0000-000000000000", "old task", 1),
]


def test_sessions_table_lists_all(runner: CliRunner, fakes) -> None:
    fakes.app.store.infos = list(SESSIONS)
    result = runner.invoke(cli_main.app, ["sessions"])
    assert result.exit_code == EXIT_OK
    assert "fix parser bug" in result.stdout
    assert "add cli tests" in result.stdout
    assert "old task" in result.stdout
    assert "aaaaaaaa" in result.stdout
    assert "3" in result.stdout


def test_sessions_limit_trims_rows(runner: CliRunner, fakes) -> None:
    fakes.app.store.infos = list(SESSIONS)
    result = runner.invoke(cli_main.app, ["sessions", "--limit", "2"])
    assert result.exit_code == EXIT_OK
    assert "fix parser bug" in result.stdout
    assert "add cli tests" in result.stdout
    assert "old task" not in result.stdout


def test_sessions_empty_store(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["sessions"])
    assert result.exit_code == EXIT_OK
    assert "no sessions found" in result.stdout


def test_sessions_invalid_limit_is_usage_error(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["sessions", "--limit", "0"])
    assert result.exit_code == EXIT_USAGE


def test_sessions_config_error_is_usage(runner: CliRunner, fakes) -> None:
    fakes.load_error = ConfigError("bad config")
    result = runner.invoke(cli_main.app, ["sessions"])
    assert result.exit_code == EXIT_USAGE
    assert "bad config" in result.stderr


def test_sessions_store_error_is_exit_1(runner: CliRunner, fakes) -> None:
    fakes.store_error = DeeppyroError("cannot read store")
    result = runner.invoke(cli_main.app, ["sessions"])
    assert result.exit_code == EXIT_ERROR
    assert "cannot read store" in result.stderr
