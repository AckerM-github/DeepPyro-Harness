"""Tests for the KI-013 config lint: alias warnings, unknown keys and strict mode."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from deeppyro.config import DEFAULT_TOML, ConfigWarning, lint_config, load_config
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
def test_camel_case_alias_warns_with_path_and_snake_case(
    document: dict[str, object], path: str, replacement: str
) -> None:
    with pytest.warns(DeprecationWarning) as recorded:
        lint_config(document)

    message = str(recorded[0].message)
    assert path in message
    assert replacement in message
    assert "deprecated camelCase alias" in message


def test_alias_value_still_loads_in_compat_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[agent]\nprovider = "local"\nmodel = "m"\nmaxTokens = 1024\n',
        encoding="utf-8",
    )
    with pytest.warns(DeprecationWarning, match=r"agent\.maxTokens"):
        config = load_config(path)

    assert config.agent.max_tokens == 1024


def test_unknown_nested_key_warns_with_path() -> None:
    with pytest.warns(ConfigWarning) as recorded:
        messages = lint_config({"agent": {"timeoutMs": 5}})

    message = str(recorded[0].message)
    assert "agent.timeoutMs" in message
    assert "unknown configuration key" in message
    assert len(messages) == 1


def test_unknown_provider_and_model_keys_are_listed() -> None:
    with pytest.warns(ConfigWarning) as recorded:
        lint_config(
            {
                "providers": {
                    "local": {
                        "bogusOption": True,
                        "models": [{"id": "m", "mystery": 1}],
                    }
                }
            }
        )

    joined = "\n".join(str(item.message) for item in recorded)
    assert "providers.local.bogusOption" in joined
    assert "providers.local.models[0].mystery" in joined


def test_strict_rejects_aliases_with_every_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_STRICT_CONFIG", "1")
    document = {
        "agent": {"maxTokens": 1024},
        "providers": {"local": {"baseURL": "http://127.0.0.1:1/v1"}},
    }
    with pytest.raises(ConfigError) as excinfo:
        lint_config(document)

    message = str(excinfo.value)
    assert "agent.maxTokens" in message
    assert "providers.local.baseURL" in message
    assert "invalid configuration" in message


def test_strict_argument_beats_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_STRICT_CONFIG", "1")
    with pytest.warns(DeprecationWarning):
        lint_config({"agent": {"maxTokens": 1}}, strict=False)


def test_strict_from_file_top_level_key() -> None:
    document = {"strict_config": True, "agent": {"maxTokens": 1}}
    with pytest.raises(ConfigError, match=r"agent\.maxTokens"):
        lint_config(document)
    # Canonical keys are fine, and the switch itself is not schema noise.
    assert lint_config({"strict_config": True, "agent": {"max_tokens": 1}}) == []


def test_load_config_strict_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[agent]\nmaxTokens = 8\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"agent\.maxTokens"):
        load_config(path, strict=True)


def test_load_config_strict_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_STRICT_CONFIG", "true")
    path = tmp_path / "config.toml"
    path.write_text("[agent]\nmaxTokens = 8\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"agent\.maxTokens"):
        load_config(path)


def test_strict_env_non_truthy_keeps_compat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_STRICT_CONFIG", "0")
    path = tmp_path / "config.toml"
    path.write_text("[agent]\nmaxTokens = 8\n", encoding="utf-8")
    with pytest.warns(DeprecationWarning):
        config = load_config(path)
    assert config.agent.max_tokens == 8


def test_plugin_sections_are_never_reported(tmp_path: Path) -> None:
    document = {
        "sandbox": {"additionalWritableRoots": []},
        "retry": {"maxRetries": 5, "initialDelayMs": 10},
        "approval": {"hookMode": "off", "hookTools": []},
        "mcp": {"servers": [{"serverName": "x", "toolCallTimeoutMs": 1000}]},
        "permission": {"defaultPreset": "workspace-write"},
        "custom_plugin": {"anything": 1},
    }
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        assert lint_config(document, strict=True) == []

    assert recorded == []


def test_default_config_file_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(DEFAULT_TOML, encoding="utf-8")
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        config = load_config(path)

    lint = [item for item in recorded if issubclass(item.category, (ConfigWarning, DeprecationWarning))]
    assert lint == []
    assert config.agent.provider == "local"


def test_clean_document_without_warnings() -> None:
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        assert lint_config({"agent": {"provider": "local", "model": "m", "max_tokens": 8}}) == []
    assert recorded == []


def test_strict_config_key_does_not_leak_into_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('strict_config = true\n\n[agent]\nprovider = "local"\nmodel = "m"\n', encoding="utf-8")
    config = load_config(path)
    assert config.agent.provider == "local"
    assert "strict_config" not in (config.model_extra or {})


def test_invalid_toml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("not valid toml = =\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid config file"):
        load_config(path)
