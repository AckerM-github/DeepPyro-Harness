"""Regression guard: the TUI and headless paths are native-only.

The 0.3 cut deliberately deleted the v4 runtime rather than keeping a fallback
switch. A future change that reintroduces the old session stack would make the
product's two entry points diverge again, so this test freezes the property at
the import/string level. It is intentionally coarse: comments may still explain
history, but no code may import the removed modules or accept the removed flags.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "deeppyro"

BANNED_STRINGS = (
    "DEEPPYRO_TUI_NATIVE",
    "DEEPPYRO_NATIVE_LOOP",
    "--legacy-v4",
    "--native-loop",
    "native_loop",
    "legacy_v4",
    "dpyro migrate",
    "tui.history",
    "tui.native_mode",
    "session.store",
    "session_query",
)

BANNED_IMPORTS = (
    "deeppyro.session.store",
    "deeppyro.session.reader",
    "deeppyro.session.writer",
    "deeppyro.session.derive",
    "deeppyro.session.events",
    "deeppyro.session.fork",
    "deeppyro.session.metadata",
    "deeppyro.session_query",
    "deeppyro.projections",
    "deeppyro.tui.history",
    "deeppyro.tui.native_mode",
    "deeppyro.core.wire",
)

TARGETS = (
    ROOT / "tui",
    ROOT / "headless.py",
)


def _python_files() -> list[Path]:
    files: list[Path] = []
    for target in TARGETS:
        if target.is_dir():
            files.extend(sorted(target.rglob("*.py")))
        elif target.is_file():
            files.append(target)
    return files


def test_tui_and_headless_have_no_v4_switches_or_imports() -> None:
    files = _python_files()
    assert files, "the TUI/headless source tree moved; update this guard"

    offenders: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT.parents[1])
        source = path.read_text(encoding="utf-8")
        for banned in BANNED_STRINGS:
            if banned in source:
                offenders.append(f"{relative}: contains {banned!r}")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in BANNED_IMPORTS:
                        offenders.append(f"{relative}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module in BANNED_IMPORTS:
                offenders.append(f"{relative}:{node.lineno}: from {node.module} import ...")

    assert not offenders, "v4 runtime came back into the native entry points:\n" + "\n".join(offenders)
