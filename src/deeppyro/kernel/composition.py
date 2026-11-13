"""Bundle composition: ordered plugin rows assembled from bundle files.

A bundle is a directory containing bundle.toml with a [bundle] table and an
ordered [[plugins]] list. Rows from several bundles and patch manifests are
merged by plugin id:

* a new id is inserted (honouring before/after) or appended;
* an existing id is overridden: config is replaced as a whole, disabled and
  critical may be changed, and the plugin target may not change class.

This mirrors how DSH composes its package-level dsh.bundle.patch rows and the
profile cordis.patch.yml: patches never deep-merge config.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from deeppyro.api import API_VERSION
from deeppyro.kernel.manifest import (
    ManifestError,
    PluginSpec,
    load_manifest,
    resolve_plugin_name,
)
from deeppyro.paths import deeppyro_home


class CompositionError(RuntimeError):
    """Raised for invalid bundle files or impossible patch operations."""


@dataclass(frozen=True, slots=True)
class PluginRow:
    """One plugin entry before it is resolved into a class."""

    id: str
    plugin: str = ""
    config: dict[str, Any] | None = None
    disabled: bool | None = None
    before: str | None = None
    after: str | None = None
    critical: bool | None = None
    origin: str = ""
    base: Path | None = None
    name: str = ""
    isolate: tuple[str, ...] = ()
    intercept: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Bundle:
    """A parsed bundle.toml."""

    id: str
    name: str
    version: str
    api_version: int
    order: int
    path: Path
    critical: bool
    rows: tuple[PluginRow, ...]


#: Keys accepted in one [[plugins]] row.
_ROW_KEYS = frozenset(
    {
        "id",
        "plugin",
        "name",
        "config",
        "disabled",
        "before",
        "after",
        "critical",
        "isolate",
        "intercept",
    }
)
_BUNDLE_KEYS = frozenset({"id", "name", "version", "api_version", "order", "critical"})


def core_bundle_path() -> Path:
    """Location of the bundle shipped with deeppyro itself."""
    return Path(__file__).resolve().parent.parent / "bundles" / "core" / "bundle.toml"


def user_bundles_dir() -> Path:
    """Directory scanned for user-installed bundles."""
    return deeppyro_home() / "bundles"


def discover_user_bundles(root: str | Path | None = None) -> list[Path]:
    """Return bundle.toml files under root, sorted by directory name."""
    base = Path(root) if root is not None else user_bundles_dir()
    if not base.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if child.is_dir():
            candidate = child / "bundle.toml"
            if candidate.is_file():
                found.append(candidate)
    return found


def _string(value: Any, *, field_name: str, origin: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompositionError(f"{origin}: {field_name} must be a non-empty string")
    return value.strip()


def _bool(value: Any, *, field_name: str, origin: str) -> bool:
    if not isinstance(value, bool):
        raise CompositionError(f"{origin}: {field_name} must be a boolean")
    return value


def _row_from_entry(entry: dict[str, Any], *, base: Path, origin: str) -> PluginRow:
    if not isinstance(entry, dict):
        raise CompositionError(f"{origin}: every [[plugins]] entry must be a table")
    unknown = set(entry) - _ROW_KEYS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise CompositionError(f"{origin}: unknown plugin row keys: {names}")
    plugin_id = _string(entry.get("id"), field_name="id", origin=origin)
    plugin = entry.get("plugin")
    if plugin is not None and not isinstance(plugin, str):
        raise CompositionError(f"{origin}: plugin must be a string")
    config = entry.get("config")
    if config is not None and not isinstance(config, dict):
        raise CompositionError(f"{origin}: config for {plugin_id!r} must be a table")
    isolate = entry.get("isolate") or []
    if not isinstance(isolate, list) or not all(isinstance(item, str) for item in isolate):
        raise CompositionError(f"{origin}: isolate for {plugin_id!r} must be a list of strings")
    intercept = entry.get("intercept") or {}
    if not isinstance(intercept, dict):
        raise CompositionError(f"{origin}: intercept for {plugin_id!r} must be a table")
    disabled = entry.get("disabled")
    if disabled is not None:
        disabled = _bool(disabled, field_name="disabled", origin=origin)
    critical = entry.get("critical")
    if critical is not None:
        critical = _bool(critical, field_name="critical", origin=origin)
    before = entry.get("before")
    after = entry.get("after")
    for key, value in (("before", before), ("after", after)):
        if value is not None and not isinstance(value, str):
            raise CompositionError(f"{origin}: {key} for {plugin_id!r} must be a string")
    return PluginRow(
        id=plugin_id,
        plugin=(plugin or "").strip(),
        config=dict(config) if config is not None else None,
        disabled=disabled,
        before=before,
        after=after,
        critical=critical,
        origin=origin,
        base=base,
        name=str(entry.get("name") or ""),
        isolate=tuple(isolate),
        intercept={str(k): dict(v) for k, v in intercept.items()},
    )


def load_bundle(path: str | Path) -> Bundle:
    """Parse one bundle.toml (or a directory containing it)."""
    bundle_path = Path(path)
    if bundle_path.is_dir():
        bundle_path = bundle_path / "bundle.toml"
    if not bundle_path.is_file():
        raise CompositionError(f"bundle not found: {bundle_path}")
    try:
        with bundle_path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CompositionError(f"cannot read bundle {bundle_path}: {exc}") from exc
    unknown = set(document) - {"bundle", "plugins"}
    if unknown:
        names = ", ".join(sorted(unknown))
        raise CompositionError(f"{bundle_path}: unknown top-level keys: {names}")
    meta = document.get("bundle")
    if not isinstance(meta, dict):
        raise CompositionError(f"{bundle_path}: missing [bundle] table")
    unknown = set(meta) - _BUNDLE_KEYS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise CompositionError(f"{bundle_path}: unknown [bundle] keys: {names}")
    origin = f"bundle:{bundle_path}"
    bundle_id = _string(meta.get("id"), field_name="bundle.id", origin=origin)
    api_version = meta.get("api_version", API_VERSION)
    if not isinstance(api_version, int) or api_version < 1:
        raise CompositionError(f"{bundle_path}: bundle.api_version must be a positive integer")
    if api_version > API_VERSION:
        raise CompositionError(
            f"{bundle_path}: bundle.api_version {api_version} is newer than supported {API_VERSION}"
        )
    order = meta.get("order", 0)
    if not isinstance(order, int):
        raise CompositionError(f"{bundle_path}: bundle.order must be an integer")
    critical = meta.get("critical", False)
    if not isinstance(critical, bool):
        raise CompositionError(f"{bundle_path}: bundle.critical must be a boolean")
    raw_rows = document.get("plugins", [])
    if not isinstance(raw_rows, list):
        raise CompositionError(f"{bundle_path}: plugins must be an array of tables")
    rows = tuple(_row_from_entry(entry, base=bundle_path.parent, origin=origin) for entry in raw_rows)
    for row in rows:
        if not row.plugin:
            raise CompositionError(f"{bundle_path}: plugin row {row.id!r} is missing the plugin target")
    return Bundle(
        id=bundle_id,
        name=str(meta.get("name") or bundle_id),
        version=str(meta.get("version") or "0.0.0"),
        api_version=api_version,
        order=order,
        path=bundle_path,
        critical=critical,
        rows=rows,
    )


_RESOLVED: dict[tuple[str, str], type] = {}
_ALIASES: dict[str, str] = {}


def _resolve_target(target: str, base: Path | None) -> type:
    target = target.strip()
    if not target:
        raise CompositionError("plugin target is empty")
    alias = _ALIASES.get(target)
    if alias is not None:
        target = alias
    key = (target, str(base) if base is not None else "")
    cached = _RESOLVED.get(key)
    if cached is not None:
        return cached
    if ":" not in target:
        try:
            cls = resolve_plugin_name(target)
        except ManifestError as exc:
            raise CompositionError(str(exc)) from exc
        _RESOLVED[key] = cls
        return cls
    module_part, _, attribute = target.rpartition(":")
    if not module_part or not attribute:
        raise CompositionError(f"invalid plugin target: {target!r} (expected module:Class)")
    looks_like_path = (
        module_part.endswith(".py")
        or module_part.startswith(".")
        or "/" in module_part
        or "\\" in module_part
    )
    if looks_like_path:
        path = Path(module_part)
        if not path.is_absolute():
            if base is None:
                raise CompositionError(f"relative plugin path {module_part!r} needs a bundle directory")
            path = base / path
        if not path.is_file():
            raise CompositionError(f"plugin module not found: {path}")
        module_name = f"_deeppyro_bundle_{abs(hash((str(path), str(base))))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise CompositionError(f"cannot import plugin module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    else:
        try:
            module = importlib.import_module(module_part)
        except ImportError as exc:
            raise CompositionError(f"cannot import plugin module {module_part!r}: {exc}") from exc
    cls = getattr(module, attribute, None)
    if cls is None:
        raise CompositionError(f"plugin class {attribute!r} not found in {module_part!r}")
    if not callable(cls):
        raise CompositionError(f"plugin target {target!r} is not callable")
    _RESOLVED[key] = cls
    return cls


def register_target_alias(alias: str, target: str) -> None:
    """Teach composition about a short name such as llm."""
    _ALIASES[alias] = target


def resolve_row(row: PluginRow) -> type:
    """Resolve one row into its plugin class."""
    return _resolve_target(row.plugin, row.base)


def rows_from_specs(specs: Any, *, origin: str = "manifest") -> list[PluginRow]:
    """Convert legacy PluginSpec entries into composition rows."""
    result: list[PluginRow] = []
    for spec in specs:
        if not isinstance(spec, PluginSpec):
            raise CompositionError(f"{origin}: expected PluginSpec, got {type(spec).__name__}")
        # An empty config means "leave the bundle config alone"; a non-empty
        # table replaces it, matching bundle patch semantics.
        config = dict(spec.config) if spec.config else None
        result.append(
            PluginRow(
                id=spec.id,
                plugin=spec.name or "",
                config=config,
                disabled=spec.disabled,
                before=spec.before,
                after=spec.after,
                origin=origin,
            )
        )
    return result


def row_to_spec(row: PluginRow) -> PluginSpec:
    """Render one composition row back as a legacy PluginSpec."""
    return PluginSpec(
        id=row.id,
        name=row.plugin or None,
        config=dict(row.config or {}),
        disabled=row.disabled,
        before=row.before,
        after=row.after,
    )


class Composition:
    """Ordered plugin rows merged from bundles and patches."""

    def __init__(self, rows: Any = (), *, origin: str = "") -> None:
        self._rows: dict[str, PluginRow] = {}
        self.order: list[str] = []
        self.warnings: list[str] = []
        self.apply(rows, origin=origin)

    # -- read -------------------------------------------------------------
    def rows(self) -> list[PluginRow]:
        return [self._rows[plugin_id] for plugin_id in self.order]

    def get(self, plugin_id: str) -> PluginRow | None:
        return self._rows.get(plugin_id)

    def enabled(self) -> list[PluginRow]:
        return [row for row in self.rows() if row.disabled is not True]

    def __len__(self) -> int:
        return len(self.order)

    # -- write ------------------------------------------------------------
    def apply(self, rows: Any, *, origin: str = "", critical: bool | None = None) -> None:
        rows = list(rows)
        for row in rows:
            if not isinstance(row, PluginRow):
                raise CompositionError(f"{origin or 'patch'}: expected PluginRow")
            if origin:
                row = replace(row, origin=origin)
            if critical is not None and row.critical is None:
                row = replace(row, critical=critical)
            current = self._rows.get(row.id)
            if current is None:
                if not row.plugin:
                    raise CompositionError(
                        f"{row.origin or origin}: unknown plugin id {row.id!r} and no plugin target was given"
                    )
                self._rows[row.id] = row
                self._insert(row)
                continue
            if row.plugin and resolve_row(row) is not resolve_row(current):
                raise CompositionError(
                    f"plugin {row.id!r} cannot change class ({current.plugin} -> {row.plugin})"
                )
            merged = replace(
                current,
                plugin=current.plugin or row.plugin,
                config=row.config if row.config is not None else current.config,
                disabled=row.disabled if row.disabled is not None else current.disabled,
                critical=row.critical if row.critical is not None else current.critical,
                origin=_merge_origin(current.origin, row.origin),
                name=row.name or current.name,
                isolate=row.isolate or current.isolate,
                intercept={**current.intercept, **row.intercept},
            )
            self._rows[row.id] = merged

    def _insert(self, row: PluginRow) -> None:
        if row.before is not None:
            if row.before not in self.order:
                raise CompositionError(f"before target {row.before!r} does not exist")
            index = self.order.index(row.before)
            self.order.insert(index, row.id)
            return
        if row.after is not None:
            if row.after not in self.order:
                raise CompositionError(f"after target {row.after!r} does not exist")
            index = self.order.index(row.after) + 1
            self.order.insert(index, row.id)
            return
        self.order.append(row.id)


def _merge_origin(current: str, patch: str) -> str:
    if not patch or patch == current:
        return current
    if not current:
        return patch
    if patch in current.split("|"):
        return current
    return f"{current}|{patch}"


def default_composition(
    *,
    bundle_paths: Any = (),
    extra_bundle_paths: Any = (),
    manifest_paths: Any = (),
) -> Composition:
    """Build the default composition: core bundle, user bundles, patches."""
    composition = Composition()
    core = load_bundle(core_bundle_path())
    composition.apply(core.rows, origin=f"bundle:{core.id}", critical=core.critical)
    candidates: list[Path] = []
    for path in bundle_paths:
        candidates.append(Path(path))
    discovered = discover_user_bundles()
    for path in discovered:
        if path not in candidates:
            candidates.append(path)
    for path in extra_bundle_paths:
        candidate = Path(path)
        if candidate not in candidates:
            candidates.append(candidate)
    bundles: list[Bundle] = []
    for path in candidates:
        try:
            bundles.append(load_bundle(path))
        except CompositionError as exc:
            composition.warnings.append(str(exc))
    for bundle in sorted(bundles, key=lambda item: (item.order, item.id)):
        composition.apply(bundle.rows, origin=f"bundle:{bundle.id}", critical=bundle.critical)
    for path in manifest_paths:
        try:
            specs = load_manifest(str(path))
        except (OSError, ManifestError) as exc:
            composition.warnings.append(f"cannot read manifest {path}: {exc}")
            continue
        composition.apply(rows_from_specs(specs, origin=f"manifest:{path}"), critical=True)
    return composition


__all__ = [
    "Bundle",
    "Composition",
    "CompositionError",
    "PluginRow",
    "core_bundle_path",
    "default_composition",
    "discover_user_bundles",
    "load_bundle",
    "register_target_alias",
    "resolve_row",
    "row_to_spec",
    "rows_from_specs",
    "user_bundles_dir",
]
