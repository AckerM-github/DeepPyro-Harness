"""Bundle composition: ordered plugin rows assembled from bundle files.

A bundle is a directory containing bundle.toml with a [bundle] table and an
ordered [[plugins]] list. Rows from several bundles and patch manifests are
merged by plugin id:

* a new id is inserted (honouring before/after) or appended;
* an existing id is overridden: config is replaced as a whole, disabled and
  critical may be changed, and the plugin target may not change class;
* rows marked overridden keep only the last merged value but remember that a
  later patch layer replaced their config/disabled (the loader reports that as
  the 'overridden' live state).

Rows may be containers:

* group = true turns a row into a group. Nested rows come from an inline
  rows = [...] array or TOML [[plugins.rows]] tables; a group creates a child
  cordis context (carrying the group's isolate/intercept) and mounts its
  children in order.
* include = "rows.toml" / "rows.json" loads an ordered row list from a file
  relative to the declaring file and splices it in place. A group row with
  include appends the included rows to its children instead.

Bundles may depend on other bundles:

    [bundle]
    requires = [{ id = "router", version = ">=0.2" }]

Requirements are resolved topologically (dependencies first); missing bundles,
dependency cycles and unsupported version constraints raise CompositionError.

This mirrors how DSH composes its package-level dsh.bundle.patch rows and the
profile cordis.patch.yml: patches never deep-merge config.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
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
    group: bool = False
    children: tuple[PluginRow, ...] = ()
    overridden: bool = False


@dataclass(frozen=True, slots=True)
class BundleRequirement:
    """One entry of [bundle].requires."""

    id: str
    version: str | None = None


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
    requires: tuple[BundleRequirement, ...] = ()


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
        "group",
        "rows",
        "include",
    }
)
_BUNDLE_KEYS = frozenset({"id", "name", "version", "api_version", "order", "critical", "requires"})

#: Guards against runaway recursive include chains (cycles are rejected first).
_MAX_INCLUDE_DEPTH = 32

_CONSTRAINT_RE = re.compile(r"^\s*(>=|<=|==|!=|>|<|=)\s*(\d+(?:\.\d+)*)\s*$")
_REQUIREMENT_ID_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(.*)$")


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


# -- requirement versions ----------------------------------------------------


def _version_parts(value: str, *, origin: str) -> tuple[int, ...]:
    parts = value.strip().split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise CompositionError(
            f"{origin}: invalid version {value!r} (expected dotted integers such as 0.1.0)"
        )
    return tuple(int(part) for part in parts)


def _parse_constraint(text: str, *, origin: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    if _CONSTRAINT_RE.match(text) is None:
        raise CompositionError(
            f"{origin}: unsupported version constraint {text!r} "
            "(expected one of >=, <=, >, <, ==, = followed by a dotted version)"
        )
    return text


def _satisfies(version: str, constraint: str, *, origin: str) -> bool:
    match = _CONSTRAINT_RE.match(constraint)
    if match is None:
        raise CompositionError(f"{origin}: unsupported version constraint {constraint!r}")
    operator, raw = match.groups()
    left = _version_parts(version, origin=origin)
    right = _version_parts(raw, origin=origin)
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    if operator == ">=":
        return left >= right
    if operator == ">":
        return left > right
    if operator == "<=":
        return left <= right
    if operator == "<":
        return left < right
    if operator in ("==", "="):
        return left == right
    return left != right


def _parse_requirements(raw: Any, *, origin: str) -> tuple[BundleRequirement, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CompositionError(f"{origin}: bundle.requires must be an array of tables or strings")
    requirements: list[BundleRequirement] = []
    for item in raw:
        if isinstance(item, str):
            match = _REQUIREMENT_ID_RE.match(item)
            if match is None:
                raise CompositionError(f"{origin}: invalid bundle requirement {item!r}")
            dep_id = match.group(1)
            constraints = match.group(2).strip()
            requirements.append(
                BundleRequirement(id=dep_id, version=_parse_constraint(constraints, origin=origin))
            )
            continue
        if not isinstance(item, dict):
            raise CompositionError(f"{origin}: bundle requirement must be a table or string")
        unknown = set(item) - {"id", "version"}
        if unknown:
            names = ", ".join(sorted(unknown))
            raise CompositionError(f"{origin}: unknown bundle requirement keys: {names}")
        dep_id = _string(item.get("id"), field_name="requires.id", origin=origin)
        version = item.get("version")
        if version is not None and not isinstance(version, str):
            raise CompositionError(f"{origin}: requires.version for {dep_id!r} must be a string")
        requirements.append(
            BundleRequirement(id=dep_id, version=_parse_constraint(version or "", origin=origin))
        )
    return tuple(requirements)


# -- rows: groups and includes ----------------------------------------------


def _read_include_file(path: Path, *, origin: str) -> list[Any]:
    suffix = path.suffix.lower()
    try:
        if suffix == ".toml":
            with path.open("rb") as handle:
                document = tomllib.load(handle)
            if not isinstance(document, dict):
                raise CompositionError(f"{origin}: include file {path} must contain a table")
            raw = document.get("plugins")
            if raw is None:
                raw = document.get("rows")
        elif suffix == ".json":
            with path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            raw = document
            if isinstance(raw, dict):
                raw = raw.get("plugins")
                if raw is None:
                    raw = document.get("rows")
        else:
            raise CompositionError(
                f"{origin}: unsupported include format {suffix or '(none)'!r} "
                "(only .toml and .json are supported)"
            )
    except CompositionError:
        raise
    except (OSError, ValueError) as exc:
        raise CompositionError(f"{origin}: cannot read include file {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise CompositionError(f"{origin}: include file {path} must contain a list of plugin rows")
    return raw


def _parse_rows(
    entries: Any,
    *,
    base: Path,
    origin: str,
    include_stack: tuple[Path, ...] = (),
) -> list[PluginRow]:
    if not isinstance(entries, list):
        raise CompositionError(f"{origin}: plugins must be an array of tables")
    rows: list[PluginRow] = []
    for entry in entries:
        rows.extend(_expand_entry(entry, base=base, origin=origin, include_stack=include_stack))
    return rows


def _expand_entry(
    entry: Any,
    *,
    base: Path,
    origin: str,
    include_stack: tuple[Path, ...],
) -> list[PluginRow]:
    row = _row_from_entry(entry, base=base, origin=origin, include_stack=include_stack)
    include = entry.get("include")
    if include is None:
        return [row]
    target = Path(include)
    resolved = target if target.is_absolute() else base / target
    resolved = resolved.resolve()
    if resolved in include_stack:
        chain = " -> ".join(str(item) for item in (*include_stack, resolved))
        raise CompositionError(f"{origin}: include cycle detected: {chain}")
    if len(include_stack) >= _MAX_INCLUDE_DEPTH:
        raise CompositionError(f"{origin}: include nesting is too deep (max {_MAX_INCLUDE_DEPTH})")
    if not resolved.is_file():
        raise CompositionError(f"{origin}: include file not found: {resolved}")
    included = _parse_rows(
        _read_include_file(resolved, origin=origin),
        base=resolved.parent,
        origin=f"{origin} include {resolved.name}",
        include_stack=(*include_stack, resolved),
    )
    if row.group:
        return [replace(row, children=(*row.children, *included))]
    return included


def _row_from_entry(
    entry: dict[str, Any],
    *,
    base: Path,
    origin: str,
    include_stack: tuple[Path, ...] = (),
) -> PluginRow:
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
    group = entry.get("group", False)
    if group is not None:
        group = _bool(group, field_name="group", origin=origin)
    nested = entry.get("rows")
    include = entry.get("include")
    if include is not None and not isinstance(include, str):
        raise CompositionError(f"{origin}: include for {plugin_id!r} must be a string")
    container = bool(group) or nested is not None or include is not None
    #: An include-only row is a load-time macro and disappears after splicing;
    #: nested rows (or group=true) make the row a real child context.
    is_group = bool(group) or nested is not None
    children: tuple[PluginRow, ...] = ()
    if container:
        if plugin:
            raise CompositionError(
                f"{origin}: container row {plugin_id!r} (group/include) must not declare a plugin target"
            )
        if nested is not None:
            children = tuple(_parse_rows(nested, base=base, origin=origin, include_stack=include_stack))
    elif not (plugin or "").strip():
        raise CompositionError(f"{origin}: plugin row {plugin_id!r} is missing the plugin target")
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
        group=is_group,
        children=children,
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
    requires = _parse_requirements(meta.get("requires"), origin=origin)
    raw_rows = document.get("plugins", [])
    rows = tuple(_parse_rows(raw_rows, base=bundle_path.parent, origin=origin))
    return Bundle(
        id=bundle_id,
        name=str(meta.get("name") or bundle_id),
        version=str(meta.get("version") or "0.0.0"),
        api_version=api_version,
        order=order,
        path=bundle_path,
        critical=critical,
        rows=rows,
        requires=requires,
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
    if not row.plugin:
        raise CompositionError(f"plugin row {row.id!r} has no plugin target")
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

    def flat(self) -> list[PluginRow]:
        """Every row including nested group children, depth first."""
        result: list[PluginRow] = []

        def walk(row: PluginRow) -> None:
            result.append(row)
            for child in row.children:
                walk(child)

        for row in self.rows():
            walk(row)
        return result

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
                if not row.plugin and not row.group:
                    raise CompositionError(
                        f"{row.origin or origin}: unknown plugin id {row.id!r} and no plugin target was given"
                    )
                self._rows[row.id] = row
                self._insert(row)
                continue
            if row.plugin:
                if not current.plugin:
                    raise CompositionError(f"plugin {row.id!r} cannot gain a plugin target from a patch row")
                if resolve_row(row) is not resolve_row(current):
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
                group=current.group or row.group,
                children=row.children if row.children else current.children,
                overridden=current.overridden or row.config is not None or row.disabled is not None,
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


def _ordered_bundles(
    bundles: list[Bundle],
    *,
    applied_ids: set[str],
    known: dict[str, Bundle],
) -> list[Bundle]:
    """Topologically order external bundles: dependencies first.

    Bundles already applied (the core bundle) are treated as satisfied roots;
    among bundles that do not constrain each other the usual (order, id) sort
    wins.
    """
    by_id = dict(known)
    for bundle in bundles:
        by_id.setdefault(bundle.id, bundle)
    for bundle in bundles:
        for requirement in bundle.requires:
            dependency = by_id.get(requirement.id)
            if dependency is None:
                raise CompositionError(f"bundle {bundle.id!r} requires missing bundle {requirement.id!r}")
            if requirement.version is not None and not _satisfies(
                dependency.version, requirement.version, origin=f"bundle {bundle.id!r}"
            ):
                raise CompositionError(
                    f"bundle {bundle.id!r} requires {requirement.id!r} {requirement.version}, "
                    f"but version {dependency.version} is installed"
                )
    ordered: list[Bundle] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(bundle: Bundle) -> None:
        status = state.get(bundle.id)
        if status == 2 or bundle.id in applied_ids:
            return
        if status == 1:
            cycle = " -> ".join([*stack, bundle.id])
            raise CompositionError(f"bundle dependency cycle: {cycle}")
        state[bundle.id] = 1
        stack.append(bundle.id)
        for requirement in bundle.requires:
            visit(by_id[requirement.id])
        stack.pop()
        state[bundle.id] = 2
        ordered.append(bundle)

    for bundle in sorted(bundles, key=lambda item: (item.order, item.id)):
        visit(bundle)
    return ordered


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
    for bundle in _ordered_bundles(bundles, applied_ids={core.id}, known={core.id: core}):
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
    "BundleRequirement",
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
