"""Enable/disable state of installed packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro import paths
from deeppyro.plugins.install_commit import _manager, _manifest_bytes
from deeppyro.plugins.install_errors import InstallFailure, UnknownPlugin


@dataclass(frozen=True, slots=True)
class StateReport:
    plugin_id: str
    enabled: bool
    changed: bool


def set_enabled(
    plugin_id: str,
    enabled: bool,
    *,
    root: Path | None = None,
    manager: Any | None = None,
) -> StateReport:
    """Enable or disable an installed package via the existing manager."""
    from deeppyro.plugins.errors import PluginError, PluginNotFoundError

    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    managed = manager or _manager(plugin_root)
    before = _manifest_bytes(Path(managed.manifest_path))
    try:
        if enabled:
            managed.enable(plugin_id)
        else:
            managed.disable(plugin_id)
    except PluginNotFoundError as exc:
        raise UnknownPlugin(plugin_id) from exc
    except PluginError as exc:
        raise InstallFailure(f"could not update {plugin_id!r}: {exc}", rolled_back=True) from exc
    after = _manifest_bytes(Path(managed.manifest_path))
    return StateReport(plugin_id, enabled, before != after)
