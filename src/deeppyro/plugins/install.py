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

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro import paths
from deeppyro.kernel.composition import CompositionError, load_bundle
from deeppyro.plugins.bootstrap import PluginPackage, scan_plugin_packages
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
from deeppyro.plugins.install_requirements import (
    SourcePlan,
    normalise_name,
    parse_requirement,
    plan_source,
    validate_plugin_id,
    validate_version,
)
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
    _execute,
    _install_requirements,
    _uv_preflight_problem,
)
from deeppyro.plugins.install_vendor import requirement_errors, vendor_conflict_map, vendor_versions
from deeppyro.plugins.metadata import MetadataError, find_vendor_dir, load_metadata, python_tag

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


@dataclass(frozen=True, slots=True)
class RemoveReport:
    plugin_id: str
    dependents: tuple[str, ...]
    manifest_changed: bool


def _require_package(plugin_id: str, root: Path) -> PluginPackage:
    for package in scan_plugin_packages(root):
        if package.id == plugin_id:
            return package
    raise UnknownPlugin(plugin_id)


def dependents_of(plugin_id: str, *, root: Path | None = None) -> tuple[str, ...]:
    """Installed package ids that require ``plugin_id`` (deps or bundle requires)."""
    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    wanted = normalise_name(plugin_id)
    found: set[str] = set()
    for package in scan_plugin_packages(plugin_root):
        if package.id == plugin_id:
            continue
        if package.metadata is not None:
            for requirement in package.metadata.deps.requires:
                try:
                    name, _ = parse_requirement(requirement)
                except ValueError:
                    continue
                if normalise_name(name) == wanted:
                    found.add(package.id)
                    break
        path = package.directory / "bundle.toml"
        if not path.is_file():
            continue
        try:
            bundle = load_bundle(path)
        except (CompositionError, OSError):
            continue
        if any(requirement.id == plugin_id for requirement in bundle.requires):
            found.add(package.id)
    return tuple(sorted(found))


def _purge_caches(root: Path) -> None:
    try:
        caches = list(root.rglob("__pycache__"))
    except OSError:
        return
    for cache in caches:
        shutil.rmtree(cache, ignore_errors=True)


def _drop_manifest_row(plugin_id: str, path: Path) -> bool:
    """Delete one row from the user manifest; return whether the file changed."""
    from deeppyro.kernel.manifest import ManifestError
    from deeppyro.plugins.manifest_store import read_manifest, save_user_manifest

    try:
        specs = read_manifest(path)
    except (OSError, ManifestError):
        return False
    kept = [spec for spec in specs if spec.id != plugin_id]
    if len(kept) == len(specs):
        return False
    try:
        save_user_manifest(path, kept)
    except OSError:
        return False
    return True


def remove(
    plugin_id: str,
    *,
    root: Path | None = None,
    force: bool = False,
    manager: Any | None = None,
) -> RemoveReport:
    """Remove an installed package directory and its manifest entry."""
    from deeppyro.plugins.errors import PluginError, PluginNotFoundError

    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    manifest_path = Path(manager.manifest_path) if manager is not None else paths.plugins_manifest_path()
    try:
        package = _require_package(plugin_id, plugin_root)
    except UnknownPlugin:
        dropped = _drop_manifest_row(plugin_id, manifest_path)
        directory = plugin_root / plugin_id
        removed = False
        if validate_plugin_id(plugin_id) is None and directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
            removed = True
        if not dropped and not removed:
            raise
        _purge_caches(plugin_root)
        return RemoveReport(plugin_id, (), dropped)
    dependents = dependents_of(plugin_id, root=plugin_root)
    if dependents and not force:
        raise PreflightFailure(tuple(f"{dependent} depends on {plugin_id}" for dependent in dependents))
    managed = manager or _manager(plugin_root)
    before = _manifest_bytes(Path(managed.manifest_path))
    try:
        managed.remove(plugin_id)
    except PluginNotFoundError as exc:
        raise UnknownPlugin(plugin_id) from exc
    except PluginError as exc:
        raise InstallFailure(f"could not remove {plugin_id!r}: {exc}", rolled_back=True) from exc
    if package.directory.exists():
        shutil.rmtree(package.directory, ignore_errors=True)
    _purge_caches(plugin_root)
    after = _manifest_bytes(Path(managed.manifest_path))
    return RemoveReport(plugin_id, dependents, before != after)


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


@dataclass(frozen=True, slots=True)
class StateReport:
    plugin_id: str
    enabled: bool
    changed: bool


def set_enabled(
    plugin_id: str,
    enabled: bool,
    *,
    root: Path | None = None,
    manager: Any | None = None,
) -> StateReport:
    """Enable or disable an installed package via the existing manager."""
    from deeppyro.plugins.errors import PluginError, PluginNotFoundError

    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    managed = manager or _manager(plugin_root)
    before = _manifest_bytes(Path(managed.manifest_path))
    try:
        if enabled:
            managed.enable(plugin_id)
        else:
            managed.disable(plugin_id)
    except PluginNotFoundError as exc:
        raise UnknownPlugin(plugin_id) from exc
    except PluginError as exc:
        raise InstallFailure(f"could not update {plugin_id!r}: {exc}", rolled_back=True) from exc
    after = _manifest_bytes(Path(managed.manifest_path))
    return StateReport(plugin_id, enabled, before != after)


@dataclass(frozen=True, slots=True)
class AllowVersionReport:
    plugin_id: str
    version: str
    changed: bool


_EXEMPT_KEYS = ("dpyro_exempt_versions", "exempt_versions")

_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*=\s*(.*)$")


def _table_header(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[[") or not stripped.endswith("]"):
        return None
    return stripped[1:-1].strip()


def _compat_bounds(lines: list[str]) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        name = _table_header(line)
        if name is None:
            continue
        if start is not None:
            return start, index
        if name == "compat":
            start = index
    if start is None:
        return None
    return start, len(lines)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _quote(value: str, *, single: bool) -> str:
    return f"'{value}'" if single else json.dumps(value)


def _array_text(values: Iterable[str], *, indent: str, single: bool, multiline: bool) -> str:
    rendered = [_quote(value, single=single) for value in values]
    if not multiline:
        return "[" + ", ".join(rendered) + "]"
    body = "".join(f"{indent}    {item},\n" for item in rendered)
    return "[\n" + body + indent + "]"


def _bracket_depth(block: Iterable[str]) -> int:
    text = " ".join(part.strip() for part in block)
    return text.count("[") - text.count("]")


def _array_values(block: Iterable[str]) -> list[str]:
    text = " ".join(part.strip() for part in block)
    start = text.find("[")
    end = text.rfind("]")
    inner = text[start + 1 : end] if start >= 0 and end > start else ""
    return re.findall(r"""["']([^"']+)["']""", inner)


def _uses_single_quotes(block: Iterable[str]) -> bool:
    text = " ".join(block)
    return text.count("'") > text.count('"')


def _section_indent(lines: list[str], start: int, end: int) -> str:
    for index in range(start + 1, end):
        match = _KEY_RE.match(lines[index])
        if match is not None and match.group(1):
            return match.group(1)
    return ""


def with_exempt_version(text: str, version: str) -> str:
    """Add ``version`` to ``[compat].dpyro_exempt_versions`` keeping the style."""
    newline = _newline(text)
    had_trailing = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    bounds = _compat_bounds(lines)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(("[compat]", f"dpyro_exempt_versions = {json.dumps([version])}"))
        return newline.join(lines) + (newline if had_trailing else "")
    start, end = bounds
    key_index: int | None = None
    for index in range(start + 1, end):
        match = _KEY_RE.match(lines[index])
        if match is not None and match.group(2) in _EXEMPT_KEYS:
            key_index = index
            break
    if key_index is None:
        indent = _section_indent(lines, start, end)
        lines.insert(start + 1, f"{indent}dpyro_exempt_versions = {json.dumps([version])}")
        return newline.join(lines) + (newline if had_trailing else "")
    match = _KEY_RE.match(lines[key_index])
    assert match is not None
    indent, key, raw = match.group(1), match.group(2), match.group(3)
    block = [raw]
    consumed = 1
    while _bracket_depth(block) > 0 and key_index + consumed < end:
        block.append(lines[key_index + consumed])
        consumed += 1
    values = _array_values(block)
    if version in values:
        return text
    values.append(version)
    multiline = consumed > 1
    rendered = _array_text(values, indent=indent, single=_uses_single_quotes(block), multiline=multiline)
    replacement = f"{indent}{key} = {rendered}"
    lines[key_index : key_index + consumed] = replacement.splitlines()
    return newline.join(lines) + (newline if had_trailing else "")


def _plugin_toml(plugin_id: str, root: Path) -> Path:
    path = root / plugin_id / "plugin.toml"
    if not path.is_file():
        raise UnknownPlugin(plugin_id)
    return path


def allow_version(plugin_id: str, version: str, *, root: Path | None = None) -> AllowVersionReport:
    """Persist one exact version in ``[compat].dpyro_exempt_versions``."""
    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    toml_path = _plugin_toml(plugin_id, plugin_root)
    problem = validate_version(version)
    if problem is not None:
        raise PreflightFailure((problem,))
    original = toml_path.read_text(encoding="utf-8")
    updated = with_exempt_version(original, version)
    if updated == original:
        return AllowVersionReport(plugin_id, version, False)
    backup = toml_path.with_name("plugin.toml.bak")
    try:
        backup.write_text(original, encoding="utf-8")
        toml_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        raise InstallFailure(f"could not write {toml_path}: {exc}", rolled_back=False) from exc
    try:
        load_metadata(toml_path, plugin_id=plugin_id)
    except MetadataError as exc:
        with contextlib.suppress(OSError):
            toml_path.write_text(original, encoding="utf-8")
        raise PreflightFailure((f"{exc.kind}: {exc.message}",)) from exc
    return AllowVersionReport(plugin_id, version, True)
