"""Install, remove and update plugin packages with a staged uv backend.

The command surface lives in :mod:`deeppyro.plugins.cli`; this module is the
argument-free core so the same operations are usable from the TUI, tests and
future automation.

Every mutating operation is staged:

1. **preflight** parses the package metadata, checks compatibility, ids,
   versions, the target directory, declared dependencies and cross-package
   vendor conflicts. A failing preflight raises :class:`PreflightFailure` and
   leaves the plugin directory and the user manifest untouched.
2. **install** builds the package (and its ``_vendor`` tree) in a temporary
   directory inside the plugin root, then swaps it into place with a single
   :func:`os.replace`.
3. **record** writes the id into the user manifest through
   :class:`~deeppyro.plugins.manager.PluginManager`; any failure rolls the
   directory swap back.

uv is never imported: it runs as an external process behind an injectable
``runner`` callable, so the test suite works without network access or a uv
binary. ``DPYRO_UV`` overrides the executable path.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from deeppyro import paths
from deeppyro.kernel.composition import (
    CompositionError,
    core_bundle_path,
    default_composition,
    load_bundle,
)
from deeppyro.plugins.bootstrap import PluginPackage, scan_plugin_packages
from deeppyro.plugins.metadata import (
    FAILURE_DEP_CONFLICT,
    FAILURE_DEP_MISSING,
    FAILURE_IMPORT,
    FAILURE_METADATA,
    REMEDIATION,
    MetadataError,
    PluginMetadata,
    check_compat,
    find_vendor_dir,
    load_metadata,
    parse_toml,
    python_tag,
    satisfies,
)

#: Environment variable that overrides the uv executable.
UV_ENV = "DPYRO_UV"
#: Upper bound for one uv invocation, in seconds.
UV_TIMEOUT = 600.0
#: Only the tail of a failing uv process is kept (avoids unbounded logs).
STDERR_TAIL_LINES = 200
STDERR_TAIL_CHARS = 20_000
STDOUT_TAIL_LINES = 400
STDOUT_TAIL_CHARS = 40_000

#: Explicit requirement list written for ``uv pip install -r``.
REQUIREMENTS_FILE = "requirements.txt"

#: Advisory finding (not a failure kind): a package bundle overrides core rows.
CUSTOMIZED_CORE = "customized-core"

#: A user-manifest row whose package directory is missing or cannot be imported.
FAILURE_MANIFEST_DANGLING = "manifest-dangling"

MANIFEST_REMEDIATION = (
    "remove the row from the user manifest or reinstall the package with 'dpyro plugin add <id>'"
)

#: A uv runner is injected in tests; ``None`` means the real subprocess runner.
Runner = Callable[["UvCommand"], "UvResult"]


class InstallerError(Exception):
    """Base class for installer failures."""


class PreflightFailure(InstallerError):
    """Preflight rejected the change; nothing outside staging was touched."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(dict.fromkeys(str(problem) for problem in problems))
        super().__init__("; ".join(self.problems) or "preflight failed")


class InstallFailure(InstallerError):
    """The install phase failed; ``rolled_back`` says whether the tree is clean."""

    def __init__(self, message: str, *, rolled_back: bool = True, stderr_tail: str = "") -> None:
        self.rolled_back = rolled_back
        self.stderr_tail = stderr_tail
        super().__init__(message)


class UnknownPlugin(InstallerError):
    """The requested plugin id is not an installed package directory."""


# -- uv process runner ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class UvCommand:
    """One uv invocation, without the executable (``DPYRO_UV`` supplies it)."""

    args: tuple[str, ...]
    cwd: Path | None = None

    def display(self) -> str:
        prefix = f"cd {self.cwd} && " if self.cwd is not None else ""
        return prefix + "uv " + " ".join(self.args)


@dataclass(frozen=True, slots=True)
class UvResult:
    """Captured outcome of one uv invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


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


def _execute(runner: Runner | None, command: UvCommand, timeout: float | None) -> UvResult:
    if runner is not None:
        return runner(command)
    return run_uv(command, timeout=timeout)


def _uv_preflight_problem(runner: Runner | None) -> str | None:
    """Preflight: registry/git and dependency installs need a uv executable."""
    if runner is not None:
        return None
    try:
        uv_executable()
    except InstallerError as exc:
        return str(exc)
    return None


# -- requirement helpers --------------------------------------------------


def normalise_name(name: str) -> str:
    """PEP 503-ish normalisation used for vendor package comparisons."""
    return name.strip().lower().replace("-", "_").replace(".", "_")


_REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$", re.DOTALL)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.!+_-]*$")


def parse_requirement(requirement: str) -> tuple[str, str]:
    """Split ``"websockets>=15"`` into ``("websockets", ">=15")``.

    Extras and environment markers are tolerated; ``~=`` and other specs the
    metadata helper cannot evaluate raise :class:`ValueError`.
    """
    text = requirement.strip()
    if not text:
        raise ValueError("empty requirement")
    match = _REQUIREMENT_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid requirement {requirement!r}")
    name, rest = match.group(1), match.group(2).strip()
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            raise ValueError(f"invalid extras in {requirement!r}")
        rest = rest[end + 1 :].strip()
    if ";" in rest:
        rest = rest.split(";", 1)[0].strip()
    if rest.startswith(("@", "(")):
        # Direct URL references carry no evaluable version spec.
        return name, ""
    if rest:
        satisfies("0.0.0", rest)  # raises ValueError for unsupported specs
    return name, rest


def validate_plugin_id(plugin_id: str) -> str | None:
    """Return a problem message, or ``None`` when the id is a safe dir name."""
    if not plugin_id:
        return "plugin id must not be empty"
    if plugin_id in (".", ".."):
        return f"plugin id {plugin_id!r} is not a usable directory name"
    if not _ID_RE.fullmatch(plugin_id):
        return f"plugin id {plugin_id!r} must match [A-Za-z0-9][A-Za-z0-9._-]*"
    return None


def validate_version(version: str) -> str | None:
    """Return a problem message, or ``None`` for a plausible version string."""
    if not version:
        return "package version is empty"
    if not _VERSION_RE.fullmatch(version):
        return f"package version {version!r} is not a valid version"
    return None


# -- source planning ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcePlan:
    """How a ``add`` source is materialised."""

    kind: str
    source: str
    path: Path | None = None
    spec: str = ""
    expected_id: str | None = None


def _looks_like_url(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith(("git+", "http://", "https://", "ssh://", "git://", "file://", "git@"))


def _looks_like_path(text: str) -> bool:
    if text.startswith((".", "~", "/", "\\")):
        return True
    if "/" in text or "\\" in text:
        return True
    if len(text) > 1 and text[1] == ":":
        return True
    return text.lower().endswith((".whl", ".zip", ".tar.gz", ".tgz", ".tar"))


def _archive_id(path: Path) -> str:
    name = path.name
    for suffix in (".tar.gz", ".tgz", ".whl", ".zip", ".tar"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    head = re.split(r"[-_](?=\d)", name)[0]
    return head.strip("._-") or path.stem


def _url_id(text: str) -> str:
    cleaned = text.strip().rstrip("/")
    cleaned = cleaned.removesuffix(".git")
    tail = cleaned.replace("\\", "/").rsplit("/", 1)[-1]
    tail = tail.rsplit(":", 1)[-1] if "://" not in cleaned else tail
    return re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip(".-") or "plugin"


def plan_source(source: str | Path) -> SourcePlan:
    """Classify ``add`` input and validate the cheap, static constraints."""
    text = str(source).strip()
    if not text:
        raise PreflightFailure(("source must not be empty",))
    candidate = Path(os.path.expanduser(text))
    if candidate.exists():
        if candidate.is_dir():
            if not (candidate / "plugin.toml").is_file():
                raise PreflightFailure((f"metadata-invalid: {candidate} does not contain a plugin.toml",))
            problem = validate_plugin_id(candidate.name)
            if problem is not None:
                raise PreflightFailure((f"metadata-invalid: {problem}",))
            return SourcePlan("directory", text, path=candidate.resolve(), expected_id=candidate.name)
        if candidate.is_file():
            if not _looks_like_path(candidate.name):
                raise PreflightFailure((f"unsupported package archive {candidate}",))
            return SourcePlan("archive", text, path=candidate.resolve(), expected_id=_archive_id(candidate))
        raise PreflightFailure((f"unsupported package source {candidate}",))
    if _looks_like_url(text):
        return SourcePlan("git", text, spec=text, expected_id=_url_id(text))
    if _looks_like_path(text):
        raise PreflightFailure((f"{text!r} does not exist",))
    try:
        name, _ = parse_requirement(text)
    except ValueError as exc:
        raise PreflightFailure((f"invalid requirement {text!r}: {exc}",)) from exc
    return SourcePlan("registry", text, spec=text, expected_id=name)


# -- vendor snapshots -----------------------------------------------------


def _dist_headers(path: Path) -> dict[str, str]:
    headers: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return headers
    for line in text.splitlines():
        if not line.strip():
            break
        key, separator, value = line.partition(":")
        if not separator:
            continue
        lowered = key.strip().lower()
        if lowered in ("name", "version") and lowered not in headers:
            headers[lowered] = value.strip()
    return headers


def vendor_versions(directory: Path | None) -> dict[str, str]:
    """``{"websockets": "15.0"}`` for every dist/egg-info in a vendor dir."""
    if directory is None or not directory.is_dir():
        return {}
    found: dict[str, str] = {}
    for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if child.name.endswith(".dist-info"):
            stem = child.name[: -len(".dist-info")]
        elif child.name.endswith(".egg-info"):
            stem = child.name[: -len(".egg-info")]
        else:
            continue
        headers = _dist_headers(child / "METADATA")
        name = headers.get("name", "")
        version = headers.get("version", "")
        if not name:
            name, _, fallback = stem.rpartition("-")
            if not name:
                name = stem
            else:
                version = version or fallback
        if name:
            found[normalise_name(name)] = version
    return dict(sorted(found.items()))


def _vendor_contents(directory: Path | None) -> set[str]:
    """Importable top-level names in a vendor dir (mirrors ``cli.vendor_contents``)."""
    if directory is None or not directory.is_dir():
        return set()
    names: set[str] = set()
    for child in directory.iterdir():
        name = child.name
        if name.endswith((".dist-info", ".egg-info")):
            names.add(normalise_name(name.split("-")[0]))
        elif child.is_dir() and not name.startswith("__pycache__"):
            names.add(normalise_name(name))
        elif child.suffix in (".pyd", ".so"):
            names.add(normalise_name(child.stem.split(".")[0]))
    return names


def _available_packages(directory: Path | None) -> dict[str, str]:
    available = vendor_versions(directory)
    for name in _vendor_contents(directory):
        available.setdefault(normalise_name(name), "")
    return available


def requirement_errors(
    plugin_id: str,
    requires: Iterable[str],
    vendor: Path | None,
) -> list[MetadataError]:
    """Declared requirements that the vendor tree cannot satisfy."""
    available = _available_packages(vendor)
    errors: list[MetadataError] = []
    for requirement in requires:
        try:
            name, spec = parse_requirement(requirement)
        except ValueError as exc:
            errors.append(
                MetadataError(
                    FAILURE_METADATA,
                    f"[deps].requires entry {requirement!r}: {exc}",
                    plugin_id=plugin_id,
                )
            )
            continue
        key = normalise_name(name)
        if key not in available:
            errors.append(
                MetadataError(
                    FAILURE_DEP_MISSING,
                    f"requires {requirement!r} but the vendor directory does not provide it",
                    plugin_id=plugin_id,
                )
            )
            continue
        version = available[key]
        if spec and version:
            try:
                if not satisfies(version, spec):
                    errors.append(
                        MetadataError(
                            FAILURE_DEP_MISSING,
                            f"requires {requirement!r} but vendored version is {version}",
                            plugin_id=plugin_id,
                        )
                    )
            except ValueError as exc:
                errors.append(
                    MetadataError(
                        FAILURE_METADATA,
                        f"[deps].requires entry {requirement!r}: {exc}",
                        plugin_id=plugin_id,
                    )
                )
    return errors


def vendor_conflict_map(
    packages: Iterable[PluginPackage],
) -> dict[str, tuple[MetadataError, ...]]:
    """Cross-package ``dep-conflict`` errors, keyed by plugin id."""
    package_list = list(packages)
    versions = {package.id: vendor_versions(package.vendor) for package in package_list}
    names: set[str] = set()
    for snapshot in versions.values():
        names.update(snapshot)
    conflicts: dict[str, list[MetadataError]] = {}
    for name in sorted(names):
        entries = [(pid, snapshot[name]) for pid, snapshot in versions.items() if name in snapshot]
        if len(entries) < 2:
            continue
        if len({version for _, version in entries}) < 2:
            continue
        detail = ", ".join(f"{pid} {version or '?'}" for pid, version in entries)
        message = f"{name} is vendored at different versions: {detail}"
        for pid, _ in entries:
            conflicts.setdefault(pid, []).append(MetadataError(FAILURE_DEP_CONFLICT, message, plugin_id=pid))
    return {pid: tuple(errors) for pid, errors in conflicts.items()}


# -- preflight ------------------------------------------------------------


@lru_cache(maxsize=1)
def core_row_ids() -> frozenset[str]:
    """Row ids declared by the built-in core bundle."""
    try:
        return frozenset(row.id for row in load_bundle(core_bundle_path()).rows)
    except (CompositionError, OSError):
        return frozenset()


def _reserved_ids(manager: Any | None) -> frozenset[str]:
    if manager is not None:
        return frozenset(str(item) for item in manager.bundle_ids)
    return core_row_ids()


def _installed_conflicts(plugin_id: str, vendor: Path | None, root: Path) -> list[MetadataError]:
    """Vendor names whose version differs from an already installed package."""
    mine = vendor_versions(vendor)
    if not mine:
        return []
    errors: list[MetadataError] = []
    for package in scan_plugin_packages(root):
        if package.id == plugin_id:
            continue
        theirs = vendor_versions(package.vendor)
        for name, version in mine.items():
            other = theirs.get(name)
            if other is None or other == version:
                continue
            detail = f"{plugin_id} {version or '?'}, {package.id} {other or '?'}"
            errors.append(
                MetadataError(
                    FAILURE_DEP_CONFLICT,
                    f"{name} is vendored at different versions: {detail}",
                    plugin_id=plugin_id,
                )
            )
    return errors


def _preflight_problems(
    metadata: PluginMetadata,
    plan: SourcePlan,
    root: Path,
    *,
    force: bool,
    manager: Any | None,
) -> list[str]:
    problems: list[str] = []
    version_problem = validate_version(metadata.version)
    if version_problem is not None:
        problems.append(f"metadata-invalid: {version_problem}")
    try:
        compat_errors = check_compat(metadata)
    except ValueError as exc:
        problems.append(f"metadata-invalid: {exc}")
    else:
        for error in compat_errors:
            problems.append(f"{error.kind}: {error.message}")
    target = root / metadata.id
    if target.exists() and not force:
        problems.append(f"{target} already exists (use --force to replace it)")
    if metadata.id in _reserved_ids(manager):
        problems.append(f"metadata-invalid: plugin id {metadata.id!r} is reserved by a bundle")
    if plan.path is not None:
        with contextlib.suppress(OSError):
            if plan.path.resolve() == target.resolve():
                problems.append(f"source {plan.path} is the install target itself")
    vendor = find_vendor_dir(metadata)
    for error in requirement_errors(metadata.id, metadata.deps.requires, vendor):
        # Missing vendor packages are installed after preflight; only an
        # unparseable requirement is a static preflight failure.
        if error.kind == FAILURE_METADATA:
            problems.append(f"{error.kind}: {error.message}")
    for conflict in _installed_conflicts(metadata.id, vendor, root):
        problems.append(f"{conflict.kind}: {conflict.message}")
    return problems


# -- archives and materialisation -----------------------------------------


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                member = (destination / info.filename).resolve()
                if member != base and base not in member.parents:
                    raise PreflightFailure((f"archive member escapes the extraction root: {info.filename}",))
            bundle.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise PreflightFailure((f"cannot read {archive}: {exc}",)) from exc
    except OSError as exc:
        raise PreflightFailure((f"cannot extract {archive}: {exc}",)) from exc


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as bundle:
            bundle.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise PreflightFailure((f"cannot extract {archive}: {exc}",)) from exc


def _safe_extract(archive: Path, destination: Path) -> None:
    name = archive.name.lower()
    if name.endswith((".whl", ".zip")):
        _safe_extract_zip(archive, destination)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        _safe_extract_tar(archive, destination)
    else:
        raise PreflightFailure((f"unsupported archive {archive}",))


def _payload_root(extracted: Path) -> Path:
    if (extracted / "plugin.toml").is_file():
        return extracted
    candidates = [
        child
        for child in sorted(extracted.iterdir())
        if child.is_dir() and not child.name.startswith((".", "__")) and (child / "plugin.toml").is_file()
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise PreflightFailure(
        (
            f"metadata-invalid: {extracted} does not contain plugin.toml at its root",
            "hint: place plugin.toml at the archive root or inside one top-level directory",
        )
    )


def _materialise(
    plan: SourcePlan,
    staging: Path,
    *,
    runner: Runner | None,
    offline: bool,
    timeout: float | None,
) -> Path:
    payload = staging / "payload"
    if plan.kind == "directory":
        assert plan.path is not None
        shutil.copytree(
            plan.path,
            payload,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "plugin.toml.bak"),
        )
        return payload
    if plan.kind == "archive":
        assert plan.path is not None
        extracted = staging / "extract"
        _safe_extract(plan.path, extracted)
        os.replace(_payload_root(extracted), payload)
        return payload
    dist = staging / "dist"
    dist.mkdir()
    command = UvCommand(
        (
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(dist),
            "--no-deps",
            plan.spec or plan.source,
        )
        + (("--offline",) if offline else ())
    )
    result = _execute(runner, command, timeout)
    if not result.ok:
        raise InstallFailure(
            f"uv pip install failed for {plan.source!r} (exit {result.returncode})",
            stderr_tail=result.stderr,
        )
    os.replace(_payload_root(dist), payload)
    return payload


def _declared_id(payload: Path) -> str | None:
    try:
        document = parse_toml((payload / "plugin.toml").read_bytes())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    package = document.get("package")
    if isinstance(package, Mapping):
        value = package.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = document.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _payload_id(plan: SourcePlan, payload: Path) -> str:
    declared = _declared_id(payload)
    if declared is not None:
        return declared
    if plan.expected_id:
        return plan.expected_id
    return payload.name


def _load_payload_metadata(payload: Path, plan: SourcePlan) -> PluginMetadata:
    expected = _payload_id(plan, payload)
    problem = validate_plugin_id(expected)
    if problem is not None:
        raise PreflightFailure((f"metadata-invalid: {problem}",))
    try:
        return load_metadata(payload, plugin_id=expected)
    except MetadataError as exc:
        raise PreflightFailure((f"{exc.kind}: {exc.message}",)) from exc


# -- target import validation ---------------------------------------------


def _purge_modules(module_name: str) -> None:
    """Forget a module and its submodules so validation imports from disk again."""
    prefix = f"{module_name}."
    for key in [name for name in sys.modules if name == module_name or name.startswith(prefix)]:
        sys.modules.pop(key, None)


def _plugin_base() -> type | None:
    try:
        from deeppyro.api import Plugin
    except Exception:  # noqa: BLE001 - the API may be unavailable during bootstrap
        return None
    return Plugin if isinstance(Plugin, type) else None


def _declared_import_names(metadata: PluginMetadata) -> frozenset[str]:
    names: set[str] = set()
    for requirement in metadata.deps.requires:
        try:
            name, _ = parse_requirement(requirement)
        except ValueError:
            continue
        names.add(normalise_name(name.split(".")[0]))
    return frozenset(names)


def _import_error(metadata: PluginMetadata, message: str) -> MetadataError:
    return MetadataError(
        FAILURE_IMPORT,
        message,
        plugin_id=metadata.id,
        remediation=REMEDIATION.get(FAILURE_IMPORT),
    )


def target_errors(
    metadata: PluginMetadata,
    directory: Path,
    *,
    defer_missing: bool = False,
) -> tuple[MetadataError, ...]:
    """Check that ``directory`` really provides the declared plugin class.

    The directory and its vendor tree are put on ``sys.path`` while importing;
    ``sys.path`` and ``sys.modules`` are restored afterwards. With
    ``defer_missing`` a missing top-level module that matches a declared
    requirement is not an error yet: dependencies are installed after preflight
    and the check runs again once they are available.
    """
    module_name = metadata.module.strip()
    attribute = metadata.class_name.strip()
    if not module_name or not attribute:
        return (_import_error(metadata, "plugin.toml declares no module/class target"),)
    base_dir = Path(directory)
    vendor = find_vendor_dir(metadata.with_directory(base_dir))
    entries = [base_dir] + ([vendor] if vendor is not None else [])
    inserted: list[str] = []
    for entry in reversed(entries):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)
            inserted.append(text)
    _purge_modules(module_name)
    try:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            own = missing == module_name or module_name.startswith(f"{missing}.")
            deferred = (
                defer_missing
                and not own
                and normalise_name(missing.split(".")[0]) in _declared_import_names(metadata)
            )
            if deferred:
                return ()
            return (_import_error(metadata, f"cannot import {module_name!r}: {exc}"),)
        except Exception as exc:  # noqa: BLE001 - any import failure is a finding
            return (_import_error(metadata, f"cannot import {module_name!r}: {exc}"),)
        try:
            target = getattr(module, attribute)
        except AttributeError:
            return (_import_error(metadata, f"module {module_name!r} has no attribute {attribute!r}"),)
        if not isinstance(target, type):
            return (_import_error(metadata, f"{module_name}.{attribute} is not a class"),)
        plugin_base = _plugin_base()
        if plugin_base is not None and not issubclass(target, plugin_base):
            return (
                _import_error(
                    metadata,
                    f"{module_name}.{attribute} does not subclass deeppyro.api.Plugin",
                ),
            )
        return ()
    finally:
        for text in inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(text)
        _purge_modules(module_name)


# -- manifest diagnostics --------------------------------------------------


def _manifest_error(plugin_id: str, message: str) -> MetadataError:
    return MetadataError(
        FAILURE_MANIFEST_DANGLING,
        message,
        plugin_id=plugin_id,
        remediation=MANIFEST_REMEDIATION,
    )


def _insert_paths(entries: Iterable[Path]) -> list[str]:
    inserted: list[str] = []
    for entry in entries:
        text = str(entry)
        if entry.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
            inserted.append(text)
    return inserted


def _consistent_manifest(plugin_root: Path, manifest_path: Path) -> bool:
    """Only diagnose a manifest that belongs to the scanned plugin root."""
    return manifest_path.parent == plugin_root.parent


def manifest_issues(root: Path | None = None) -> tuple[MetadataError, ...]:
    """Manifest rows whose package directory is gone or whose target cannot import."""
    from deeppyro.kernel.manifest import ManifestError, load_manifest, resolve_plugin_name

    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    manifest_path = paths.plugins_manifest_path()
    if not _consistent_manifest(plugin_root, manifest_path):
        return ()
    try:
        specs = load_manifest(manifest_path)
    except (OSError, ManifestError) as exc:
        return (
            MetadataError(
                FAILURE_METADATA,
                f"cannot read plugin manifest {manifest_path}: {exc}",
                remediation=REMEDIATION.get(FAILURE_METADATA),
            ),
        )
    issues: dict[str, MetadataError] = {}
    for spec in specs:
        plugin_id = spec.id
        if plugin_id in issues:
            continue
        directory = plugin_root / plugin_id
        if validate_plugin_id(plugin_id) is not None or not (directory / "plugin.toml").is_file():
            issues[plugin_id] = _manifest_error(
                plugin_id,
                f"manifest lists {plugin_id!r} but {directory} is not an installed plugin package",
            )
            continue
        if not spec.name:
            issues[plugin_id] = _manifest_error(
                plugin_id,
                f"manifest row {plugin_id!r} declares no plugin target",
            )
            continue
        vendor = directory / "_vendor" / python_tag()
        inserted = _insert_paths([directory, vendor, directory / "_vendor"])
        try:
            resolve_plugin_name(spec.name)
        except Exception as exc:  # noqa: BLE001 - any import failure is a finding
            issues[plugin_id] = _manifest_error(
                plugin_id,
                f"cannot import target {spec.name!r}: {exc}",
            )
        finally:
            for text in inserted:
                with contextlib.suppress(ValueError):
                    sys.path.remove(text)
    return tuple(issues[key] for key in sorted(issues))


# -- requirements install -------------------------------------------------


def _install_requirements(
    requires: Iterable[str],
    vendor_root: Path,
    *,
    runner: Runner | None,
    offline: bool,
    timeout: float | None,
) -> dict[str, str]:
    vendor_root.mkdir(parents=True, exist_ok=True)
    requirement_file = vendor_root / REQUIREMENTS_FILE
    requirement_file.write_text("\n".join(requires) + "\n", encoding="utf-8")
    command = UvCommand(
        (
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(vendor_root),
            "-r",
            str(requirement_file),
        )
        + (("--offline",) if offline else ())
    )
    result = _execute(runner, command, timeout)
    if not result.ok:
        detail = result.stderr.strip()
        message = f"uv pip install failed (exit {result.returncode})"
        if detail:
            message = f"{message}: {detail}"
        raise InstallFailure(message, stderr_tail=result.stderr)
    return vendor_versions(vendor_root)


# -- commit and record ----------------------------------------------------


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


# -- diagnostics ----------------------------------------------------------


def bundle_row_ids(package: PluginPackage) -> frozenset[str]:
    """Row ids declared by a package's optional bundle.toml."""
    path = package.directory / "bundle.toml"
    if not path.is_file():
        return frozenset()
    try:
        return frozenset(row.id for row in load_bundle(path).rows)
    except (CompositionError, OSError):
        return frozenset()


def _overrides_core(rows: Iterable[str]) -> bool:
    try:
        composition = default_composition()
    except Exception:  # noqa: BLE001 - diagnostics must never fail
        # Diagnostics must never fail; assume the rows are customised.
        return True
    for row_id in rows:
        row = composition.get(row_id)
        if row is None or not row.overridden:
            continue
        parts = [part for part in row.origin.split("|") if part]
        if any(part != "bundle:core" for part in parts[1:]):
            return True
    return False


def customized_core(packages: Iterable[PluginPackage]) -> tuple[str, ...]:
    """Packages whose bundle overrides at least one core composition row."""
    core_ids = core_row_ids()
    flagged: list[str] = []
    for package in packages:
        rows = bundle_row_ids(package) & core_ids
        if rows and _overrides_core(rows):
            flagged.append(package.id)
    return tuple(sorted(set(flagged)))


# -- removal --------------------------------------------------------------


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


# -- update ---------------------------------------------------------------


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


# -- enable / disable -----------------------------------------------------


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


# -- exact-version exemption ----------------------------------------------


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
