"""PyInstaller inputs for a frozen deeppyro build, derived from shipped data.

The composition a build must carry is *data*: ``bundles/core/bundle.toml``
names 41 plugin modules that nothing imports statically, so a static analyser
alone drops them (and, transitively, httpx/Pillow).  This module
derives ``hiddenimports`` and ``datas`` from the very bundle files the runtime
reads, so the frozen build cannot drift from the shipped composition -- there
is nothing to hand-maintain.

Called by ``packaging/deeppyro.spec`` at build time, printed by
``packaging/build.ps1 -DryRun``, and asserted by
``tests/architecture/test_frozen_hiddenimports.py``.  Standard library only, and
it never imports the harness (so it also works from a half-built checkout).
"""

from __future__ import annotations

import importlib.util
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
PACKAGE_ROOT = SRC_ROOT / "deeppyro"
LAUNCHER = Path(__file__).resolve().with_name("launcher.py")
CORE_BUNDLE = PACKAGE_ROOT / "bundles" / "core" / "bundle.toml"

#: Modules imported only through strings, attributes or hidden flags.
DYNAMIC_HIDDENIMPORTS: tuple[str, ...] = (
    # textual picks its driver inside App.get_driver_class() by platform.
    "textual.drivers.windows_driver",
    "textual.drivers.linux_driver",
    # zoneinfo finds the tz database through importlib.resources, not an import.
    "tzdata",
    # Our own hidden re-entry targets, imported inside the dispatch branch.
    "deeppyro.ptc.worker",
    "deeppyro.sandbox.windows_acl_runner",
)

#: Heavy packages the harness never imports (kept out of the bundle).
EXCLUDES: tuple[str, ...] = (
    "IPython",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "_pytest",
    "_tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "pip",
    "pydoc",
    "pytest",
    "respx",
    "scipy",
    "setuptools",
    "tkinter",
    "wheel",
)

_BUNDLE_FILE = "bundle.toml"
_PLUGIN_FILE = "plugin.toml"
_LEGACY_KEYS = ("module", "class")


# -- TOML helpers ---------------------------------------------------------


def _load_toml(path: Path) -> Mapping[str, Any]:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):  # tolerate a BOM from hand-edited files
        data = data[3:]
    return tomllib.loads(data.decode("utf-8"))


def module_name(target: str) -> str | None:
    """Importable module named by a row target, or None for path-loaded plugins.

    ``deeppyro.builtin.llm:LlmPlugin`` -> ``deeppyro.builtin.llm``.
    ``./plugin.py:Thing`` and other path-like or alias-only targets load from
    disk at runtime, so they must never become hiddenimports.
    """
    head = target.partition(":")[0].strip()
    if not head or "." not in head:
        return None
    if head.endswith(".py") or head.startswith((".", "/", "\\")):
        return None
    if "/" in head or "\\" in head:
        return None
    if not all(part.isidentifier() for part in head.split(".")):
        return None
    return head


# -- bundle rows ----------------------------------------------------------


def bundle_targets(path: str | os.PathLike[str], *, _seen: set[Path] | None = None) -> list[str]:
    """Plugin targets declared by one bundle file, in row order.

    Walks inline ``rows`` arrays and ``include`` files the same way
    ``deeppyro.kernel.composition`` does, so a nested or included row is never
    missed; cycles are ignored instead of raised (the runtime reports those).
    """
    bundle_path = Path(path)
    if bundle_path.is_dir():
        bundle_path = bundle_path / _BUNDLE_FILE
    seen = set() if _seen is None else _seen
    if not bundle_path.is_file():
        return []
    resolved = bundle_path.resolve()
    if resolved in seen:
        return []
    seen.add(resolved)
    document = _load_toml(bundle_path)
    rows = document.get("plugins")
    targets: list[str] = []
    for row in rows if isinstance(rows, list) else []:
        targets.extend(_row_targets(row, base=bundle_path.parent, seen=seen))
    return targets


def _row_targets(row: Any, *, base: Path, seen: set[Path]) -> list[str]:
    if not isinstance(row, Mapping):
        return []
    out: list[str] = []
    plugin = row.get("plugin")
    if isinstance(plugin, str) and plugin.strip():
        out.append(plugin.strip())
    include = row.get("include")
    if isinstance(include, str) and include.strip():
        out.extend(bundle_targets(base / include.strip(), _seen=seen))
    nested = row.get("rows")
    if isinstance(nested, list):
        for child in nested:
            out.extend(_row_targets(child, base=base, seen=seen))
    return out


def discover_bundles(root: str | os.PathLike[str]) -> list[Path]:
    """Bundle files under root, sorted by directory name (user bundle layout)."""
    base = Path(root)
    if not base.is_dir():
        return []
    return [
        child / _BUNDLE_FILE
        for child in sorted(base.iterdir(), key=lambda item: item.name)
        if child.is_dir() and (child / _BUNDLE_FILE).is_file()
    ]


# -- plugin package directories (opt-in) ----------------------------------


def discover_plugin_dirs(root: str | os.PathLike[str]) -> list[Path]:
    """Plugin package directories (those holding a plugin.toml), sorted."""
    base = Path(root)
    if not base.is_dir():
        return []
    return [
        child
        for child in sorted(base.iterdir(), key=lambda item: item.name)
        if child.is_dir() and (child / _PLUGIN_FILE).is_file()
    ]


def plugin_dir_targets(directory: str | os.PathLike[str]) -> list[str]:
    """``module:Class`` targets declared by one plugin package (best effort).

    The authoritative parser is ``deeppyro.plugins.metadata``; this reads the
    same two keys from the flat, ``[plugin]`` and ``[identity]`` shapes so the
    generator stays import-free.  Only used when freezing plugin code, which is
    opt-in (``DEEPPYRO_FREEZE_PLUGINS=1``).
    """
    path = Path(directory) / _PLUGIN_FILE
    if not path.is_file():
        return []
    document = _load_toml(path)
    sections = [document]
    sections.extend(value for value in document.values() if isinstance(value, Mapping))
    module = class_name = ""
    for section in sections:
        for key in _LEGACY_KEYS:
            value = section.get(key)
            if isinstance(value, str) and value.strip():
                if key == "module":
                    module = module or value.strip()
                else:
                    class_name = class_name or value.strip()
    if not module:
        return []
    return [f"{module}:{class_name}" if class_name else module]


# -- user defaults (mirror deeppyro.paths without importing it) -----------


def default_bundles_dir() -> Path:
    """``~/.dpro/bundles`` or ``$DEEPPYRO_HOME/bundles``."""
    home = os.environ.get("DEEPPYRO_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".dpro"
    return base / "bundles"


def default_plugins_dir() -> Path:
    """``$DEEPPYRO_PLUGINS_DIR`` or ``~/.dpro/plugins``."""
    override = os.environ.get("DEEPPYRO_PLUGINS_DIR")
    return Path(override).expanduser() if override else Path.home() / ".dpro" / "plugins"


# -- generated inputs -----------------------------------------------------


def collect_hiddenimports(
    *,
    core_bundle: str | os.PathLike[str] = CORE_BUNDLE,
    bundles_dir: str | os.PathLike[str] | None = None,
    plugins_dir: str | os.PathLike[str] | None = None,
    include_plugins: bool = False,
) -> list[str]:
    """Every module the frozen build must carry, sorted and de-duplicated.

    Core bundle rows are mandatory.  User bundles under ``bundles_dir`` are
    discovered too (default ``~/.dpro/bundles``), so a build matches the
    machine that produced it.  Plugin packages are opt-in: freezing plugin code
    conflicts with installing plugins into an already-built exe.
    """
    targets = bundle_targets(core_bundle)
    for path in discover_bundles(default_bundles_dir() if bundles_dir is None else bundles_dir):
        targets.extend(bundle_targets(path))
    if include_plugins:
        root = default_plugins_dir() if plugins_dir is None else plugins_dir
        for directory in discover_plugin_dirs(root):
            targets.extend(plugin_dir_targets(directory))
    modules = {name for target in targets if (name := module_name(target)) is not None}
    modules.update(DYNAMIC_HIDDENIMPORTS)
    return sorted(modules)


def core_bundle_modules(*, core_bundle: str | os.PathLike[str] = CORE_BUNDLE) -> list[str]:
    """Modules named by the core bundle only (used by the architecture test)."""
    return sorted(
        {name for target in bundle_targets(core_bundle) if (name := module_name(target)) is not None}
    )


def _site_package(name: str) -> Path | None:
    """Directory of an installed package, or None when it is not available."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    if spec is None or spec.submodule_search_locations is None:
        return None
    return Path(next(iter(spec.submodule_search_locations)))


def data_files(*, package_root: str | os.PathLike[str] = PACKAGE_ROOT) -> list[tuple[str, str]]:
    """``(source, destination)`` pairs for data the runtime reads at boot.

    Destinations are relative to ``sys._MEIPASS``, which is exactly what
    ``deeppyro.runtime_env.data_path`` prepends.
    """
    root = Path(package_root)
    items: list[tuple[str, str]] = [(str(root / "bundles" / "core" / _BUNDLE_FILE), "bundles/core")]
    certifi = _site_package("certifi")
    if certifi is not None and (certifi / "cacert.pem").is_file():
        items.append((str(certifi / "cacert.pem"), "certifi"))
    tzdata = _site_package("tzdata")
    if tzdata is not None and (tzdata / "zoneinfo").is_dir():
        items.append((str(tzdata / "zoneinfo"), "tzdata/zoneinfo"))
    return items


def summary(*, include_plugins: bool = False) -> str:
    """Human-readable build input summary (``build.ps1 -DryRun``)."""
    targets = bundle_targets(CORE_BUNDLE)
    hidden = collect_hiddenimports(include_plugins=include_plugins)
    datas = data_files()
    return "\n".join(
        [
            f"core bundle rows : {len(targets)} targets, {len(core_bundle_modules())} modules",
            f"hiddenimports   : {len(hidden)} modules",
            f"datas           : {len(datas)} entries",
            *[f"  {dest}" for _source, dest in datas],
        ]
    )


def missing_core_modules(**kwargs: Any) -> list[str]:
    """Core bundle modules absent from the generated list (must stay empty)."""
    generated = set(collect_hiddenimports(**kwargs))
    return [name for name in core_bundle_modules() if name not in generated]


__all__ = [
    "CORE_BUNDLE",
    "DYNAMIC_HIDDENIMPORTS",
    "EXCLUDES",
    "LAUNCHER",
    "PACKAGE_ROOT",
    "REPO_ROOT",
    "SRC_ROOT",
    "bundle_targets",
    "collect_hiddenimports",
    "core_bundle_modules",
    "data_files",
    "default_bundles_dir",
    "default_plugins_dir",
    "discover_bundles",
    "discover_plugin_dirs",
    "missing_core_modules",
    "module_name",
    "plugin_dir_targets",
    "summary",
]
