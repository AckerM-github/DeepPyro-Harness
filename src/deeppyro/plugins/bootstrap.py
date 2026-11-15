"""Plugin package bootstrap: scan ``~/.dpro/plugins`` and activate import paths.

A plugin package is a directory with a ``plugin.toml``. Boot has to make two
import roots visible before any composition row can resolve a target:

* the package directory itself (so ``import dpyro_im`` works), and
* its vendored dependency directory (``_vendor/cp313`` or ``_vendor``), so the
  plugin's third-party imports resolve inside the host interpreter.

This module is the single place that does that. It runs before the composition
is built (``deeppyro.kernel.app.create_app``), which removes the old implicit
coupling between "plugin-manager mounted" and "plugin importable": rows can now
resolve targets regardless of where ``plugin-manager`` sits in the ordering.

Scanning never imports plugin code and never raises for a broken package: every
failure is classified into a :class:`~deeppyro.plugins.metadata.MetadataError`
so the manager, the TUI panel and ``dpyro plugin doctor`` can show the same
diagnosis.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from deeppyro.paths import plugins_dir
from deeppyro.plugins.metadata import (
    FAILURE_METADATA,
    MetadataError,
    PluginMetadata,
    check_compat,
    find_vendor_dir,
    load_metadata,
)

log = logging.getLogger("deeppyro.plugins")

PLUGIN_MANIFEST_NAME = "plugin.toml"


@dataclass(frozen=True, slots=True)
class PluginPackage:
    """One directory under the plugin root, parsed but not imported."""

    id: str
    directory: Path
    metadata: PluginMetadata | None = None
    errors: tuple[MetadataError, ...] = ()
    vendor: Path | None = None

    @property
    def loadable(self) -> bool:
        """True when metadata parsed and every compatibility check passed."""
        return self.metadata is not None and not self.errors

    @property
    def target(self) -> str:
        return self.metadata.target if self.metadata is not None else ""

    @property
    def provides(self) -> tuple[str, ...]:
        return self.metadata.provides if self.metadata is not None else ()

    @property
    def version(self) -> str:
        return self.metadata.version if self.metadata is not None else ""

    @property
    def error(self) -> str:
        """First failure as ``"kind: message"``, or ``""`` when healthy."""
        if not self.errors:
            return ""
        first = self.errors[0]
        return f"{first.kind}: {first.message}"

    def failure_kind(self) -> str:
        return self.errors[0].kind if self.errors else ""


@dataclass
class Activation:
    """Result of making package import roots visible."""

    packages: list[PluginPackage] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    @property
    def loadable(self) -> list[PluginPackage]:
        return [package for package in self.packages if package.loadable]

    def get(self, plugin_id: str) -> PluginPackage | None:
        for package in self.packages:
            if package.id == plugin_id:
                return package
        return None

    def targets(self) -> dict[str, str]:
        return {package.id: package.target for package in self.loadable}


def plugin_root(root: str | Path | None = None) -> Path:
    return Path(root) if root is not None else plugins_dir()


def scan_plugin_packages(
    root: str | Path | None = None,
    *,
    known_slots: tuple[str, ...] | None = None,
) -> list[PluginPackage]:
    """Parse every ``plugin.toml`` under the plugin root, sorted by id.

    A directory without ``plugin.toml`` is ignored (legacy bundles, notes).
    A directory with a broken ``plugin.toml`` is returned with ``errors`` set so
    the problem is visible instead of silently dropped.
    """
    base = plugin_root(root)
    if not base.is_dir():
        return []
    packages: list[PluginPackage] = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or not (child / PLUGIN_MANIFEST_NAME).is_file():
            continue
        packages.append(_load_package(child, known_slots=known_slots))
    return sorted(packages, key=lambda package: package.id)


def _load_package(directory: Path, *, known_slots: tuple[str, ...] | None) -> PluginPackage:
    plugin_id = directory.name
    try:
        metadata = load_metadata(directory, plugin_id=plugin_id, known_slots=known_slots)
    except MetadataError as exc:
        return PluginPackage(id=plugin_id, directory=directory, errors=(exc,))
    except Exception as exc:  # noqa: BLE001 - never let one package break the scan
        failure = MetadataError(FAILURE_METADATA, f"{directory}: {exc}")
        return PluginPackage(id=plugin_id, directory=directory, errors=(failure,))
    errors: list[MetadataError] = list(check_compat(metadata))
    vendor = find_vendor_dir(metadata)
    return PluginPackage(
        id=metadata.id,
        directory=directory,
        metadata=metadata,
        errors=tuple(errors),
        vendor=vendor,
    )


def activation_paths(packages: list[PluginPackage]) -> list[str]:
    """Import roots in load order: package directory first, then its vendor dir."""
    paths: list[str] = []
    for package in packages:
        paths.append(str(package.directory))
        if package.vendor is not None:
            paths.append(str(package.vendor))
    return paths


def activate(
    packages: list[PluginPackage],
    *,
    path: list[str] | None = None,
) -> Activation:
    """Prepend the import roots of ``packages`` to ``sys.path`` (idempotent)."""
    target = sys.path if path is None else path
    result = Activation(packages=list(packages))
    entries = activation_paths(result.packages)
    present = set(target)
    added: list[str] = []
    for entry in reversed(entries):
        if entry in present:
            continue
        target.insert(0, entry)
        present.add(entry)
        added.append(entry)
    result.added = list(reversed(added))
    for entry in result.added:
        log.debug("plugin import root active: %s", entry)
    return result


def prepare(
    root: str | Path | None = None,
    *,
    known_slots: tuple[str, ...] | None = None,
) -> Activation:
    """Scan the plugin root and activate every package's import roots."""
    packages = scan_plugin_packages(root, known_slots=known_slots)
    return activate(packages)


def load_package(plugin_id: str, root: str | Path | None = None) -> PluginPackage:
    """Load one package by id; raises ``KeyError`` when it is not installed."""
    base = plugin_root(root)
    directory = base / plugin_id
    if not (directory / PLUGIN_MANIFEST_NAME).is_file():
        raise KeyError(f"plugin {plugin_id!r} is not installed under {base}")
    return _load_package(directory, known_slots=None)


__all__ = [
    "PLUGIN_MANIFEST_NAME",
    "Activation",
    "PluginPackage",
    "activate",
    "activation_paths",
    "load_package",
    "plugin_root",
    "prepare",
    "scan_plugin_packages",
]
