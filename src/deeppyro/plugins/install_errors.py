"""Installer failure types shared by the install pipeline."""

from __future__ import annotations

from collections.abc import Iterable


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
