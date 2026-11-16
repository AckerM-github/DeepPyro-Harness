"""Preflight checks: reserved ids, vendor conflicts and install problems."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

from deeppyro.kernel.composition import CompositionError, core_bundle_path, default_composition, load_bundle
from deeppyro.plugins.bootstrap import PluginPackage, scan_plugin_packages
from deeppyro.plugins.install_requirements import SourcePlan, validate_version
from deeppyro.plugins.install_vendor import requirement_errors, vendor_versions
from deeppyro.plugins.metadata import (
    FAILURE_DEP_CONFLICT,
    FAILURE_METADATA,
    MetadataError,
    PluginMetadata,
    check_compat,
    find_vendor_dir,
)

CUSTOMIZED_CORE = "customized-core"


@lru_cache(maxsize=1)
def core_row_ids() -> frozenset[str]:
    """Row ids declared by the built-in core bundle."""
    try:
        return frozenset(row.id for row in load_bundle(core_bundle_path()).rows)
    except (CompositionError, OSError):
        return frozenset()


def _reserved_ids(manager: Any | None) -> frozenset[str]:
    if manager is not None:
        return frozenset(str(item) for item in manager.bundle_ids)
    return core_row_ids()


def _installed_conflicts(plugin_id: str, vendor: Path | None, root: Path) -> list[MetadataError]:
    """Vendor names whose version differs from an already installed package."""
    mine = vendor_versions(vendor)
    if not mine:
        return []
    errors: list[MetadataError] = []
    for package in scan_plugin_packages(root):
        if package.id == plugin_id:
            continue
        theirs = vendor_versions(package.vendor)
        for name, version in mine.items():
            other = theirs.get(name)
            if other is None or other == version:
                continue
            detail = f"{plugin_id} {version or '?'}, {package.id} {other or '?'}"
            errors.append(
                MetadataError(
                    FAILURE_DEP_CONFLICT,
                    f"{name} is vendored at different versions: {detail}",
                    plugin_id=plugin_id,
                )
            )
    return errors


def _preflight_problems(
    metadata: PluginMetadata,
    plan: SourcePlan,
    root: Path,
    *,
    force: bool,
    manager: Any | None,
) -> list[str]:
    problems: list[str] = []
    version_problem = validate_version(metadata.version)
    if version_problem is not None:
        problems.append(f"metadata-invalid: {version_problem}")
    try:
        compat_errors = check_compat(metadata)
    except ValueError as exc:
        problems.append(f"metadata-invalid: {exc}")
    else:
        for error in compat_errors:
            problems.append(f"{error.kind}: {error.message}")
    target = root / metadata.id
    if target.exists() and not force:
        problems.append(f"{target} already exists (use --force to replace it)")
    if metadata.id in _reserved_ids(manager):
        problems.append(f"metadata-invalid: plugin id {metadata.id!r} is reserved by a bundle")
    if plan.path is not None:
        with contextlib.suppress(OSError):
            if plan.path.resolve() == target.resolve():
                problems.append(f"source {plan.path} is the install target itself")
    vendor = find_vendor_dir(metadata)
    for error in requirement_errors(metadata.id, metadata.deps.requires, vendor):
        # Missing vendor packages are installed after preflight; only an
        # unparseable requirement is a static preflight failure.
        if error.kind == FAILURE_METADATA:
            problems.append(f"{error.kind}: {error.message}")
    for conflict in _installed_conflicts(metadata.id, vendor, root):
        problems.append(f"{conflict.kind}: {conflict.message}")
    return problems


def bundle_row_ids(package: PluginPackage) -> frozenset[str]:
    """Row ids declared by a package's optional bundle.toml."""
    path = package.directory / "bundle.toml"
    if not path.is_file():
        return frozenset()
    try:
        return frozenset(row.id for row in load_bundle(path).rows)
    except (CompositionError, OSError):
        return frozenset()


def _overrides_core(rows: Iterable[str]) -> bool:
    try:
        composition = default_composition()
    except Exception:  # noqa: BLE001 - diagnostics must never fail
        # Diagnostics must never fail; assume the rows are customised.
        return True
    for row_id in rows:
        row = composition.get(row_id)
        if row is None or not row.overridden:
            continue
        parts = [part for part in row.origin.split("|") if part]
        if any(part != "bundle:core" for part in parts[1:]):
            return True
    return False


def customized_core(packages: Iterable[PluginPackage]) -> tuple[str, ...]:
    """Packages whose bundle overrides at least one core composition row."""
    core_ids = core_row_ids()
    flagged: list[str] = []
    for package in packages:
        rows = bundle_row_ids(package) & core_ids
        if rows and _overrides_core(rows):
            flagged.append(package.id)
    return tuple(sorted(set(flagged)))
