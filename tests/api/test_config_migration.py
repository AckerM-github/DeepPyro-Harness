"""Pre-0.3 configuration files are upgraded before the runtime boots."""

from __future__ import annotations

import tomllib

import pytest

from deeppyro.config import Config, load_config, migrate_legacy_config
from deeppyro.core.errors import ConfigError

OLD_CONFIG = """# keep this comment
[agent]
provider = "local"
model = "m"
permissionPreset = "workspace-write"
approvalPolicy = "ask"
maxSteps = 4

[providers.local]
api = "openai"
baseURL = "http://127.0.0.1:1/v1"
apiKeyEnv = "LOCAL_KEY"
models = [{ id = "m", contextWindow = 8192 }]

[permission]
# keep this one too
"""


def test_old_config_is_migrated_and_boots(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(OLD_CONFIG, encoding="utf-8")

    result = migrate_legacy_config(path)

    assert result.changed is True
    assert result.backup is not None
    assert result.backup.read_text(encoding="utf-8") == OLD_CONFIG
    migrated = path.read_text(encoding="utf-8")
    assert "baseURL" not in migrated
    assert "apiKeyEnv" not in migrated
    assert "contextWindow" not in migrated
    assert "permissionPreset" not in migrated
    assert "approvalPolicy" not in migrated
    assert "# keep this comment" in migrated
    assert "# keep this one too" in migrated
    data = tomllib.loads(migrated)
    assert data["agent"] == {"provider": "local", "model": "m", "max_steps": 4}
    assert data["permission"]["defaultPreset"] == "workspace-write"
    assert data["approval"]["policy"] == "ask"
    cfg = load_config(path)
    assert isinstance(cfg, Config)
    assert cfg.providers["local"].base_url == "http://127.0.0.1:1/v1"
    assert cfg.providers["local"].api_key_env == "LOCAL_KEY"
    assert cfg.providers["local"].models[0].context_window == 8192


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(OLD_CONFIG, encoding="utf-8")
    migrate_legacy_config(path)
    before = path.read_bytes()

    result = migrate_legacy_config(path)

    assert result.changed is False
    assert result.backup is None
    assert path.read_bytes() == before


def test_alias_names_inside_strings_and_comments_survive(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[agent]\n"
        'provider = "local"\n'
        'model = "apiKey-maxTokens#1"  # maxSteps stays in the comment\n'
        "maxSteps = 2\n"
        "\n"
        "[providers.local]\n"
        'api = "openai"\n'
        'baseURL = "http://127.0.0.1:1/v1"\n'
        'models = [{ id = "baseURL", contextWindow = 8192 }]\n',
        encoding="utf-8",
    )

    migrate_legacy_config(path)

    migrated = path.read_text(encoding="utf-8")
    assert 'model = "apiKey-maxTokens#1"  # maxSteps stays in the comment' in migrated
    assert 'id = "baseURL"' in migrated
    data = tomllib.loads(migrated)
    assert data["agent"]["model"] == "apiKey-maxTokens#1"
    assert data["agent"]["max_steps"] == 2
    assert data["providers"]["local"]["models"][0]["id"] == "baseURL"


def test_load_config_stays_strict(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(OLD_CONFIG, encoding="utf-8")

    with pytest.raises(ConfigError, match="baseURL"):
        load_config(path)
