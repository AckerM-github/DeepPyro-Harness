"""Vendor-tree snapshots plus requirement and conflict diagnostics."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from deeppyro.plugins.bootstrap import PluginPackage
from deeppyro.plugins.install_requirements import normalise_name, parse_requirement
from deeppyro.plugins.metadata import (
    FAILURE_DEP_CONFLICT,
    FAILURE_DEP_MISSING,
    FAILURE_METADATA,
    MetadataError,
    satisfies,
)


def _dist_headers(path: Path) -> dict[str, str]:
    headers: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return headers
    for line in text.splitlines():
        if not line.strip():
            break
        key, separator, value = line.partition(":")
        if not separator:
            continue
        lowered = key.strip().lower()
        if lowered in ("name", "version") and lowered not in headers:
            headers[lowered] = value.strip()
    return headers


def vendor_versions(directory: Path | None) -> dict[str, str]:
    """``{"websockets": "15.0"}`` for every dist/egg-info in a vendor dir."""
    if directory is None or not directory.is_dir():
        return {}
    found: dict[str, str] = {}
    for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if child.name.endswith(".dist-info"):
            stem = child.name[: -len(".dist-info")]
        elif child.name.endswith(".egg-info"):
            stem = child.name[: -len(".egg-info")]
        else:
            continue
        headers = _dist_headers(child / "METADATA")
        name = headers.get("name", "")
        version = headers.get("version", "")
        if not name:
            name, _, fallback = stem.rpartition("-")
            if not name:
                name = stem
            else:
                version = version or fallback
        if name:
            found[normalise_name(name)] = version
    return dict(sorted(found.items()))


def _vendor_contents(directory: Path | None) -> set[str]:
    """Importable top-level names in a vendor dir (mirrors ``cli.vendor_contents``)."""
    if directory is None or not directory.is_dir():
        return set()
    names: set[str] = set()
    for child in directory.iterdir():
        name = child.name
        if name.endswith((".dist-info", ".egg-info")):
            names.add(normalise_name(name.split("-")[0]))
        elif child.is_dir() and not name.startswith("__pycache__"):
            names.add(normalise_name(name))
        elif child.suffix in (".pyd", ".so"):
            names.add(normalise_name(child.stem.split(".")[0]))
    return names


def _available_packages(directory: Path | None) -> dict[str, str]:
    available = vendor_versions(directory)
    for name in _vendor_contents(directory):
        available.setdefault(normalise_name(name), "")
    return available


def requirement_errors(
    plugin_id: str,
    requires: Iterable[str],
    vendor: Path | None,
) -> list[MetadataError]:
    """Declared requirements that the vendor tree cannot satisfy."""
    available = _available_packages(vendor)
    errors: list[MetadataError] = []
    for requirement in requires:
        try:
            name, spec = parse_requirement(requirement)
        except ValueError as exc:
            errors.append(
                MetadataError(
                    FAILURE_METADATA,
                    f"[deps].requires entry {requirement!r}: {exc}",
                    plugin_id=plugin_id,
                )
            )
            continue
        key = normalise_name(name)
        if key not in available:
            errors.append(
                MetadataError(
                    FAILURE_DEP_MISSING,
                    f"requires {requirement!r} but the vendor directory does not provide it",
                    plugin_id=plugin_id,
                )
            )
            continue
        version = available[key]
        if spec and version:
            try:
                if not satisfies(version, spec):
                    errors.append(
                        MetadataError(
                            FAILURE_DEP_MISSING,
                            f"requires {requirement!r} but vendored version is {version}",
                            plugin_id=plugin_id,
                        )
                    )
            except ValueError as exc:
                errors.append(
                    MetadataError(
                        FAILURE_METADATA,
                        f"[deps].requires entry {requirement!r}: {exc}",
                        plugin_id=plugin_id,
                    )
                )
    return errors


def vendor_conflict_map(
    packages: Iterable[PluginPackage],
) -> dict[str, tuple[MetadataError, ...]]:
    """Cross-package ``dep-conflict`` errors, keyed by plugin id."""
    package_list = list(packages)
    versions = {package.id: vendor_versions(package.vendor) for package in package_list}
    names: set[str] = set()
    for snapshot in versions.values():
        names.update(snapshot)
    conflicts: dict[str, list[MetadataError]] = {}
    for name in sorted(names):
        entries = [(pid, snapshot[name]) for pid, snapshot in versions.items() if name in snapshot]
        if len(entries) < 2:
            continue
        if len({version for _, version in entries}) < 2:
            continue
        detail = ", ".join(f"{pid} {version or '?'}" for pid, version in entries)
        message = f"{name} is vendored at different versions: {detail}"
        for pid, _ in entries:
            conflicts.setdefault(pid, []).append(MetadataError(FAILURE_DEP_CONFLICT, message, plugin_id=pid))
    return {pid: tuple(errors) for pid, errors in conflicts.items()}
