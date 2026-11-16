"""Install commit/rollback primitives and the install report."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro.plugins.install_errors import InstallFailure, PreflightFailure


@dataclass(frozen=True, slots=True)
class InstallReport:
    """Outcome of a successful :func:`add`."""

    plugin_id: str
    version: str
    directory: Path
    replaced: bool
    manifest_changed: bool
    vendor: dict[str, str]
    warnings: tuple[str, ...] = ()


def _manifest_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def _restore_manifest(path: Path, data: bytes) -> None:
    # Rollback is best effort; the directory swap already happened.
    with contextlib.suppress(OSError):
        if data:
            path.write_bytes(data)
        elif path.exists():
            path.unlink()


def _manager(root: Path) -> Any:
    from deeppyro.plugins.manager import PluginManager

    return PluginManager(plugins_dir=root)


def _record(plugin_id: str, directory: Path, vendor: Path | None, *, manager: Any) -> bool:
    entries = [vendor, directory]
    added: list[str] = []
    for candidate in entries:
        if candidate is None:
            continue
        text = str(candidate)
        if text not in sys.path:
            sys.path.insert(0, text)
            added.append(text)
    before = _manifest_bytes(Path(manager.manifest_path))
    try:
        manager.enable(plugin_id)
    finally:
        for text in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(text)
    after = _manifest_bytes(Path(manager.manifest_path))
    return before != after


def _commit(payload: Path, target: Path, *, force: bool) -> Path | None:
    backup: Path | None = None
    if target.exists():
        if not force:
            raise PreflightFailure((f"{target} already exists (use --force to replace it)",))
        backup = target.parent.parent / f".dpyro-backup-{target.name}-{os.getpid()}-{id(payload)}"
        os.replace(target, backup)
    try:
        os.replace(payload, target)
    except OSError as exc:
        rolled_back = True
        if backup is not None:
            try:
                os.replace(backup, target)
            except OSError:
                rolled_back = False
        raise InstallFailure(f"could not install {target}: {exc}", rolled_back=rolled_back) from exc
    return backup


def _rollback_commit(target: Path, backup: Path | None) -> None:
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    if backup is not None and backup.exists():
        os.replace(backup, target)
