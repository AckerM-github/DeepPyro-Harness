"""KI-010 boundary: the native agent loop must stay off the v4 wire layer.

The v4 models in ``deeppyro.core.wire`` are a compatibility contract for
session.v4.jsonl and are slated for deletion when the v4 stack retires.  The
native loop speaks ``deeppyro.core.messages``, ``deeppyro.core.llm`` and
``deeppyro.session.schema`` instead; the old native provider adapter that
adapted the v4 model catalog is gone, replaced by ``deeppyro.agent.model_call``
and the native transport in ``deeppyro.llm.native_provider``.  These tests
scan imports with AST rather than importing modules: importing the native
package transitively loads ``core.wire`` through ``core.protocols`` anyway,
so a subprocess import check would not prove anything.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "deeppyro"

WIRE = "deeppyro.core.wire"
PROTOCOLS = "deeppyro.core.protocols"

#: Native-loop modules (M2 footprint): none of them may import ``core.wire``.
NATIVE_FOOTPRINT = (
    "agent/budget.py",
    "agent/checkpoint.py",
    "agent/context_plan.py",
    "agent/errors.py",
    "agent/events.py",
    "agent/loop.py",
    "agent/model_call.py",
    "agent/run_state.py",
    "session/fold.py",
    "session/schema.py",
    "observability/trace.py",
    "core/ids.py",
    "core/llm.py",
    "core/messages.py",
    "llm/attachments.py",
    "llm/http.py",
    "llm/native_messages.py",
    "llm/native_provider.py",
)

#: Native protocol modules must not import ``core.protocols`` either: that
#: module re-exports the v4 types and imports ``core.wire`` at import time, so
#: the footprint would be only one indirection away from the wire layer.
NATIVE_PROTOCOL_MODULES = (
    "core/llm.py",
    "agent/model_call.py",
    "llm/attachments.py",
    "llm/http.py",
    "llm/native_messages.py",
    "llm/native_provider.py",
)

#: The remaining v4 agent modules allowed to import the wire layer.  This
#: list shrinks as v4 retires; a new entry means a regression (KI-010).
AGENT_WIRE_ALLOWLIST = frozenset(
    {
        "agent/images.py",
        "agent/inbox.py",
        "agent/request.py",
        "agent/session_records.py",
        "agent/session_state.py",
        "agent/session_stream.py",
        "agent/session_support.py",
        "agent/session_tools.py",
    }
)


def _imported_modules(path: Path) -> set[str]:
    """Return the module names imported by ``path``.

    Both ``import deeppyro.core.wire`` and ``from deeppyro.core import wire``
    are resolved to ``deeppyro.core.wire`` so the guard cannot be bypassed by
    switching import style.  Relative imports are resolved against the file's
    package (the repo layout is fixed: ``SRC/<package>/<module>.py``).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    try:
        package = ("deeppyro", *path.relative_to(SRC).with_suffix("").parts[:-1])
    except ValueError:  # tests feed temp files outside the source tree
        package = ()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                keep = len(package) - (node.level - 1)
                base = list(package[:keep]) if keep > 0 else []
            else:
                base = []
            module_parts = [*base, *(node.module.split(".") if node.module else [])]
            module = ".".join(module_parts)
            if module:
                modules.add(module)
            for alias in node.names:
                if alias.name != "*" and module:
                    modules.add(f"{module}.{alias.name}")
    return modules


def _imports_wire(path: Path) -> bool:
    return any(module == WIRE or module.startswith(WIRE + ".") for module in _imported_modules(path))


def _imports_protocols(path: Path) -> bool:
    return any(
        module == PROTOCOLS or module.startswith(PROTOCOLS + ".") for module in _imported_modules(path)
    )


def test_native_footprint_does_not_import_core_wire() -> None:
    checkouts = [module for module in NATIVE_FOOTPRINT if not (SRC / module).is_file()]
    assert checkouts == [], f"native footprint files are missing: {checkouts}"
    offenders = [module for module in NATIVE_FOOTPRINT if _imports_wire(SRC / module)]
    assert offenders == [], f"native modules import {WIRE}: {offenders}"


def test_observability_package_is_wire_free() -> None:
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in sorted((SRC / "observability").rglob("*.py"))
        if _imports_wire(path)
    ]
    assert offenders == [], f"observability modules import {WIRE}: {offenders}"


def test_agent_wire_importers_are_the_known_v4_surface() -> None:
    importers = {
        path.relative_to(SRC).as_posix()
        for path in sorted((SRC / "agent").rglob("*.py"))
        if _imports_wire(path)
    }
    unknown = importers - AGENT_WIRE_ALLOWLIST
    assert unknown == set(), (
        f"new agent modules import {WIRE}; native code must use core.messages: {sorted(unknown)}"
    )


def test_native_protocol_modules_do_not_import_core_protocols() -> None:
    checkouts = [module for module in NATIVE_PROTOCOL_MODULES if not (SRC / module).is_file()]
    assert checkouts == [], f"native protocol files are missing: {checkouts}"
    offenders = [module for module in NATIVE_PROTOCOL_MODULES if _imports_protocols(SRC / module)]
    assert offenders == [], f"native protocol modules import {PROTOCOLS}, which imports {WIRE}: {offenders}"


def test_import_scanner_catches_wire_import_styles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the AST guard honest: switching import style must not evade it."""
    absolute = tmp_path / "absolute.py"
    absolute.write_text(
        "from deeppyro.core import wire\nimport deeppyro.core.wire\n",
        encoding="utf-8",
    )
    assert _imports_wire(absolute)

    fake_root = tmp_path / "fake_src" / "deeppyro"
    (fake_root / "agent").mkdir(parents=True)
    relative = fake_root / "agent" / "loop.py"
    relative.write_text("from ..core import wire\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SRC", fake_root)
    assert _imports_wire(relative)


def test_core_wire_documents_its_deprecation() -> None:
    """KI-010 asks for an in-module deprecation note with a removal condition."""
    tree = ast.parse((SRC / "core" / "wire.py").read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree) or ""
    assert "Deprecated" in doc
    assert "KI-010" in doc
    assert "native" in doc
