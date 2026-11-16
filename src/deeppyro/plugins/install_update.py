"""Rebuilding a package's vendor tree and reporting version changes."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro import paths
from deeppyro.plugins.install_errors import InstallFailure, PreflightFailure
from deeppyro.plugins.install_removal import _require_package
from deeppyro.plugins.install_uv import (
    REQUIREMENTS_FILE,
    Runner,
    UvCommand,
    UvResult,
    _execute,
    _install_requirements,
    _uv_preflight_problem,
)
from deeppyro.plugins.install_vendor import vendor_versions
from deeppyro.plugins.metadata import find_vendor_dir, python_tag


@dataclass(frozen=True, slots=True)
class VendorChange:
    name: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class UpdateReport:
    plugin_id: str
    rebuilt: bool
    changes: tuple[VendorChange, ...]
    warnings: tuple[str, ...] = ()


def vendor_diff(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[VendorChange, ...]:
    """Package-name/version changes between two vendor snapshots."""
    names = sorted(set(before) | set(after))
    return tuple(
        VendorChange(name, before.get(name, ""), after.get(name, ""))
        for name in names
        if before.get(name, "") != after.get(name, "")
    )


def _run_step(
    args: Iterable[str],
    cwd: Path,
    runner: Runner | None,
    timeout: float | None,
) -> UvResult:
    command = UvCommand(tuple(args), cwd=cwd)
    result = _execute(runner, command, timeout)
    if not result.ok:
        detail = result.stderr.strip()
        message = f"{command.display()} failed (exit {result.returncode})"
        if detail:
            message = f"{message}: {detail}"
        raise InstallFailure(message, stderr_tail=result.stderr)
    return result


def _pip_install_file(
    requirement_file: Path,
    target: Path,
    *,
    runner: Runner | None,
    offline: bool,
    timeout: float | None,
) -> None:
    command = (
        "pip",
        "install",
        "--python",
        sys.executable,
        "--target",
        str(target),
        "-r",
        str(requirement_file),
    )
    _run_step(command + (("--offline",) if offline else ()), target.parent, runner, timeout)


def _restore_lock(path: Path, data: bytes | None) -> None:
    with contextlib.suppress(OSError):
        if data is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(data)


def _swap_directory(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.parent / f".dpyro-backup-{destination.name}-{os.getpid()}-{id(source)}"
    had_destination = destination.exists()
    if had_destination:
        os.replace(destination, backup)
    try:
        os.replace(source, destination)
    except OSError as exc:
        rolled_back = True
        if had_destination:
            try:
                os.replace(backup, destination)
            except OSError:
                rolled_back = False
        raise InstallFailure(f"could not replace {destination}: {exc}", rolled_back=rolled_back) from exc
    if had_destination and backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


def update(
    plugin_id: str,
    *,
    root: Path | None = None,
    offline: bool = False,
    runner: Runner | None = None,
    manager: Any | None = None,
    timeout: float | None = None,
) -> UpdateReport:
    """Rebuild one package's ``_vendor`` tree and report the version diff."""
    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    package = _require_package(plugin_id, plugin_root)
    metadata = package.metadata
    if metadata is None or package.errors:
        problems = [f"{error.kind}: {error.message}" for error in package.errors]
        raise PreflightFailure(problems or (f"metadata-invalid: {plugin_id!r} could not be loaded",))
    if runner is None:
        problem = _uv_preflight_problem(runner)
        if problem is not None:
            raise PreflightFailure((problem,))
    directory = package.directory
    existing = find_vendor_dir(metadata.with_directory(directory))
    before = vendor_versions(existing)
    lock_mode = (directory / "pyproject.toml").is_file()
    warnings: list[str] = []
    staging = Path(tempfile.mkdtemp(prefix=".dpyro-update-", dir=plugin_root))
    new_vendor = staging / "vendor"
    lock_path = directory / "uv.lock"
    lock_before = lock_path.read_bytes() if lock_path.is_file() else None
    try:
        rebuilt = False
        try:
            if lock_mode:
                offline_flag = ("--offline",) if offline else ()
                _run_step(("lock", "--upgrade", *offline_flag), directory, runner, timeout)
                requirement_file = staging / REQUIREMENTS_FILE
                _run_step(
                    (
                        "export",
                        "--format",
                        "requirements-txt",
                        "--no-hashes",
                        "--output-file",
                        str(requirement_file),
                        *offline_flag,
                    ),
                    directory,
                    runner,
                    timeout,
                )
                new_vendor.mkdir()
                _pip_install_file(
                    requirement_file,
                    new_vendor,
                    runner=runner,
                    offline=offline,
                    timeout=timeout,
                )
                rebuilt = True
            elif metadata.deps.requires:
                _install_requirements(
                    metadata.deps.requires,
                    new_vendor,
                    runner=runner,
                    offline=offline,
                    timeout=timeout,
                )
                rebuilt = True
            else:
                warnings.append("package declares no dependencies and no pyproject.toml; nothing to rebuild")
        except Exception:
            _restore_lock(lock_path, lock_before)
            raise
        after = vendor_versions(new_vendor)
        changes = vendor_diff(before, after)
        if new_vendor.exists() and (before or any(new_vendor.iterdir())):
            destination = existing or (directory / metadata.deps.vendor / python_tag())
            _swap_directory(new_vendor, destination)
            after = vendor_versions(destination)
            changes = vendor_diff(before, after)
        if manager is not None:
            manager.refresh()
        return UpdateReport(plugin_id, rebuilt, changes, tuple(warnings))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
