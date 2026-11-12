"""Command surface: help, version, unknown commands, usage errors and exit codes."""

from __future__ import annotations

from typer.testing import CliRunner

from deeppyro.cli import main as cli_main
from deeppyro.cli.frontend import EXIT_USAGE


def test_help_lists_all_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "chat", "sessions", "config"):
        assert command in result.stdout


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["--version"])
    assert result.exit_code == 0
    assert "dpyro" in result.stdout
    assert cli_main.__version__ in result.stdout


def test_no_args_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, [])
    assert result.exit_code == EXIT_USAGE
    assert "Usage" in result.stdout


def test_unknown_command_exits_usage(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["bogus"])
    assert result.exit_code == EXIT_USAGE


def test_run_without_prompt_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["run"])
    assert result.exit_code == EXIT_USAGE
    assert "no prompt given" in result.stderr


def test_run_blank_prompt_is_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["run", "   "])
    assert result.exit_code == EXIT_USAGE
    assert "no prompt given" in result.stderr


def test_max_steps_zero_is_rejected_by_parser(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["run", "hi", "--max-steps", "0"])
    assert result.exit_code == EXIT_USAGE


def test_max_steps_negative_is_rejected_by_parser(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["run", "hi", "--max-steps", "-3"])
    assert result.exit_code == EXIT_USAGE


def test_config_subcommand_help(runner: CliRunner) -> None:
    result = runner.invoke(cli_main.app, ["config", "--help"])
    assert result.exit_code == 0
    assert "show" in result.stdout
