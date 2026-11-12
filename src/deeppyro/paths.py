"""User-level locations for DeepPyro.

DeepPyro keeps everything user-scoped under one directory, mirroring DSH's
.home directory: configuration, sessions, plugins and prompt history all live
in ~/.dpro by default. Environment variables can override the home or a
single path so portable installs and tests stay hermetic.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Directory name under the user home (DSH uses .dsh; DeepPyro uses .dpro).
HOME_DIR_NAME = ".dpro"

HOME_ENV = "DEEPPYRO_HOME"
CONFIG_ENV = "DEEPPYRO_CONFIG"
SESSIONS_ENV = "DEEPPYRO_SESSION_DIR"
HISTORY_ENV = "DEEPPYRO_HISTORY"
PLUGINS_MANIFEST_ENV = "DEEPPYRO_PLUGINS_MANIFEST"
PLUGINS_DIR_ENV = "DEEPPYRO_PLUGINS_DIR"


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def deeppyro_home() -> Path:
    """Root of user-scoped state; DEEPPYRO_HOME wins over ~/.dpro."""
    override = _env_path(HOME_ENV)
    return override if override is not None else Path.home() / HOME_DIR_NAME


def default_config_path() -> Path:
    return deeppyro_home() / "config.toml"


def config_path() -> Path:
    return _env_path(CONFIG_ENV) or default_config_path()


def default_sessions_dir() -> Path:
    return deeppyro_home() / "sessions"


def sessions_dir() -> Path:
    return _env_path(SESSIONS_ENV) or default_sessions_dir()


def default_plugins_manifest_path() -> Path:
    return deeppyro_home() / "plugins.toml"


def plugins_manifest_path() -> Path:
    return _env_path(PLUGINS_MANIFEST_ENV) or default_plugins_manifest_path()


def default_plugins_dir() -> Path:
    return deeppyro_home() / "plugins"


def plugins_dir() -> Path:
    return _env_path(PLUGINS_DIR_ENV) or default_plugins_dir()


def default_history_path() -> Path:
    return deeppyro_home() / "history.jsonl"


def history_path() -> Path:
    return _env_path(HISTORY_ENV) or default_history_path()


def ensure_home() -> Path:
    """Create the home directory if needed and return it."""
    home = deeppyro_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def _legacy_roots() -> list[Path]:
    roots: list[Path] = []
    for variable in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if not base:
            continue
        root = Path(base) / "deeppyro"
        if root not in roots:
            roots.append(root)
    return roots


def _first_file(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.is_file()), None)


def _first_dir(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.is_dir()), None)


def _copy_missing_file(source: Path, target: Path) -> bool:
    if not source.is_file() or target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _copy_missing_tree(source: Path, target: Path) -> int:
    copied = 0
    for path in sorted(source.rglob("*")):
        if path.is_file() and _copy_missing_file(path, target / path.relative_to(source)):
            copied += 1
    return copied


def migrate_legacy_layout() -> dict[str, int]:
    """Copy legacy platformdirs data into the DeepPyro home directory.

    Older builds stored config.toml and history.jsonl in
    %APPDATA%/deeppyro and sessions in %LOCALAPPDATA%/deeppyro/sessions (one
    intermediate build nested them under deeppyro/deeppyro). Migration copies
    only missing files, never overwrites, and leaves the old tree untouched so
    playback stays possible. A missing legacy tree is a no-op.
    """
    report = {"config": 0, "history": 0, "plugins_manifest": 0, "plugins": 0, "sessions": 0}
    roots = _legacy_roots()
    if not roots:
        return report
    config_sources = [root / "config.toml" for root in roots]
    config_sources += [root / "deeppyro" / "config.toml" for root in roots]
    history_sources = [root / "history.jsonl" for root in roots]
    manifest_sources = [root / "plugins.toml" for root in roots]
    plugin_dirs = [root / "plugins" for root in roots]
    session_dirs = [root / "sessions" for root in roots]
    legacy_exists = any(
        path.exists()
        for path in (*config_sources, *history_sources, *manifest_sources, *plugin_dirs, *session_dirs)
    )
    if not legacy_exists:
        return report
    config_override = _env_path(CONFIG_ENV)
    history_override = _env_path(HISTORY_ENV)
    manifest_override = _env_path(PLUGINS_MANIFEST_ENV)
    plugins_override = _env_path(PLUGINS_DIR_ENV)
    sessions_override = _env_path(SESSIONS_ENV)
    ensure_home()
    if config_override is None:
        source = _first_file(config_sources)
        if source is not None and _copy_missing_file(source, config_path()):
            report["config"] = 1
    if history_override is None:
        source = _first_file(history_sources)
        if source is not None and _copy_missing_file(source, history_path()):
            report["history"] = 1
    if manifest_override is None:
        source = _first_file(manifest_sources)
        if source is not None and _copy_missing_file(source, plugins_manifest_path()):
            report["plugins_manifest"] = 1
    if plugins_override is None:
        source = _first_dir(plugin_dirs)
        if source is not None:
            report["plugins"] = _copy_missing_tree(source, plugins_dir())
    if sessions_override is None:
        target = sessions_dir()
        for source in (path for path in session_dirs if path.is_dir()):
            target.mkdir(parents=True, exist_ok=True)
            report["sessions"] += _copy_missing_tree(source, target)
    return report


__all__ = [
    "CONFIG_ENV",
    "HISTORY_ENV",
    "HOME_DIR_NAME",
    "HOME_ENV",
    "PLUGINS_DIR_ENV",
    "PLUGINS_MANIFEST_ENV",
    "SESSIONS_ENV",
    "config_path",
    "deeppyro_home",
    "default_config_path",
    "default_history_path",
    "default_plugins_dir",
    "default_plugins_manifest_path",
    "default_sessions_dir",
    "ensure_home",
    "history_path",
    "migrate_legacy_layout",
    "plugins_dir",
    "plugins_manifest_path",
    "sessions_dir",
]
