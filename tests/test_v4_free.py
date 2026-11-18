"""Guard: v4-exclusive modules must stay deleted and unreferenced.

The v4 removal happened in waves W1-W5; this tuple is the frozen record of every
module and package those waves deleted. The guard is stdlib-only and cheap enough
to run in every gate: it parses every Python file under src with ast, never imports
the source. A deleted module must not be imported, named inside a string
(docstrings count) or mounted from the core bundle; tests must not keep exercising
a deleted path either. This file is the only one allowed to name the modules (the
frozen tuple below). Keep the scanner self-test green when editing the scanner.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "deeppyro"
TESTS = ROOT / "tests"
BUNDLE = SRC / "bundles" / "core" / "bundle.toml"

#: Modules/packages deleted by the v4-removal waves, frozen so they never come back.
V4_MODULES: tuple[str, ...] = (
    # W1: v4 LLM transports and message converters, retry/journal plugins, migration.
    "deeppyro.migrate",
    "deeppyro.session.migrate_cli",
    "deeppyro.session.migrate_service",
    "deeppyro.llm.anthropic",
    "deeppyro.llm.messages",
    "deeppyro.llm.messages_anthropic",
    "deeppyro.llm.openai_compat",
    "deeppyro.builtin.llm_retry",
    "deeppyro.builtin.hook_journal",
    # W1/W2: v4 TUI history and mode plumbing.
    "deeppyro.tui.history",
    "deeppyro.tui.native_mode",
    # W2: v4 session-query and projection services.
    "deeppyro.session_query",
    "deeppyro.builtin.session_query",
    "deeppyro.projection",
    "deeppyro.builtin.projections",
    # W3: the v4 agent session stack.
    "deeppyro.agent.session",
    "deeppyro.agent.session_state",
    "deeppyro.agent.session_turn",
    "deeppyro.agent.session_tools",
    "deeppyro.agent.session_records",
    "deeppyro.agent.session_support",
    "deeppyro.agent.session_stream",
    "deeppyro.agent.session_prompt",
    "deeppyro.agent.request",
    "deeppyro.agent.inbox",
    "deeppyro.agent.retry",
    "deeppyro.agent.stream",
    "deeppyro.agent.images",
    "deeppyro.agent.options",
    # W3: the v4 compaction stack (native_* modules replaced it).
    "deeppyro.compact.compactor",
    "deeppyro.compact.surface",
    "deeppyro.compact.tokens",
    "deeppyro.compact.refs",
    "deeppyro.compact.compress_tool",
    "deeppyro.compact.block_tools",
    # W3: v4 goal replay and round driver.
    "deeppyro.goal.replay",
    "deeppyro.goal.round_driver",
    # W3: v4 hook records and prompt projection.
    "deeppyro.hooks.records",
    "deeppyro.prompt.projection",
    # W3: v4 image budget.
    "deeppyro.llm.image_budget",
    # W4: the v4 session log stack (schema/fold/native_* are native and stay).
    "deeppyro.session.session",
    "deeppyro.session.store",
    "deeppyro.session.writer",
    "deeppyro.session.reader",
    "deeppyro.session.events",
    "deeppyro.session.derive",
    "deeppyro.session.fork",
    "deeppyro.session.layout",
    "deeppyro.session.metadata",
    "deeppyro.session.image_projection",
    # W5: v4 wire/payload models.
    "deeppyro.core.wire",
    "deeppyro.core.payloads",
)
# NOTE: deeppyro.core.protocols is NOT frozen: the native loop still imports it as
# the live tool contract (Tool/ToolContext/ToolResult). Only the v4-only wire and
# payload models were deleted.

#: Class names from the deleted v4 stack. Module paths are covered by V4_MODULES,
#: but a bare class name in prose would otherwise go unnoticed.
V4_SYMBOLS: tuple[str, ...] = ("AgentSession",)
#: Directories whose class-name sweep is owned by another change (the TUI is
#: native and green; its module-path guard still applies like everywhere else).
_LEAD_OWNED = (SRC / "tui", TESTS / "tui")

#: Any dotted name introduced by a deeppyro.* string constant (docstrings included).
_MODULE_REF = re.compile(r"\bdeeppyro\.(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*")
#: A list assigned to a name like BANNED_IMPORTS/FORBIDDEN_PREFIXES is a negative
#: guard: it asserts removed modules are ABSENT, so its names are not references.
_NEGATIVE_GUARD_NAME = re.compile(r"(?i)(ban|forbid)")


def _module_name(path: Path, root: Path) -> str:
    """Import name of a file under root (its package prefix is deeppyro)."""
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["deeppyro", *parts])


def _module_path(module: str) -> Path:
    """On-disk location a plain module would occupy."""
    base = SRC.joinpath(*module.split(".")[1:])
    return base.with_suffix(".py")


def _package_init(module: str) -> Path:
    return SRC.joinpath(*module.split(".")[1:]) / "__init__.py"


def _is_frozen(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in V4_MODULES)


def _absolute_import_targets(tree: ast.AST, module: str, is_package: bool) -> Iterator[tuple[int, str]]:
    """Yield (line, import name) pairs, resolving relative imports against the package."""
    package = module if is_package else module.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parents = package.split(".")
                trimmed = parents[: len(parents) - (node.level - 1)] if node.level > 1 else parents
                base = ".".join(trimmed + ([base] if base else []))
            if not base:
                continue
            yield node.lineno, base
            for alias in node.names:
                if alias.name == "*":
                    continue
                yield node.lineno, f"{base}.{alias.name}"


def _negative_guard_nodes(tree: ast.AST) -> set[int]:
    """Ids of string constants that live inside a banned/forbidden name list."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if node.value is None:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not any(_NEGATIVE_GUARD_NAME.search(name) for name in names):
            continue
        guarded.update(id(child) for child in ast.walk(node.value))
    return guarded


def _string_refs(tree: ast.AST, guarded: set[int]) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _MODULE_REF.finditer(node.value):
                yield node.lineno, match.group(0)


def _scan(root: Path = SRC) -> list[str]:
    """Return human-readable violations found under root."""
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path, root)
        is_package = path.name == "__init__.py"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            violations.append(f"{path}:{exc.lineno}: SyntaxError: {exc.msg}")
            continue
        for lineno, target in _absolute_import_targets(tree, module, is_package):
            if _is_frozen(target):
                violations.append(f"{path}:{lineno}: imports {target}")
        guarded = _negative_guard_nodes(tree)
        for lineno, ref in _string_refs(tree, guarded):
            if _is_frozen(ref):
                violations.append(f"{path}:{lineno}: references {ref}")
    return violations


def _symbol_violations(root: Path) -> list[str]:
    """Deleted class names must not survive even as prose under root."""
    violations: list[str] = []
    self_path = Path(__file__).resolve()
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == self_path:
            continue
        if any(directory in path.parents for directory in _LEAD_OWNED):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            violations.append(f"{path}:{exc.lineno}: SyntaxError: {exc.msg}")
            continue
        for node in ast.walk(tree):
            name: str | None = None
            if isinstance(node, ast.Name) and node.id in V4_SYMBOLS:
                name = node.id
            elif isinstance(node, ast.Attribute) and node.attr in V4_SYMBOLS:
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = next((symbol for symbol in V4_SYMBOLS if symbol in node.value), None)
            if name is not None:
                violations.append(f"{path}:{getattr(node, 'lineno', 0)}: mentions {name}")
    return violations


def _bundle_plugin_rows() -> list[str]:
    data = tomllib.loads(BUNDLE.read_text(encoding="utf-8"))
    return [str(plugin.get("plugin", "")) for plugin in data.get("plugins", [])]


def test_deleted_module_files_are_gone() -> None:
    present = [
        module for module in V4_MODULES if _module_path(module).exists() or _package_init(module).exists()
    ]
    assert present == [], f"deleted modules are back on disk: {present}"


def test_src_imports_and_strings_do_not_reference_deleted_modules() -> None:
    violations = _scan(SRC)
    assert violations == [], "\n".join(violations)


def test_tests_do_not_reference_deleted_modules() -> None:
    guard = str(Path(__file__).resolve())
    violations = [violation for violation in _scan(TESTS) if not violation.startswith(guard)]
    assert violations == [], "\n".join(violations)


def test_deleted_symbols_are_unreferenced() -> None:
    violations = _symbol_violations(SRC) + _symbol_violations(TESTS)
    assert violations == [], "\n".join(violations)


def test_core_bundle_has_no_deleted_plugin_rows() -> None:
    offenders = [row for row in _bundle_plugin_rows() if _is_frozen(row.split(":")[0])]
    assert offenders == [], f"core bundle still mounts deleted modules: {offenders}"


def test_scanner_self_test(tmp_path: Path) -> None:
    package = tmp_path / "deeppyro"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "gone.py").write_text("", encoding="utf-8")
    (package / "alive.py").write_text("", encoding="utf-8")
    (package / "uses.py").write_text(
        "from deeppyro.gone import thing\n"
        "from deeppyro import gone\n"
        "import deeppyro.gone.sub\n"
        "TEXT = 'see deeppyro.gone for details'\n",
        encoding="utf-8",
    )
    original = V4_MODULES
    try:
        globals()["V4_MODULES"] = ("deeppyro.gone",)
        violations = _scan(tmp_path)
    finally:
        globals()["V4_MODULES"] = original
    assert len(violations) == 5, violations
    assert all("deeppyro.gone" in violation for violation in violations)


def test_symbol_scanner_self_test(tmp_path: Path) -> None:
    package = tmp_path / "deeppyro"
    package.mkdir()
    (package / "old.py").write_text(
        '"""Removed: AgentSession emitted dicts."""\nclass Wrapper:\n    agent = AgentSession()\n',
        encoding="utf-8",
    )
    violations = _symbol_violations(tmp_path)
    assert len(violations) == 2, violations
    assert all("AgentSession" in violation for violation in violations)
