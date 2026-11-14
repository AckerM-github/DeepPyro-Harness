"""Architecture gates for imports that cross internal boundaries.

deeppyro._vendor.cordis_port is the vendored cordis port.  It is an
implementation detail of the kernel bridge: only deeppyro.kernel and the
deeppyro.api seam may import it.  Every other package must go through a public
API (for example deeppyro.api.FiberState) so the vendored implementation stays
replaceable.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "deeppyro"

VENDOR_TARGET = "deeppyro._vendor.cordis_port"
VENDOR_PARENT = "deeppyro._vendor"
ALLOWED_PACKAGES = frozenset({"api", "kernel"})


def _target_from_import(node: ast.Import) -> list[str]:
    return [alias.name for alias in node.names]


def _target_from_import_from(node: ast.ImportFrom) -> list[str]:
    if node.level:
        # The codebase uses absolute imports for the vendor package.  A
        # relative import cannot name deeppyro._vendor from outside that
        # package anyway, so it needs no special handling here.
        return []
    module = node.module or ""
    if module == VENDOR_PARENT:
        return [f"{module}.{alias.name}" for alias in node.names if alias.name == "cordis_port"]
    return [module]


def _is_vendor_target(module: str) -> bool:
    return module == VENDOR_TARGET or module.startswith(VENDOR_TARGET + ".")


def find_vendor_import_violations(root: Path = SRC_ROOT) -> list[str]:
    """Return "path:line: import ..." for every forbidden vendor import.

    root is the package directory (the directory whose children are kernel/,
    api/, builtin/, ...).  Callers use the default src/deeppyro root; tests use
    a temporary tree to prove the detector recognises both import forms.
    """
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        package = relative.parts[0] if len(relative.parts) > 1 else ""
        if package in ALLOWED_PACKAGES:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        lines = source.splitlines()
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = _target_from_import(node)
            elif isinstance(node, ast.ImportFrom):
                targets = _target_from_import_from(node)
            for target in targets:
                if _is_vendor_target(target):
                    line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    violations.append(f"{relative.as_posix()}:{node.lineno}: {line}")
    return violations


def test_vendored_cordis_port_imports_stay_in_kernel_and_api() -> None:
    violations = find_vendor_import_violations()
    assert not violations, (
        "deeppyro._vendor.cordis_port may only be imported from deeppyro/kernel/ "
        "and deeppyro/api/; route other imports through deeppyro.api.\n" + "\n".join(violations)
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
    (tmp_path / "builtin" / "bad_parent.py").write_text(
        "from deeppyro._vendor import cordis_port\n",
        encoding="utf-8",
    )
    (tmp_path / "kernel" / "allowed.py").write_text(
        "from deeppyro._vendor.cordis_port import Context\n",
        encoding="utf-8",
    )

    violations = find_vendor_import_violations(tmp_path)
    assert len(violations) == 3
    assert any("bad_from.py:1:" in item and "cordis_port" in item for item in violations)
    assert any("bad_import.py:1:" in item for item in violations)
    assert any("bad_parent.py:1:" in item for item in violations)
    assert not any("allowed.py" in item for item in violations)
