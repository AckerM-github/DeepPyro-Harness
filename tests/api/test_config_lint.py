"""KI-013: configuration validation is always strict and snake_case only."""

from __future__ import annotations

import tomllib
import warnings
from pathlib import Path

import pytest

from deeppyro.config import DEFAULT_TOML, Config, lint_config, load_config
from deeppyro.core.errors import ConfigError

ALIAS_CASES = [
    ({"agent": {"maxTokens": 1024}}, "agent.maxTokens", "agent.max_tokens"),
    ({"agent": {"maxSteps": 3}}, "agent.maxSteps", "agent.max_steps"),
    ({"sessionDir": "sessions"}, "sessionDir", "session_dir"),
    (
        {"providers": {"local": {"baseURL": "http://127.0.0.1:1/v1"}}},
        "providers.local.baseURL",
        "providers.local.base_url",
    ),
    (
        {"providers": {"local": {"apiKeyEnv": "LOCAL_KEY"}}},
        "providers.local.apiKeyEnv",
        "providers.local.api_key_env",
    ),
    (
        {"providers": {"local": {"apiKey": "secret"}}},
        "providers.local.apiKey",
        "providers.local.api_key",
    ),
    (
        {"providers": {"local": {"models": [{"id": "m", "contextWindow": 8192}]}}},
        "providers.local.models[0].contextWindow",
        "providers.local.models[0].context_window",
    ),
]


@pytest.fixture(autouse=True)
def _clear_strict_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPPYRO_STRICT_CONFIG", raising=False)


@pytest.mark.parametrize(("document", "path", "replacement"), ALIAS_CASES)
def test_camel_case_alias_is_rejected_with_path_and_snake_case(
    document: dict[str, object], path: str, replacement: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.model_validate(document)

    message = str(excinfo.value)
    assert path in message
    assert replacement in message
    assert "not supported" in message


@pytest.mark.parametrize(("document", "path", "replacement"), ALIAS_CASES)
def test_lint_config_reports_alias_with_path_and_snake_case(
    document: dict[str, object], path: str, replacement: str
) -> None:
    messages = lint_config(document)
    assert len(messages) == 1
    assert path in messages[0]
    assert replacement in messages[0]
    assert "not supported" in messages[0]


def test_alias_in_file_fails_to_load(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[agent]\nprovider = "local"\nmodel = "m"\nmaxTokens = 1024\n', encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    message = str(excinfo.value)
    assert "invalid configuration" in message
    assert "agent.maxTokens" in message
    assert "agent.max_tokens" in message


def test_alias_is_an_error_not_a_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[agent]\nmaxTokens = 1\n", encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with pytest.raises(ConfigError):
            load_config(path)


def test_removed_strict_env_switch_does_not_soften_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPPYRO_STRICT_CONFIG", "0")
    path = tmp_path / "config.toml"
    path.write_text("[agent]\nmaxTokens = 8\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"agent\.max_tokens"):
        load_config(path)


def test_unknown_nested_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.model_validate({"agent": {"timeoutMs": 5}})

    message = str(excinfo.value)
    assert "agent.timeoutMs" in message
    assert "unknown configuration key" in message
    assert lint_config({"agent": {"timeoutMs": 5}}) == [
        "unknown configuration key 'agent.timeoutMs' for AgentConfig"
    ]


def test_unknown_provider_and_model_keys_are_listed() -> None:
    document = {
        "providers": {
            "local": {
                "bogusOption": True,
                "models": [{"id": "m", "mystery": 1}],
            }
        }
    }
    messages = lint_config(document)
    joined = "\n".join(messages)
    assert "providers.local.bogusOption" in joined
    assert "providers.local.models[0].mystery" in joined

    with pytest.raises(ConfigError) as excinfo:
        Config.model_validate(document)
    message = str(excinfo.value)
    assert "providers.local.bogusOption" in message
    assert "providers.local.models[0].mystery" in message


def test_every_issue_in_a_file_is_reported() -> None:
    document = {
        "agent": {"maxTokens": 1024},
        "providers": {"local": {"baseURL": "http://127.0.0.1:1/v1"}},
    }
    messages = lint_config(document)
    assert len(messages) == 2
    assert "agent.maxTokens" in messages[0]
    assert "providers.local.baseURL" in messages[1]


def test_plugin_tables_are_kept_and_never_validated() -> None:
    document = {
        "sandbox": {"mode": "read-only", "additionalWritableRoots": []},
        "retry": {"maxRetries": 5, "initialDelayMs": 10},
        "approval": {"hookMode": "off", "hookTools": []},
        "mcp": {"servers": [{"serverName": "x", "toolCallTimeoutMs": 1000}]},
        "permission": {"defaultPreset": "workspace-write"},
        "custom_plugin": {"maxTokens": 5},
    }
    assert lint_config(document) == []

    config = Config.model_validate(document)
    assert config.model_extra is not None
    assert config.model_extra["sandbox"]["mode"] == "read-only"
    assert config.model_extra["custom_plugin"]["maxTokens"] == 5


def test_adapter_options_accept_snake_case() -> None:
    config = Config.model_validate(
        {
            "providers": {
                "p": {
                    "api": "anthropic-messages",
                    "base_url": "https://api.example.invalid",
                    "max_tokens": 200,
                    "reasoning_effort": "high",
                    "models": [
                        {
                            "id": "m",
                            "max_tokens": 100,
                            "reasoning_effort": "low",
                            "input_modalities": ["text"],
                        }
                    ],
                }
            }
        }
    )
    provider = config.providers["p"]
    assert provider.model_extra == {"max_tokens": 200, "reasoning_effort": "high"}
    assert provider.models[0].model_extra == {
        "max_tokens": 100,
        "reasoning_effort": "low",
        "input_modalities": ["text"],
    }


@pytest.mark.parametrize(
    ("spelling", "replacement"),
    [
        ("inputModalities", "input_modalities"),
        ("reasoningEffort", "reasoning_effort"),
        ("maxOutputTokens", "max_tokens"),
        ("maxCompletionTokens", "max_tokens"),
    ],
)
def test_camel_case_adapter_options_are_rejected(spelling: str, replacement: str) -> None:
    document = {"providers": {"p": {"models": [{"id": "m", spelling: 1}]}}}
    with pytest.raises(ConfigError) as excinfo:
        Config.model_validate(document)
    message = str(excinfo.value)
    assert f"providers.p.models[0].{spelling}" in message
    assert replacement in message

    messages = lint_config(document)
    assert len(messages) == 1
    assert replacement in messages[0]


@pytest.mark.parametrize("key", ["strict_config", "strictConfig"])
def test_removed_strict_switches_are_rejected(key: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.model_validate({key: True})
    message = str(excinfo.value)
    assert key in message
    assert "always strict" in message

    messages = lint_config({key: True})
    assert len(messages) == 1
    assert "always strict" in messages[0]


def test_strict_config_in_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('strict_config = true\n\n[agent]\nprovider = "local"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="strict_config"):
        load_config(path)


def test_default_config_file_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_TOML, encoding="utf-8")
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        config = load_config(path)

    assert recorded == []
    assert lint_config(tomllib.loads(DEFAULT_TOML)) == []
    assert config.agent.provider == "local"


def test_clean_document_reports_nothing() -> None:
    assert lint_config({"agent": {"provider": "local", "model": "m", "max_tokens": 8}}) == []


def test_load_config_and_model_validate_agree(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[agent]\nmaxTokens = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as load_exc:
        load_config(path)
    with pytest.raises(ConfigError) as model_exc:
        Config.model_validate({"agent": {"maxTokens": 1}})
    assert "agent.max_tokens" in str(load_exc.value)
    assert "agent.max_tokens" in str(model_exc.value)


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("not valid toml = =\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid config file"):
        load_config(path)
