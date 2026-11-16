"""User-manifest diagnostics for installed plugin packages."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterable
from pathlib import Path

from deeppyro import paths
from deeppyro.plugins.install_requirements import validate_plugin_id
from deeppyro.plugins.metadata import FAILURE_METADATA, REMEDIATION, MetadataError, python_tag

FAILURE_MANIFEST_DANGLING = "manifest-dangling"

MANIFEST_REMEDIATION = (
    "remove the row from the user manifest or reinstall the package with 'dpyro plugin add <id>'"
)


def _manifest_error(plugin_id: str, message: str) -> MetadataError:
    return MetadataError(
        FAILURE_MANIFEST_DANGLING,
        message,
        plugin_id=plugin_id,
        remediation=MANIFEST_REMEDIATION,
    )


def _insert_paths(entries: Iterable[Path]) -> list[str]:
    inserted: list[str] = []
    for entry in entries:
        text = str(entry)
        if entry.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
            inserted.append(text)
    return inserted


def _consistent_manifest(plugin_root: Path, manifest_path: Path) -> bool:
    """Only diagnose a manifest that belongs to the scanned plugin root."""
    return manifest_path.parent == plugin_root.parent


def manifest_issues(root: Path | None = None) -> tuple[MetadataError, ...]:
    """Manifest rows whose package directory is gone or whose target cannot import."""
    from deeppyro.kernel.manifest import ManifestError, load_manifest, resolve_plugin_name

    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    manifest_path = paths.plugins_manifest_path()
    if not _consistent_manifest(plugin_root, manifest_path):
        return ()
    try:
        specs = load_manifest(manifest_path)
    except (OSError, ManifestError) as exc:
        return (
            MetadataError(
                FAILURE_METADATA,
                f"cannot read plugin manifest {manifest_path}: {exc}",
                remediation=REMEDIATION.get(FAILURE_METADATA),
            ),
        )
    issues: dict[str, MetadataError] = {}
    for spec in specs:
        plugin_id = spec.id
        if plugin_id in issues:
            continue
        directory = plugin_root / plugin_id
        if validate_plugin_id(plugin_id) is not None or not (directory / "plugin.toml").is_file():
            issues[plugin_id] = _manifest_error(
                plugin_id,
                f"manifest lists {plugin_id!r} but {directory} is not an installed plugin package",
            )
            continue
        if not spec.name:
            issues[plugin_id] = _manifest_error(
                plugin_id,
                f"manifest row {plugin_id!r} declares no plugin target",
            )
            continue
        vendor = directory / "_vendor" / python_tag()
        inserted = _insert_paths([directory, vendor, directory / "_vendor"])
        try:
            resolve_plugin_name(spec.name)
        except Exception as exc:  # noqa: BLE001 - any import failure is a finding
            issues[plugin_id] = _manifest_error(
                plugin_id,
                f"cannot import target {spec.name!r}: {exc}",
            )
        finally:
            for text in inserted:
                with contextlib.suppress(ValueError):
                    sys.path.remove(text)
    return tuple(issues[key] for key in sorted(issues))
