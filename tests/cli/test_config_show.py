"""config show: effective values, masked secrets, JSON shape and error mapping."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deeppyro.cli import main as cli_main
from deeppyro.cli.frontend import EXIT_ERROR, EXIT_OK, EXIT_USAGE
from deeppyro.core.errors import ConfigError, DeeppyroError

CONFIG_TOML = """
[agent]
provider = "local"
model = "local-model"
permissionPreset = "read-only"
approvalPolicy = "never"

[providers.local]
api = "openai-completions"
baseURL = "http://127.0.0.1:8000/v1"
apiKey = "sk-1234567890abcdef"
models = [{ id = "local-model", name = "Local", contextWindow = 8192 }]
"""

API_KEY = "sk-1234567890abcdef"


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TOML, encoding="utf-8")
    monkeypatch.setenv("DEEPPYRO_CONFIG", str(path))
    monkeypatch.setenv("DEEPPYRO_SESSION_DIR", str(tmp_path / "sessions"))
    return path


def test_config_show_renders_effective_values(runner: CliRunner, config_file: Path) -> None:
    result = runner.invoke(cli_main.app, ["config", "show"])
    assert result.exit_code == EXIT_OK
    assert "local-model" in result.stdout
    assert "http://127.0.0.1:8000/v1" in result.stdout
    assert "read-only" in result.stdout
    assert "never" in result.stdout


def test_config_show_masks_api_key(runner: CliRunner, config_file: Path) -> None:
    result = runner.invoke(cli_main.app, ["config", "show"])
    assert result.exit_code == EXIT_OK
    assert API_KEY not in result.stdout
    assert "sk-1" in result.stdout


def test_config_show_json(runner: CliRunner, config_file: Path) -> None:
    result = runner.invoke(cli_main.app, ["config", "show", "--json"])
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout.strip())
    assert payload["configPath"] == str(config_file)
    assert payload["configExists"] is True
    assert payload["sessionDir"] == str(config_file.parent / "sessions")
    assert payload["agent"]["provider"] == "local"
    assert payload["agent"]["model"] == "local-model"
    assert payload["agent"]["permissionPreset"] == "read-only"
    provider = payload["providers"]["local"]
    assert provider["api"] == "openai-completions"
    assert provider["baseURL"] == "http://127.0.0.1:8000/v1"
    assert provider["apiKey"] == "sk-1…cdef"
    assert provider["apiKeySource"] == "config"
    assert provider["models"] == [{"id": "local-model", "name": "Local", "contextWindow": 8192}]


def test_config_show_missing_file_uses_defaults(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "nested" / "config.toml"
    monkeypatch.setenv("DEEPPYRO_CONFIG", str(path))
    result = runner.invoke(cli_main.app, ["config", "show"])
    assert result.exit_code == EXIT_OK
    assert path.exists()
    assert "deepseek-v4-pro-qwen3.5-9b" in result.stdout


def test_config_show_unknown_provider_is_usage_error(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[agent]\nprovider = "nope"\n', encoding="utf-8")
    monkeypatch.setenv("DEEPPYRO_CONFIG", str(path))
    result = runner.invoke(cli_main.app, ["config", "show"])
    assert result.exit_code == EXIT_USAGE
    assert "unknown provider" in result.stderr.lower()


def test_config_show_config_error_is_usage(runner: CliRunner, fakes) -> None:
    fakes.load_error = ConfigError("broken toml")
    result = runner.invoke(cli_main.app, ["config", "show"])
    assert result.exit_code == EXIT_USAGE
    assert "broken toml" in result.stderr


def test_config_show_runtime_error_is_exit_1(runner: CliRunner, fakes) -> None:
    fakes.load_error = DeeppyroError("disk on fire")
    result = runner.invoke(cli_main.app, ["config", "show"])
    assert result.exit_code == EXIT_ERROR
    assert "disk on fire" in result.stderr
