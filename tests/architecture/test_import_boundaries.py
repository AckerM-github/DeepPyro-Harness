"""Architecture gates for imports that cross internal boundaries.

The kernel used to run on a vendored cordis port (``deeppyro._vendor.cordis_port``).
That port is gone: ``deeppyro.kernel`` now hosts the plugin runtime natively and
``deeppyro.api`` is the only seam plugins see.  These tests keep the removal from
regressing: the vendored tree must not come back, and no module under
``src/deeppyro`` may import it again.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "deeppyro"
VENDOR_ROOT = SRC_ROOT / "_vendor"
VENDOR_PACKAGE = "deeppyro._vendor"


def _import_targets(node: ast.AST) -> list[str]:
    """Dotted module names named by one ``import``/``from ... import`` node."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.module else []
    return []


def find_vendor_imports(root: Path = SRC_ROOT) -> list[str]:
    """Return "path:line: import ..." for every import of the removed vendor tree.

    root is the package directory whose children are kernel/, api/, builtin/, ...
    Tests pass a temporary tree to prove both import forms are detected.
    """
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        lines = source.splitlines()
        for node in ast.walk(tree):
            for target in _import_targets(node):
                if target == VENDOR_PACKAGE or target.startswith(VENDOR_PACKAGE + "."):
                    line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    offenders.append(f"{path.relative_to(root).as_posix()}:{node.lineno}: {line}")
    return offenders


def test_the_vendored_cordis_port_is_gone() -> None:
    assert not VENDOR_ROOT.exists(), (
        "src/deeppyro/_vendor still exists: the kernel must run on its native runtime, "
        "not on a vendored framework port"
    )


def test_no_module_imports_the_removed_vendor_package() -> None:
    offenders = find_vendor_imports()
    assert not offenders, (
        "deeppyro._vendor was removed; import the kernel runtime or deeppyro.api instead:\n"
        + "\n".join(offenders)
    )


def test_detector_flags_both_import_forms(tmp_path: Path) -> None:
    (tmp_path / "builtin").mkdir()
    (tmp_path / "kernel").mkdir()
    (tmp_path / "builtin" / "bad_import.py").write_text(
        "import deeppyro._vendor.cordis_port\n",
        encoding="utf-8",
    )
    (tmp_path / "builtin" / "bad_from.py").write_text(
        "from deeppyro._vendor.cordis_port import Fiber\n",
        encoding="utf-8",
    )
    (tmp_path / "kernel" / "allowed.py").write_text(
        "from deeppyro.kernel.fiber import Fiber\n",
        encoding="utf-8",
    )

    offenders = find_vendor_imports(tmp_path)
    assert len(offenders) == 2
    assert any("bad_import.py:1:" in item for item in offenders)
    assert any("bad_from.py:1:" in item and "cordis_port" in item for item in offenders)
    assert not any("allowed.py" in item for item in offenders)
