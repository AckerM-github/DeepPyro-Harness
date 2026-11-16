"""Install, remove and update plugin packages with a staged uv backend.

The command surface lives in deeppyro.plugins.cli; this module is the
argument-free core so the same operations are usable from the TUI, tests and
future automation.

Every mutating operation is staged:

1. preflight parses the package metadata, checks compatibility, ids, versions,
   the target directory, declared dependencies and cross-package vendor
   conflicts. A failing preflight raises PreflightFailure and leaves the plugin
   directory and the user manifest untouched.
2. install builds the package (and its _vendor tree) in a temporary directory
   inside the plugin root, then swaps it into place with a single os.replace.
3. record writes the id into the user manifest through the plugin manager; any
   failure rolls the directory swap back.

The implementation is split across the private install_* modules in this
package; the names exported below keep their original import paths.

uv is never imported: it runs as an external process behind an injectable
runner callable, so the test suite works without network access or a uv binary.
DPYRO_UV overrides the executable path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from deeppyro import paths
from deeppyro.plugins.install_commit import (
    InstallReport,
    _commit,
    _manager,
    _manifest_bytes,
    _record,
    _restore_manifest,
    _rollback_commit,
)
from deeppyro.plugins.install_errors import InstallerError, InstallFailure, PreflightFailure, UnknownPlugin
from deeppyro.plugins.install_exempt import AllowVersionReport, allow_version, with_exempt_version
from deeppyro.plugins.install_importcheck import target_errors
from deeppyro.plugins.install_manifest import FAILURE_MANIFEST_DANGLING, MANIFEST_REMEDIATION, manifest_issues
from deeppyro.plugins.install_payload import _load_payload_metadata, _materialise
from deeppyro.plugins.install_preflight import (
    CUSTOMIZED_CORE,
    _installed_conflicts,
    _preflight_problems,
    bundle_row_ids,
    core_row_ids,
    customized_core,
)
from deeppyro.plugins.install_removal import RemoveReport, dependents_of, remove
from deeppyro.plugins.install_requirements import (
    SourcePlan,
    normalise_name,
    parse_requirement,
    plan_source,
    validate_plugin_id,
    validate_version,
)
from deeppyro.plugins.install_state import StateReport, set_enabled
from deeppyro.plugins.install_update import UpdateReport, VendorChange, update, vendor_diff
from deeppyro.plugins.install_uv import (
    REQUIREMENTS_FILE,
    STDERR_TAIL_CHARS,
    STDERR_TAIL_LINES,
    STDOUT_TAIL_CHARS,
    STDOUT_TAIL_LINES,
    UV_ENV,
    UV_TIMEOUT,
    Runner,
    UvCommand,
    UvResult,
    _install_requirements,
    _uv_preflight_problem,
)
from deeppyro.plugins.install_vendor import requirement_errors, vendor_conflict_map, vendor_versions
from deeppyro.plugins.metadata import MetadataError, find_vendor_dir, python_tag

__all__ = [
    "CUSTOMIZED_CORE",
    "FAILURE_MANIFEST_DANGLING",
    "MANIFEST_REMEDIATION",
    "REQUIREMENTS_FILE",
    "STDERR_TAIL_CHARS",
    "STDERR_TAIL_LINES",
    "STDOUT_TAIL_CHARS",
    "STDOUT_TAIL_LINES",
    "UV_ENV",
    "UV_TIMEOUT",
    "AllowVersionReport",
    "InstallFailure",
    "InstallReport",
    "InstallerError",
    "MetadataError",
    "PreflightFailure",
    "RemoveReport",
    "Runner",
    "SourcePlan",
    "StateReport",
    "UnknownPlugin",
    "UpdateReport",
    "UvCommand",
    "UvResult",
    "VendorChange",
    "add",
    "allow_version",
    "bundle_row_ids",
    "core_row_ids",
    "customized_core",
    "dependents_of",
    "manifest_issues",
    "normalise_name",
    "parse_requirement",
    "plan_source",
    "remove",
    "requirement_errors",
    "run_uv",
    "set_enabled",
    "target_errors",
    "update",
    "uv_available",
    "uv_executable",
    "validate_plugin_id",
    "validate_version",
    "vendor_conflict_map",
    "vendor_diff",
    "vendor_versions",
    "with_exempt_version",
]


def _tail(text: str | bytes | None, *, lines: int, chars: int) -> str:
    """Last ``lines`` lines of ``text``, capped at ``chars`` characters."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not text:
        return ""
    trimmed = "\n".join(text.splitlines()[-lines:])
    return trimmed[-chars:]


def uv_executable() -> str:
    """Resolve the uv executable, honouring ``DPYRO_UV``."""
    override = os.environ.get(UV_ENV, "").strip()
    if override:
        return override
    found = shutil.which("uv")
    if not found:
        raise InstallerError(f"uv executable not found; install uv or set {UV_ENV}")
    return found


def uv_available() -> bool:
    """Whether a uv executable can be resolved without running it."""
    try:
        uv_executable()
    except InstallerError:
        return False
    return True


def run_uv(
    command: UvCommand,
    *,
    timeout: float | None = None,
    executable: str | None = None,
) -> UvResult:
    """Run one uv command, capturing output and never raising."""
    exe = executable or uv_executable()
    limit = UV_TIMEOUT if timeout is None else timeout
    argv = [exe, *command.args]
    cwd = str(command.cwd) if command.cwd is not None else None
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limit,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = _tail(exc.stderr, lines=STDERR_TAIL_LINES, chars=STDERR_TAIL_CHARS)
        message = f"uv timed out after {limit:g}s"
        if stderr:
            message = f"{message}\n{stderr}"
        return UvResult(
            124, _tail(exc.stdout, lines=STDOUT_TAIL_LINES, chars=STDOUT_TAIL_CHARS), message, True
        )
    except OSError as exc:
        return UvResult(127, "", f"cannot run {exe}: {exc}", False)
    return UvResult(
        completed.returncode,
        _tail(completed.stdout, lines=STDOUT_TAIL_LINES, chars=STDOUT_TAIL_CHARS),
        _tail(completed.stderr, lines=STDERR_TAIL_LINES, chars=STDERR_TAIL_CHARS),
        False,
    )


def add(
    source: str | Path,
    *,
    root: Path | None = None,
    force: bool = False,
    no_deps: bool = False,
    offline: bool = False,
    runner: Runner | None = None,
    manager: Any | None = None,
    timeout: float | None = None,
) -> InstallReport:
    """Install one plugin package (directory, archive, registry spec or git URL).

    Raises :class:`PreflightFailure` (nothing installed), :class:`InstallFailure`
    (rolled back) or :class:`InstallerError` for uv/environment problems.
    """
    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    plan = plan_source(source)
    plugin_root.mkdir(parents=True, exist_ok=True)
    if not os.access(plugin_root, os.W_OK):
        raise PreflightFailure((f"{plugin_root} is not writable",))
    if plan.kind in ("registry", "git"):
        problem = _uv_preflight_problem(runner)
        if problem is not None:
            raise PreflightFailure((problem,))
    staging = Path(tempfile.mkdtemp(prefix=".dpyro-install-", dir=plugin_root))
    try:
        payload = _materialise(plan, staging, runner=runner, offline=offline, timeout=timeout)
        metadata = _load_payload_metadata(payload, plan)
        managed = manager or _manager(plugin_root)
        problems = _preflight_problems(metadata, plan, plugin_root, force=force, manager=managed)
        for error in target_errors(metadata, payload, defer_missing=not no_deps):
            problems.append(f"{error.kind}: {error.message}")
        if problems:
            raise PreflightFailure(problems)
        vendor_root = payload / metadata.deps.vendor / python_tag()
        warnings: list[str] = []
        if metadata.deps.requires:
            if no_deps:
                warnings.append("declared dependencies were not installed (--no-deps)")
            else:
                problem = _uv_preflight_problem(runner)
                if problem is not None:
                    raise PreflightFailure((problem,))
                _install_requirements(
                    metadata.deps.requires,
                    vendor_root,
                    runner=runner,
                    offline=offline,
                    timeout=timeout,
                )
                installed_errors = target_errors(metadata, payload)
                if installed_errors:
                    detail = "; ".join(f"{error.kind}: {error.message}" for error in installed_errors)
                    raise InstallFailure(f"installed package failed validation: {detail}")
                conflicts = _installed_conflicts(metadata.id, vendor_root, plugin_root)
                if conflicts:
                    raise PreflightFailure(tuple(f"{error.kind}: {error.message}" for error in conflicts))
        target = plugin_root / metadata.id
        backup = _commit(payload, target, force=force)
        replaced = backup is not None
        manifest_before = _manifest_bytes(Path(managed.manifest_path))
        try:
            installed_errors = target_errors(metadata, target)
            if installed_errors:
                detail = "; ".join(f"{error.kind}: {error.message}" for error in installed_errors)
                raise InstallFailure(f"installed package failed validation: {detail}")
        except InstallFailure:
            _rollback_commit(target, backup)
            _restore_manifest(Path(managed.manifest_path), manifest_before)
            raise
        try:
            manifest_changed = _record(
                metadata.id,
                target,
                find_vendor_dir(metadata.with_directory(target)),
                manager=managed,
            )
        except Exception as exc:
            _rollback_commit(target, backup)
            _restore_manifest(Path(managed.manifest_path), manifest_before)
            raise InstallFailure(f"could not record {metadata.id!r}: {exc}") from exc
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
        installed_vendor = find_vendor_dir(metadata.with_directory(target))
        return InstallReport(
            plugin_id=metadata.id,
            version=metadata.version,
            directory=target,
            replaced=replaced,
            manifest_changed=manifest_changed,
            vendor=vendor_versions(installed_vendor),
            warnings=tuple(warnings),
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
