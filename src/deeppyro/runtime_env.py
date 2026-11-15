"""Locate runtime resources and hidden sub-entry points in one place.

The harness ships in two shapes:

* a source/wheel install, where packaged data lives next to this module;
* a PyInstaller frozen executable, where packaged data lives under
  ``sys._MEIPASS`` and a Python interpreter is no longer available on disk.

Everything that answers "am I frozen", "where is this data file" or "how do I
re-enter myself as a subprocess" goes through this module, so a frozen build
only has to be taught one convention.

Hidden sub-entry points
-----------------------
A frozen executable cannot re-invoke ``python -m deeppyro....``: its
``sys.executable`` is the harness binary itself.  The subprocess protocols
therefore re-enter that same binary through the flags below, dispatched by
``deeppyro.tui.main`` before any runtime boot.  Source installs keep using
``-m``; both paths stay supported (see docs/spec/frozen-distribution.md).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Hidden flag: re-enter the executable as the Windows ACL sandbox runner.
INTERNAL_SANDBOX_RUNNER = "--internal-sandbox-runner"
#: Hidden flag: re-enter the executable as the PTC worker.
INTERNAL_PTC_WORKER = "--internal-ptc-worker"
#: Every hidden flag; the CLI rejects unknown arguments that look like these.
INTERNAL_FLAGS: tuple[str, ...] = (INTERNAL_SANDBOX_RUNNER, INTERNAL_PTC_WORKER)


def is_frozen() -> bool:
    """True when running from a PyInstaller (or compatible) frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Root directory that packaged data files are read from.

    Frozen builds use the PyInstaller extraction root (``sys._MEIPASS``; for an
    onedir build that is ``<app>/_internal``).  When a frozen build somehow has
    no extraction root, the executable directory is the best available answer.
    Source installs use the ``deeppyro`` package directory, so
    ``data_path("bundles/core/bundle.toml")`` resolves identically in both.
    """
    if is_frozen():
        root = getattr(sys, "_MEIPASS", None)
        if root:
            return Path(os.fspath(root))
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_path(name: str | os.PathLike[str]) -> Path:
    """Absolute path of one packaged data file (existence is not checked)."""
    return bundle_dir() / Path(name)


def executable() -> str:
    """The program that starts subprocesses: the interpreter or the frozen exe."""
    return sys.executable or "python"


__all__ = [
    "INTERNAL_FLAGS",
    "INTERNAL_PTC_WORKER",
    "INTERNAL_SANDBOX_RUNNER",
    "bundle_dir",
    "data_path",
    "executable",
    "is_frozen",
]
