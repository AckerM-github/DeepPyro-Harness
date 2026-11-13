"""Lead-owned tests for config loading."""

from __future__ import annotations

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


def test_defaults_live_in_the_deeppyro_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    for name in ("DEEPPYRO_CONFIG", "DEEPPYRO_SESSION_DIR"):
        monkeypatch.delenv(name, raising=False)
    assert config.config_path() == tmp_path / "home" / "config.toml"
    assert config.session_dir() == tmp_path / "home" / "sessions"


def test_save_agent_selection_preserves_comments(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        '# my config\n[agent]\nprovider = "local"  # keep me\nmodel = "old"\n\n'
        '[providers.local]\nbaseURL = "http://127.0.0.1:8000/v1"\n',
        encoding="utf-8",
    )
    written = config.save_agent_selection("deepseek", "deepseek-chat", p)
    text = p.read_text(encoding="utf-8")
    assert written == p
    assert "# my config" in text
    assert "# keep me" in text
    assert "[providers.local]" in text
    cfg = config.load_config(p, create=False)
    assert (cfg.agent.provider, cfg.agent.model) == ("deepseek", "deepseek-chat")


def test_save_agent_selection_inserts_missing_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[agent]\nmaxTokens = 1024\n\n[bundle]\nid = "core"\n', encoding="utf-8")
    config.save_agent_selection("local", "local-model", p)
    cfg = config.load_config(p, create=False)
    assert (cfg.agent.provider, cfg.agent.model) == ("local", "local-model")
    assert cfg.agent.maxTokens == 1024
    lines = [line.strip() for line in p.read_text(encoding="utf-8").splitlines()]
    assert lines.index('provider = "local"') < lines.index('model = "local-model"')


def test_save_agent_selection_appends_agent_section(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[providers.local]\nbaseURL = "http://127.0.0.1:8000/v1"\n', encoding="utf-8")
    config.save_agent_selection("deepseek", "deepseek-chat", p)
    cfg = config.load_config(p, create=False)
    assert (cfg.agent.provider, cfg.agent.model) == ("deepseek", "deepseek-chat")
    assert "providers" in cfg.model_dump()


def test_save_agent_selection_creates_a_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "config.toml"
    config.save_agent_selection("deepseek", "deepseek-chat", p)
    cfg = config.load_config(p, create=False)
    assert (cfg.agent.provider, cfg.agent.model) == ("deepseek", "deepseek-chat")


def test_save_agent_selection_rejects_invalid_toml(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[agent\nprovider = ", encoding="utf-8")
    with pytest.raises(ConfigError):
        config.save_agent_selection("deepseek", "deepseek-chat", p)


def test_save_agent_selection_escapes_values(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[agent]\n", encoding="utf-8")
    config.save_agent_selection('my"provider', "model\\one", p)
    cfg = config.load_config(p, create=False)
    assert cfg.agent.provider == 'my"provider'
    assert cfg.agent.model == "model\\one"
