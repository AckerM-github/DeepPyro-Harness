"""Bundle lifecycle for user-installed bundles.

A bundle is a directory with bundle.toml and ordered plugin rows. Layer D lets
a running agent install such a directory under ~/.dpro/bundles/<id>/, list the
installed bundles, toggle their rows through the user patch manifest, and
remove them again. Nothing here calls pip: bundles are data, never installed
distributions.

Enable state uses the same mechanism as the rest of the composition: a user
manifest row with disabled = true patches the matching bundle row. Core rows
(the built-in bundle) are protected and can never be disabled or removed here.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path

from deeppyro import paths
from deeppyro.kernel.bundle import DEFAULT_PLUGINS
from deeppyro.kernel.composition import (
    Bundle,
    CompositionError,
    load_bundle,
    user_bundles_dir,
)
from deeppyro.kernel.manifest import ManifestError, PluginSpec

from .errors import (
    PluginError,
    PluginNotFoundError,
    PluginProtectionError,
    PluginRemovalError,
)
from .manifest_store import read_manifest, save_user_manifest
from .model import (
    BUNDLE_STATE_DISABLED,
    BUNDLE_STATE_FAILED,
    BUNDLE_STATE_LOADED,
    BUNDLE_STATE_OVERRIDDEN,
    BUNDLE_STATE_REMOVED,
    BundleInfo,
)

_BUNDLE_FILE = "bundle.toml"
_FORBIDDEN_ID_CHARS = ("/", "\\", ":", "\0")


def _effective_disabled(specs: Sequence[PluginSpec]) -> dict[str, bool]:
    """Last-wins disabled map, matching merge_plugins ordering."""
    disabled: dict[str, bool] = {}
    for spec in specs:
        disabled[spec.id] = spec.disabled is True
    return disabled


def _last_index(specs: Sequence[PluginSpec], plugin_id: str) -> int | None:
    for index in range(len(specs) - 1, -1, -1):
        if specs[index].id == plugin_id:
            return index
    return None


def _validate_bundle_id(bundle_id: str) -> str:
    value = bundle_id.strip()
    if not value:
        raise PluginError("bundle id must not be empty")
    if value in {".", ".."}:
        raise PluginError(f"bundle id {bundle_id!r} is not allowed")
    if any(char in value for char in _FORBIDDEN_ID_CHARS):
        raise PluginError(f"bundle id {bundle_id!r} must be a single path component")
    return value


class BundleStore:
    """Install, list, toggle and remove user bundles under ~/.dpro/bundles."""

    def __init__(
        self,
        *,
        manifest_path: str | Path | None = None,
        root: str | Path | None = None,
        bundle_ids: Iterable[str] | None = None,
    ) -> None:
        if manifest_path is None:
            self._manifest_path = paths.plugins_manifest_path()
        else:
            self._manifest_path = Path(manifest_path)
        if root is None:
            self._root = user_bundles_dir()
        else:
            self._root = Path(root)
        ids = bundle_ids if bundle_ids is not None else (spec.id for spec in DEFAULT_PLUGINS)
        self._bundle_ids = frozenset(ids)

    @property
    def manifest_path(self) -> Path:
        """The user patch manifest used for enable/disable patches."""
        return self._manifest_path

    @property
    def root(self) -> Path:
        """Directory scanned for installed bundles (one subdirectory each)."""
        return self._root

    @property
    def bundle_ids(self) -> frozenset[str]:
        """Plugin ids owned by the built-in bundle; those rows are protected."""
        return self._bundle_ids

    # -- public API --------------------------------------------------------

    def list(self) -> tuple[BundleInfo, ...]:
        """Snapshot of every installed bundle, including unreadable ones."""
        disabled = _effective_disabled(self._read_specs())
        scanned = self._scan()
        overridden = self._overridden_rows(scanned)
        return tuple(
            self._info(path, bundle, error, disabled, overridden.get(path, frozenset()))
            for path, bundle, error in scanned
        )

    def install(self, source: str | Path, *, force: bool = False) -> BundleInfo:
        """Copy a bundle directory (or bundle.toml) under the bundles root.

        The source is validated with kernel.composition.load_bundle before any
        file is copied, so malformed bundles never reach the user directory.
        Installation is staged in a temporary sibling directory and moved into
        place with os.replace; a failure leaves no partial tree behind.
        """
        source_path = Path(source).expanduser()
        bundle_path = source_path / _BUNDLE_FILE if source_path.is_dir() else source_path
        bundle = self._load(bundle_path)
        bundle_id = _validate_bundle_id(bundle.id)
        target = self._root / bundle_id
        if target.exists() and not force:
            raise PluginError(
                f"bundle {bundle_id!r} is already installed at {target}; pass force=True to replace it"
            )
        self._root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".install-{bundle_id}-", dir=self._root))
        try:
            copied = staging / "bundle"
            try:
                shutil.copytree(bundle_path.parent, copied)
            except OSError as exc:
                raise PluginError(f"cannot copy bundle {bundle_path}: {exc}") from exc
            try:
                if target.exists():
                    shutil.rmtree(target)
                os.replace(copied, target)
            except OSError as exc:
                raise PluginError(f"cannot install bundle {bundle_id!r} at {target}: {exc}") from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        installed = self._load(target / _BUNDLE_FILE)
        disabled = _effective_disabled(self._read_specs())
        return replace(
            self._info(target, installed, None, disabled, frozenset()),
            restart_required=True,
        )

    def set_enabled(self, bundle_id: str, enabled: bool) -> BundleInfo:
        """Enable or disable every row of an installed bundle.

        Writes disabled flags into the user manifest. Rows of the built-in
        bundle are protected: a user bundle patching a core row can still be
        installed, but toggling it cannot disable a core capability.
        """
        _path, bundle, error = self._find(bundle_id)
        if bundle is None:
            raise PluginError(f"bundle {bundle_id!r} cannot be toggled: {error}")
        row_ids = [row.id for row in bundle.rows]
        protected = [row_id for row_id in row_ids if row_id in self._bundle_ids]
        if protected:
            names = ", ".join(protected)
            raise PluginProtectionError(f"bundle {bundle_id!r} patches built-in plugin rows: {names}")
        specs = self._read_specs()
        original = list(specs)
        for row_id in row_ids:
            index = _last_index(specs, row_id)
            if enabled:
                if index is None:
                    continue
                current = specs[index]
                cleared = replace(current, disabled=None)
                if (
                    cleared.name is None
                    and not cleared.config
                    and cleared.before is None
                    and cleared.after is None
                ):
                    specs.pop(index)
                else:
                    specs[index] = cleared
            elif index is None:
                specs.append(PluginSpec(id=row_id, disabled=True))
            else:
                specs[index] = replace(specs[index], disabled=True)
        changed = specs != original
        if changed:
            self._write_specs(specs)
        refreshed = self._find(bundle_id)
        info = self._info(refreshed[0], refreshed[1], refreshed[2], _effective_disabled(specs), frozenset())
        return replace(info, restart_required=changed)

    def remove(self, bundle_id: str) -> BundleInfo:
        """Delete an installed bundle and its orphaned manifest rows.

        Manifest entries for row ids that no other remaining bundle (and no
        core row) provides are deleted before the directory is removed, so the
        next boot does not fail on an unknown plugin id. pip distributions are
        not touched because bundles are never installed distributions.
        """
        path, bundle, error = self._find(bundle_id)
        specs = self._read_specs()
        row_ids = tuple(row.id for row in bundle.rows) if bundle is not None else ()
        remaining = {
            row.id
            for other_path, other, _other_error in self._scan()
            if other is not None and other_path != path
            for row in other.rows
        }
        removable = {
            row_id for row_id in row_ids if row_id not in self._bundle_ids and row_id not in remaining
        }
        if removable:
            specs = [spec for spec in specs if spec.id not in removable]
            self._write_specs(specs)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise PluginRemovalError(f"cannot remove bundle directory {path}: {exc}") from exc
        if bundle is None:
            return BundleInfo(
                id=bundle_id,
                name=bundle_id,
                path=str(path / _BUNDLE_FILE),
                state=BUNDLE_STATE_REMOVED,
                enabled=False,
                error=error,
                restart_required=True,
            )
        info = self._info(path, bundle, None, _effective_disabled(specs), frozenset())
        return replace(info, state=BUNDLE_STATE_REMOVED, enabled=False, restart_required=True)

    # -- discovery helpers -------------------------------------------------

    def _read_specs(self) -> list[PluginSpec]:
        try:
            return list(read_manifest(self._manifest_path))
        except (ManifestError, OSError) as exc:
            raise PluginError(f"cannot read plugin manifest {self._manifest_path}: {exc}") from exc

    def _write_specs(self, specs: Sequence[PluginSpec]) -> None:
        save_user_manifest(self._manifest_path, tuple(specs))

    def _load(self, bundle_path: Path) -> Bundle:
        try:
            return load_bundle(bundle_path)
        except CompositionError as exc:
            raise PluginError(str(exc)) from exc

    def _scan(self) -> list[tuple[Path, Bundle | None, str | None]]:
        if not self._root.is_dir():
            return []
        scanned: list[tuple[Path, Bundle | None, str | None]] = []
        for path in sorted(self._root.iterdir(), key=lambda item: item.name):
            if not path.is_dir():
                continue
            bundle_path = path / _BUNDLE_FILE
            if not bundle_path.is_file():
                continue
            try:
                bundle = load_bundle(bundle_path)
            except CompositionError as exc:
                scanned.append((path, None, str(exc)))
            else:
                scanned.append((path, bundle, None))
        return scanned

    def _find(self, bundle_id: str) -> tuple[Path, Bundle | None, str | None]:
        scanned = self._scan()
        for entry in scanned:
            _path, bundle, _error = entry
            if bundle is not None and bundle.id == bundle_id:
                return entry
        for entry in scanned:
            if entry[0].name == bundle_id:
                return entry
        raise PluginNotFoundError(f"no user bundle named {bundle_id!r} under {self._root}")

    def _overridden_rows(
        self, scanned: Sequence[tuple[Path, Bundle | None, str | None]]
    ) -> dict[Path, frozenset[str]]:
        owners: dict[str, list[tuple[int, str, str]]] = {}
        for path, bundle, _error in scanned:
            if bundle is None:
                continue
            for row in bundle.rows:
                owners.setdefault(row.id, []).append((bundle.order, bundle.id, path.name))
        overridden: dict[Path, set[str]] = {path: set() for path, _bundle, _error in scanned}
        for path, bundle, _error in scanned:
            if bundle is None:
                continue
            for row in bundle.rows:
                winner = max(owners[row.id])
                if winner != (bundle.order, bundle.id, path.name):
                    overridden[path].add(row.id)
        return {path: frozenset(rows) for path, rows in overridden.items()}

    def _info(
        self,
        path: Path,
        bundle: Bundle | None,
        error: str | None,
        disabled: dict[str, bool],
        overridden: frozenset[str],
    ) -> BundleInfo:
        bundle_path = path / _BUNDLE_FILE
        if bundle is None:
            return BundleInfo(
                id=path.name,
                name=path.name,
                path=str(bundle_path),
                enabled=False,
                state=BUNDLE_STATE_FAILED,
                error=error or "",
            )
        row_ids = tuple(row.id for row in bundle.rows)
        disabled_rows = tuple(row_id for row_id in row_ids if disabled.get(row_id, False))
        overridden_rows = tuple(row_id for row_id in row_ids if row_id in overridden)
        if row_ids and len(disabled_rows) == len(row_ids):
            state = BUNDLE_STATE_DISABLED
        elif overridden_rows:
            state = BUNDLE_STATE_OVERRIDDEN
        else:
            state = BUNDLE_STATE_LOADED
        enabled = not (row_ids and len(disabled_rows) == len(row_ids))
        return BundleInfo(
            id=bundle.id,
            name=bundle.name,
            version=bundle.version,
            api_version=bundle.api_version,
            order=bundle.order,
            critical=bundle.critical,
            path=str(bundle_path),
            enabled=enabled,
            state=state,
            row_ids=row_ids,
            disabled_rows=disabled_rows,
            overridden_rows=overridden_rows,
        )


__all__ = ["BundleStore"]
