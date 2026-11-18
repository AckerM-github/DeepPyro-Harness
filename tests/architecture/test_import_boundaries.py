"""Architecture gates for imports that cross internal boundaries.

The kernel used to run on a vendored cordis port (deeppyro._vendor.cordis_port).
That port is gone: deeppyro.kernel now hosts the plugin runtime natively and
deeppyro.api is the only seam plugins see.  Three gates keep that shape:

1. the vendored tree must not come back;
2. nothing under src/deeppyro may import it again;
3. only the kernel package and the process composition entry points
   (headless.py, tui/runtime.py, web/host.py) may import the runtime modules
   (kernel.app/container/context/fiber/host).  Domain code reaches the same
   services through ctx/deeppyro.api, so "everything is a plugin" stays
   enforceable instead of eroding into direct imports of the framework.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = ROOT / "src" / "deeppyro"
VENDOR_ROOT = SRC_ROOT / "_vendor"
VENDOR_PACKAGE = "deeppyro._vendor"
KERNEL_PACKAGE = "deeppyro.kernel"

#: Runtime internals: container, context tree, lifecycle, host adapter and the
#: composition root.  Domain code must not import these directly.
RUNTIME_MODULES = frozenset({"app", "container", "context", "fiber", "host"})

#: Paths relative to src/deeppyro allowed to import them: the kernel itself and
#: the two composition entry points (they call boot() to assemble the app).
RUNTIME_ALLOWED = ("kernel/", "headless.py", "tui/runtime.py", "web/host.py")


def _import_targets(node: ast.AST) -> list[str]:
    """Dotted module names named by one import/from-import node."""
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


def _module_parts(path: Path, root: Path) -> list[str]:
    """Dotted module path of one file, root being the deeppyro package."""
    parts = ["deeppyro", *path.relative_to(root).with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _absolute_module(node: ast.ImportFrom, path: Path, root: Path) -> str | None:
    """Resolve a relative from-import against the file's own package."""
    if node.level == 0:
        return node.module
    package = _module_parts(path, root)
    if path.name != "__init__.py":
        package = package[:-1]
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _is_runtime_target(target: str, *, imports_kernel_package: bool) -> bool:
    """Whether one dotted target reaches a runtime module or the kernel package."""
    if not target.startswith(KERNEL_PACKAGE):
        return False
    if target == KERNEL_PACKAGE:
        # from deeppyro.kernel import X: the package re-exports the runtime.
        return imports_kernel_package
    parts = target.split(".")
    return len(parts) > 2 and parts[2] in RUNTIME_MODULES


def find_runtime_imports(root: Path = SRC_ROOT) -> list[str]:
    """Return "path:line: import ..." for domain modules importing the runtime."""
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(RUNTIME_ALLOWED):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        lines = source.splitlines()
        for node in ast.walk(tree):
            targets: list[tuple[str, bool]] = []
            if isinstance(node, ast.Import):
                targets = [(alias.name, alias.name == KERNEL_PACKAGE) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = _absolute_module(node, path, root)
                if module is not None:
                    targets = [(module, module == KERNEL_PACKAGE)]
            for target, imports_package in targets:
                if _is_runtime_target(target, imports_kernel_package=imports_package):
                    line = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                    offenders.append(f"{relative}:{node.lineno}: {line}")
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


def test_domain_modules_do_not_import_the_kernel_runtime() -> None:
    offenders = find_runtime_imports()
    assert not offenders, (
        "domain modules must reach services through ctx/deeppyro.api, not by "
        "importing the kernel runtime; only deeppyro.kernel and the composition "
        "entry points (headless.py, tui/runtime.py, web/host.py) may import it:\n" + "\n".join(offenders)
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


def test_runtime_detector_flags_every_forbidden_form(tmp_path: Path) -> None:
    (tmp_path / "builtin").mkdir()
    (tmp_path / "kernel").mkdir()
    (tmp_path / "tui").mkdir()
    files = {
        "builtin/bad_import.py": "import deeppyro.kernel.container\n",
        "builtin/bad_from.py": "from deeppyro.kernel.fiber import Fiber\n",
        "builtin/bad_package.py": "from deeppyro.kernel import boot\n",
        "builtin/bad_relative.py": "from ..kernel.app import create_app\n",
        "builtin/ok_config.py": "from deeppyro.kernel.config_service import KernelConfig\n",
        "builtin/ok_composition.py": "from deeppyro.kernel.composition import PluginRow\n",
        "builtin/ok_runtime.py": "from deeppyro.kernel.runtime import KernelRuntime\n",
        "kernel/allowed.py": "from deeppyro.kernel.app import App\n",
        "headless.py": "from deeppyro.kernel import boot\n",
        "tui/runtime.py": "from deeppyro.kernel import boot\n",
    }
    for relative, source in files.items():
        (tmp_path / relative).write_text(source, encoding="utf-8")

    offenders = find_runtime_imports(tmp_path)
    flagged = sorted(item.split(":")[0] for item in offenders)
    assert flagged == [
        "builtin/bad_from.py",
        "builtin/bad_import.py",
        "builtin/bad_package.py",
        "builtin/bad_relative.py",
    ]
