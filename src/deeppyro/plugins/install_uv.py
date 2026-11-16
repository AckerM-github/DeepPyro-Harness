"""Captured uv invocations and the subprocess runner behind an injectable runner."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from deeppyro.plugins.install_errors import InstallerError, InstallFailure
from deeppyro.plugins.install_vendor import vendor_versions

UV_ENV = "DPYRO_UV"

UV_TIMEOUT = 600.0

STDERR_TAIL_LINES = 200

STDERR_TAIL_CHARS = 20_000

STDOUT_TAIL_LINES = 400

STDOUT_TAIL_CHARS = 40_000

REQUIREMENTS_FILE = "requirements.txt"

Runner = Callable[["UvCommand"], "UvResult"]


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


def _execute(runner: Runner | None, command: UvCommand, timeout: float | None) -> UvResult:
    if runner is not None:
        return runner(command)
    from deeppyro.plugins import install

    return install.run_uv(command, timeout=timeout)


def _uv_preflight_problem(runner: Runner | None) -> str | None:
    """Preflight: registry/git and dependency installs need a uv executable."""
    if runner is not None:
        return None
    from deeppyro.plugins import install

    try:
        install.uv_executable()
    except InstallerError as exc:
        return str(exc)
    return None


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
