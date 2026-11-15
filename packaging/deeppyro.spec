# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821 - SPECPATH/EXE/PYZ/COLLECT are injected by PyInstaller
r"""PyInstaller spec for a frozen DeepPyro Harness build.

``packaging/build.ps1`` is the supported entry point: it sets the environment
read here, runs the build and then the self-test.  Direct use also works::

    $env:DEEPPYRO_FREEZE_MODE = "onedir"    # or "onefile"
    .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean packaging\deeppyro.spec

Build inputs are *generated* by ``packaging/hiddenimports.py`` from the shipped
bundle data, so the frozen plugin module list can never drift from
``src/deeppyro/bundles/core/bundle.toml`` (see docs/spec/frozen-distribution.md).
"""

import os
import sys
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()  # noqa: F821 - SPECPATH is injected by PyInstaller
sys.path.insert(0, str(SPEC_DIR))

import hiddenimports as inputs  # noqa: E402
from PyInstaller.utils.hooks import copy_metadata  # noqa: E402

MODE = os.environ.get("DEEPPYRO_FREEZE_MODE", "onedir").strip().lower()
if MODE not in ("onedir", "onefile"):
    raise SystemExit(f"unknown DEEPPYRO_FREEZE_MODE={MODE!r}: expected onedir or onefile")

NAME = os.environ.get("DEEPPYRO_FREEZE_NAME", "dpyro").strip() or "dpyro"
# Freezing plugin code is opt-in: it conflicts with installing plugins into an
# already-built executable, and plugin packages carry their own _vendor deps.
INCLUDE_PLUGINS = os.environ.get("DEEPPYRO_FREEZE_PLUGINS", "0") == "1"

hiddenimports = inputs.collect_hiddenimports(include_plugins=INCLUDE_PLUGINS)
datas = inputs.data_files() + copy_metadata("deeppyro-harness")

a = Analysis(
    [str(inputs.LAUNCHER)],
    pathex=[str(inputs.SRC_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(inputs.EXCLUDES),
    noarchive=False,
)
pyz = PYZ(a.pure)

# console=True is not negotiable: the TUI and every child-process protocol need
# a real console handle, so never build this --windowed / --noconsole.
if MODE == "onefile":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=NAME,
    )
