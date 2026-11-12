"""dpyro run: NDJSON stream purity, rich streaming output, exit codes, overrides."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from deeppyro.cli import main as cli_main
from deeppyro.cli.app import resolve_workspace
from deeppyro.cli.frontend import EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE
from deeppyro.core.errors import ConfigError, LlmError


def ndjson(result: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_run_supports_async_agent_session(runner: CliRunner, fakes) -> None:
    fakes.app.async_agent = True
    result = runner.invoke(cli_main.app, ["run", "hi", "--json"])
    assert result.exit_code == EXIT_OK
    assert fakes.app.agents[0].sent == ["hi"]


def test_run_async_agent_interrupt_returns_130(runner: CliRunner, fakes) -> None:
    fakes.app.async_agent = True
    fakes.app.keyboard_interrupt = True
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_INTERRUPTED
    assert fakes.app.agents[0].interrupted is True


def test_run_json_emits_one_event_per_line(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["run", "hello", "--json"])
    assert result.exit_code == EXIT_OK
    events = ndjson(result)
    assert events[0]["type"] == "session"
    assert events[0]["sessionId"].startswith("session-")
    assert [event["type"] for event in events] == [
        "session",
        "status",
        "thinking",
        "text",
        "usage",
        "final",
    ]
    assert events[3] == {"type": "text", "text": "hello"}
    assert events[4] == {"type": "usage", "input": 10, "output": 5, "cached": 2}
    assert events[5] == {"type": "final", "text": "hello"}
    assert result.stderr == ""


def test_run_json_has_no_human_output(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["run", "hello", "--json"])
    assert result.exit_code == EXIT_OK
    assert "dpyro" not in result.stdout
    assert "tokens" not in result.stdout
    assert "→" not in result.stdout


def test_run_streams_text_tools_and_footer(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["run", "hello"])
    session_id = fakes.app.store.sessions[0].session_id
    assert result.exit_code == EXIT_OK
    assert "dpyro" in result.stdout
    assert "local/local-model" in result.stdout
    assert "pondering" in result.stdout
    assert result.stdout.count("hello") == 1
    assert "tokens in=10 out=5 cached=2" in result.stdout
    assert f"session {session_id}" in result.stdout
    assert f"session {session_id.removeprefix('session-')[:8]}" in result.stdout


def test_run_tool_events_rendered(runner: CliRunner, fakes) -> None:
    fakes.app.agent_events = [
        {"type": "tool_call", "callId": "c1", "name": "read", "arguments": '{"path": "a.txt"}'},
        {"type": "tool_result", "callId": "c1", "isError": False, "text": "file body"},
        {"type": "final", "text": "done"},
    ]
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_OK
    assert "read" in result.stdout
    assert "file body" in result.stdout
    assert "done" in result.stdout


def test_run_error_event_is_exit_1(runner: CliRunner, fakes) -> None:
    fakes.app.agent_events = [{"type": "error", "message": "kaboom"}]
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_ERROR
    assert "kaboom" in result.stderr


def test_run_json_error_event_still_ndjson(runner: CliRunner, fakes) -> None:
    fakes.app.agent_events = [{"type": "error", "message": "kaboom"}]
    result = runner.invoke(cli_main.app, ["run", "hi", "--json"])
    assert result.exit_code == EXIT_ERROR
    events = ndjson(result)
    assert events[-1]["type"] == "error"
    assert "kaboom" in result.stdout


def test_run_build_config_error_is_usage(runner: CliRunner, fakes) -> None:
    fakes.build_error = ConfigError("unknown provider: 'nope'")
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_USAGE
    assert "unknown provider" in result.stderr


def test_run_build_runtime_error_is_exit_1(runner: CliRunner, fakes) -> None:
    fakes.build_error = LlmError("no credentials")
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_ERROR
    assert "no credentials" in result.stderr


def test_run_agent_exception_is_exit_1(runner: CliRunner, fakes) -> None:
    fakes.app.agent_exc = LlmError("model died")
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_ERROR
    assert "model died" in result.stderr
    assert fakes.app.closed is True


def test_run_keyboard_interrupt_is_130(runner: CliRunner, fakes) -> None:
    fakes.app.keyboard_interrupt = True
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_INTERRUPTED
    assert "interrupted" in result.stderr
    assert fakes.app.agents[0].interrupted is True
    assert fakes.app.closed is True


def test_run_json_interrupt_keeps_stdout_clean(runner: CliRunner, fakes) -> None:
    fakes.app.keyboard_interrupt = True
    result = runner.invoke(cli_main.app, ["run", "hi", "--json"])
    assert result.exit_code == EXIT_INTERRUPTED
    assert "interrupted" in result.stderr
    for line in result.stdout.splitlines():
        assert json.loads(line)["type"] == "session"


def test_run_passes_max_steps(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["run", "hi", "--max-steps", "3"])
    assert result.exit_code == EXIT_OK
    assert fakes.app.max_steps_args == [3]


def test_run_default_max_steps_is_none(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_OK
    assert fakes.app.max_steps_args == [None]


def test_run_provider_and_model_overrides(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["run", "hi", "--provider", "deepseek", "--model", "deepseek-chat"])
    assert result.exit_code == EXIT_OK
    assert fakes.build_kwargs["provider"] == "deepseek"
    assert fakes.build_kwargs["model"] == "deepseek-chat"


def test_run_reads_prompt_from_stdin(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["run"], input="piped prompt")
    assert result.exit_code == EXIT_OK
    assert fakes.app.agents[0].sent == ["piped prompt"]


def test_run_missing_cwd_is_usage_error(runner: CliRunner, fakes) -> None:
    fakes.build_error = ConfigError("workspace directory does not exist: D:/nope")
    result = runner.invoke(cli_main.app, ["run", "hi", "--cwd", "D:/nope"])
    assert result.exit_code == EXIT_USAGE
    assert "workspace directory does not exist" in result.stderr


def test_resolve_workspace(tmp_path: Path) -> None:
    assert resolve_workspace(tmp_path) == tmp_path.resolve()
    with pytest.raises(ConfigError):
        resolve_workspace(tmp_path / "missing")


def test_run_closes_provider(runner: CliRunner, fakes) -> None:
    result = runner.invoke(cli_main.app, ["run", "hi"])
    assert result.exit_code == EXIT_OK
    assert fakes.app.closed is True
