"""User-home path resolution and legacy layout migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro import paths

_OVERRIDES = (
    paths.HOME_ENV,
    paths.CONFIG_ENV,
    paths.SESSIONS_ENV,
    paths.HISTORY_ENV,
    paths.PLUGINS_MANIFEST_ENV,
    paths.PLUGINS_DIR_ENV,
)


def _clear_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OVERRIDES:
        monkeypatch.delenv(name, raising=False)


def _use_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv(paths.HOME_ENV, str(home))
    return home


def test_default_home_is_dot_dpro(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert paths.deeppyro_home() == tmp_path / ".dpro"
    assert paths.config_path() == tmp_path / ".dpro" / "config.toml"
    assert paths.sessions_dir() == tmp_path / ".dpro" / "sessions"
    assert paths.plugins_manifest_path() == tmp_path / ".dpro" / "plugins.toml"
    assert paths.plugins_dir() == tmp_path / ".dpro" / "plugins"
    assert paths.history_path() == tmp_path / ".dpro" / "history.jsonl"


def test_home_env_moves_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    home = _use_home(monkeypatch, tmp_path)
    assert paths.deeppyro_home() == home
    assert paths.config_path() == home / "config.toml"
    assert paths.sessions_dir() == home / "sessions"
    assert paths.plugins_manifest_path() == home / "plugins.toml"
    assert paths.plugins_dir() == home / "plugins"
    assert paths.history_path() == home / "history.jsonl"


def test_individual_path_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    _use_home(monkeypatch, tmp_path)
    monkeypatch.setenv(paths.CONFIG_ENV, str(tmp_path / "c.toml"))
    monkeypatch.setenv(paths.SESSIONS_ENV, str(tmp_path / "s"))
    monkeypatch.setenv(paths.HISTORY_ENV, str(tmp_path / "h.jsonl"))
    monkeypatch.setenv(paths.PLUGINS_MANIFEST_ENV, str(tmp_path / "p.toml"))
    monkeypatch.setenv(paths.PLUGINS_DIR_ENV, str(tmp_path / "pdir"))
    assert paths.config_path() == tmp_path / "c.toml"
    assert paths.sessions_dir() == tmp_path / "s"
    assert paths.history_path() == tmp_path / "h.jsonl"
    assert paths.plugins_manifest_path() == tmp_path / "p.toml"
    assert paths.plugins_dir() == tmp_path / "pdir"


def _legacy_tree(tmp_path: Path) -> tuple[Path, Path]:
    appdata = tmp_path / "appdata"
    localappdata = tmp_path / "localappdata"
    roaming = appdata / "deeppyro"
    local = localappdata / "deeppyro"
    roaming.mkdir(parents=True)
    (roaming / "config.toml").write_text("[agent]\nprovider = 'legacy'\n", encoding="utf-8")
    (roaming / "history.jsonl").write_text('{"prompt": "old"}\n', encoding="utf-8")
    (roaming / "plugins.toml").write_text("[[plugins]]\nid = 'hello'\n", encoding="utf-8")
    plugin = roaming / "plugins" / "hello"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text("id = 'hello'\n", encoding="utf-8")
    sessions = local / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session-a.jsonl").write_text("{}\n", encoding="utf-8")
    (sessions / "session-b.jsonl").write_text("{}\n", encoding="utf-8")
    return appdata, localappdata


def test_migrate_legacy_layout_copies_missing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    home = _use_home(monkeypatch, tmp_path)
    appdata, localappdata = _legacy_tree(tmp_path)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    report = paths.migrate_legacy_layout()
    assert report == {
        "config": 1,
        "history": 1,
        "plugins_manifest": 1,
        "plugins": 1,
        "sessions": 2,
    }
    assert (home / "config.toml").read_text("utf-8") == "[agent]\nprovider = 'legacy'\n"
    assert (home / "history.jsonl").exists()
    assert (home / "plugins.toml").exists()
    assert (home / "plugins" / "hello" / "plugin.toml").exists()
    assert sorted(path.name for path in (home / "sessions").iterdir()) == [
        "session-a.jsonl",
        "session-b.jsonl",
    ]
    assert paths.migrate_legacy_layout() == {
        "config": 0,
        "history": 0,
        "plugins_manifest": 0,
        "plugins": 0,
        "sessions": 0,
    }


def test_migration_never_overwrites_user_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    home = _use_home(monkeypatch, tmp_path)
    appdata, localappdata = _legacy_tree(tmp_path)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    paths.migrate_legacy_layout()
    (home / "config.toml").write_text("user edited\n", encoding="utf-8")
    (home / "sessions" / "session-a.jsonl").write_text("kept\n", encoding="utf-8")
    report = paths.migrate_legacy_layout()
    assert report["config"] == 0
    assert report["sessions"] == 0
    assert (home / "config.toml").read_text("utf-8") == "user edited\n"
    assert (home / "sessions" / "session-a.jsonl").read_text("utf-8") == "kept\n"


def test_migration_finds_nested_local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    home = _use_home(monkeypatch, tmp_path)
    appdata = tmp_path / "appdata"
    localappdata = tmp_path / "localappdata"
    nested = localappdata / "deeppyro" / "deeppyro"
    nested.mkdir(parents=True)
    (nested / "config.toml").write_text("nested\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    report = paths.migrate_legacy_layout()
    assert report["config"] == 1
    assert (home / "config.toml").read_text("utf-8") == "nested\n"


def test_migration_is_a_noop_without_legacy_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    home = _use_home(monkeypatch, tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-localappdata"))
    report = paths.migrate_legacy_layout()
    assert report == {
        "config": 0,
        "history": 0,
        "plugins_manifest": 0,
        "plugins": 0,
        "sessions": 0,
    }
    assert not home.exists()


def test_migration_respects_single_path_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    home = _use_home(monkeypatch, tmp_path)
    appdata, localappdata = _legacy_tree(tmp_path)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    custom_config = tmp_path / "custom" / "config.toml"
    monkeypatch.setenv(paths.CONFIG_ENV, str(custom_config))
    report = paths.migrate_legacy_layout()
    assert report["config"] == 0
    assert not (home / "config.toml").exists()
    assert not custom_config.exists()
    assert report["history"] == 1


def test_ensure_home_creates_the_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_overrides(monkeypatch)
    home = _use_home(monkeypatch, tmp_path)
    assert paths.ensure_home() == home
    assert home.is_dir()
