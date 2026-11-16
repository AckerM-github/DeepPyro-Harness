"""Import-level validation of an installed or staged package."""

from __future__ import annotations

import contextlib
import importlib
import sys
from pathlib import Path

from deeppyro.plugins.install_requirements import normalise_name, parse_requirement
from deeppyro.plugins.metadata import (
    FAILURE_IMPORT,
    REMEDIATION,
    MetadataError,
    PluginMetadata,
    find_vendor_dir,
)


def _purge_modules(module_name: str) -> None:
    """Forget a module and its submodules so validation imports from disk again."""
    prefix = f"{module_name}."
    for key in [name for name in sys.modules if name == module_name or name.startswith(prefix)]:
        sys.modules.pop(key, None)


def _plugin_base() -> type | None:
    try:
        from deeppyro.api import Plugin
    except Exception:  # noqa: BLE001 - the API may be unavailable during bootstrap
        return None
    return Plugin if isinstance(Plugin, type) else None


def _declared_import_names(metadata: PluginMetadata) -> frozenset[str]:
    names: set[str] = set()
    for requirement in metadata.deps.requires:
        try:
            name, _ = parse_requirement(requirement)
        except ValueError:
            continue
        names.add(normalise_name(name.split(".")[0]))
    return frozenset(names)


def _import_error(metadata: PluginMetadata, message: str) -> MetadataError:
    return MetadataError(
        FAILURE_IMPORT,
        message,
        plugin_id=metadata.id,
        remediation=REMEDIATION.get(FAILURE_IMPORT),
    )


def target_errors(
    metadata: PluginMetadata,
    directory: Path,
    *,
    defer_missing: bool = False,
) -> tuple[MetadataError, ...]:
    """Check that ``directory`` really provides the declared plugin class.

    The directory and its vendor tree are put on ``sys.path`` while importing;
    ``sys.path`` and ``sys.modules`` are restored afterwards. With
    ``defer_missing`` a missing top-level module that matches a declared
    requirement is not an error yet: dependencies are installed after preflight
    and the check runs again once they are available.
    """
    module_name = metadata.module.strip()
    attribute = metadata.class_name.strip()
    if not module_name or not attribute:
        return (_import_error(metadata, "plugin.toml declares no module/class target"),)
    base_dir = Path(directory)
    vendor = find_vendor_dir(metadata.with_directory(base_dir))
    entries = [base_dir] + ([vendor] if vendor is not None else [])
    inserted: list[str] = []
    for entry in reversed(entries):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)
            inserted.append(text)
    _purge_modules(module_name)
    try:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            own = missing == module_name or module_name.startswith(f"{missing}.")
            deferred = (
                defer_missing
                and not own
                and normalise_name(missing.split(".")[0]) in _declared_import_names(metadata)
            )
            if deferred:
                return ()
            return (_import_error(metadata, f"cannot import {module_name!r}: {exc}"),)
        except Exception as exc:  # noqa: BLE001 - any import failure is a finding
            return (_import_error(metadata, f"cannot import {module_name!r}: {exc}"),)
        try:
            target = getattr(module, attribute)
        except AttributeError:
            return (_import_error(metadata, f"module {module_name!r} has no attribute {attribute!r}"),)
        if not isinstance(target, type):
            return (_import_error(metadata, f"{module_name}.{attribute} is not a class"),)
        plugin_base = _plugin_base()
        if plugin_base is not None and not issubclass(target, plugin_base):
            return (
                _import_error(
                    metadata,
                    f"{module_name}.{attribute} does not subclass deeppyro.api.Plugin",
                ),
            )
        return ()
    finally:
        for text in inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(text)
        _purge_modules(module_name)
