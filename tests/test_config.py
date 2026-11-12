"""Lead-owned tests for config loading."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deeppyro import config
from deeppyro.core.errors import ConfigError


def test_default_toml_parses(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(config.DEFAULT_TOML, encoding="utf-8")
    cfg = config.load_config(p, create=False)
    provider, model = cfg.resolve()
    assert cfg.agent.provider == "local"
    assert provider.baseURL == "http://127.0.0.1:8000/v1"
    assert model.startswith("deepseek")


def test_create_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "config.toml"
    cfg = config.load_config(p)
    assert p.exists()
    assert "providers" in cfg.model_dump()


def test_api_key_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "config.toml"
    p.write_text(config.DEFAULT_TOML, encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    cfg = config.load_config(p, create=False)
    assert cfg.providers["deepseek"].resolve_api_key() == "sk-test"


def test_unknown_provider_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[agent]\nprovider = "nope"\n', encoding="utf-8")
    cfg = config.load_config(p, create=False)
    with pytest.raises(KeyError):
        cfg.resolve()


def test_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_CONFIG", str(tmp_path / "c.toml"))
    monkeypatch.setenv("DEEPPYRO_SESSION_DIR", str(tmp_path / "s"))
    assert config.config_path() == tmp_path / "c.toml"
    assert config.session_dir() == tmp_path / "s"
