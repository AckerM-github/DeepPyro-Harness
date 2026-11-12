"""Layered plugin manifests (DSH-style bundle + profile patch).

DSH ships a bundle plugin list and applies `cordis.patch.yml` on top: entries
are selected by `id`, may override `config`, may disable a plugin, or insert
new ones. This module reproduces that model with TOML (stdlib `tomllib`,
so the kernel keeps cordis-free dependencies):

.. code-block:: toml

    [[plugins]]
    id = "session"
    name = "deeppyro.plugins.session:SessionPlugin"
    config = { root = "C:/Users/me/sessions" }

    [[plugins]]
    id = "frontend"
    disabled = true

Fields:
- `id` (required, unique key for patching)
- `name` (required for new entries; `"module:Class"` or a registered alias)
- `config` (dict merged into lower layer's config)
- `disabled` (bool)
- `before` / `after` (placement hints for newly inserted entries)
"""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """Raised when a manifest entry or plugin reference is invalid."""


@dataclass(frozen=True)
class PluginSpec:
    """One plugin entry of the manifest."""

    id: str
    name: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    disabled: bool | None = None
    before: str | None = None
    after: str | None = None

    def resolve(self) -> type:
        """Import and return the plugin class/factory for `name`."""
        if not self.name:
            raise ManifestError(f"plugin {self.id!r} has no name and cannot be resolved")
        return resolve_plugin_name(self.name)

    def enabled(self) -> bool:
        return self.disabled is not True


# Module path -> plugin class attribute registered by the package itself, e.g.
# `register_alias("session", "deeppyro.plugins.session:SessionPlugin")`.
_ALIASES: dict[str, str] = {}


def register_alias(alias: str, target: str) -> None:
    """Register a short plugin name (used by the default manifest)."""
    if ":" not in target:
        raise ManifestError(f"alias target must be 'module:Class', got {target!r}")
    _ALIASES[alias] = target


def resolve_plugin_name(name: str) -> type:
    """Resolve `"module:Class"` or a registered alias to a plugin class."""
    target = _ALIASES.get(name, name)
    if ":" not in target:
        raise ManifestError(f"plugin reference {name!r} is not 'module:Class' and is not a registered alias")
    module_name, _, attr = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - surfaced to user
        raise ManifestError(f"cannot import plugin module {module_name!r}: {exc}") from exc
    try:
        obj = getattr(module, attr)
    except AttributeError as exc:
        raise ManifestError(f"module {module_name!r} has no attribute {attr!r}") from exc
    if not callable(obj):
        raise ManifestError(f"plugin target {target!r} is not callable")
    return obj


def _spec_from_entry(entry: dict[str, Any], *, origin: str) -> PluginSpec:
    if not isinstance(entry, dict):
        raise ManifestError(f"{origin}: each [[plugins]] entry must be a table")
    pid = entry.get("id")
    if not isinstance(pid, str) or not pid:
        raise ManifestError(f"{origin}: plugin entry is missing a non-empty 'id'")
    config = entry.get("config", {})
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ManifestError(f"{origin}: plugin {pid!r} config must be a table")
    name = entry.get("name")
    if name is not None and not isinstance(name, str):
        raise ManifestError(f"{origin}: plugin {pid!r} name must be a string")
    disabled = entry.get("disabled")
    if disabled is not None and not isinstance(disabled, bool):
        raise ManifestError(f"{origin}: plugin {pid!r} disabled must be a boolean")
    before = entry.get("before")
    after = entry.get("after")
    for key, value in (("before", before), ("after", after)):
        if value is not None and not isinstance(value, str):
            raise ManifestError(f"{origin}: plugin {pid!r} {key} must be a string")
    return PluginSpec(id=pid, name=name, config=dict(config), disabled=disabled, before=before, after=after)


def load_manifest(path: str | Path) -> tuple[PluginSpec, ...]:
    """Load one patch-layer manifest, or an empty tuple when the file is absent."""
    p = Path(path)
    if not p.is_file():
        return ()
    try:
        with p.open("rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{p}: invalid TOML: {exc}") from exc
    entries = doc.get("plugins", [])
    if not isinstance(entries, list):
        raise ManifestError(f"{p}: 'plugins' must be an array of tables ([[plugins]])")
    return tuple(_spec_from_entry(entry, origin=str(p)) for entry in entries)


def _insert_position(order: list[str], spec: PluginSpec) -> int | None:
    if spec.before is not None:
        try:
            return order.index(spec.before)
        except ValueError as exc:
            raise ManifestError(
                f"plugin {spec.id!r} wants to be inserted before unknown id {spec.before!r}"
            ) from exc
    if spec.after is not None:
        try:
            return order.index(spec.after) + 1
        except ValueError as exc:
            raise ManifestError(
                f"plugin {spec.id!r} wants to be inserted after unknown id {spec.after!r}"
            ) from exc
    return None


def merge_plugins(base: Iterable[PluginSpec], patch: Iterable[PluginSpec]) -> tuple[PluginSpec, ...]:
    """Apply patch entries on top of a base layer, in DSH patch semantics.

    - same `id`: config shallow-merged (patch wins), name/disabled overridden
      only when the patch sets them;
    - new `id`: inserted at the position given by `before`/`after`, else
      appended at the end.
    """
    specs: dict[str, PluginSpec] = {spec.id: spec for spec in base}
    order: list[str] = [spec.id for spec in list(base)]

    for entry in patch:
        current = specs.get(entry.id)
        if current is None:
            if entry.name is None:
                raise ManifestError(f"new plugin {entry.id!r} requires a 'name'")
            position = _insert_position(order, entry)
            specs[entry.id] = entry
            if position is None:
                order.append(entry.id)
            else:
                order.insert(position, entry.id)
            continue
        specs[entry.id] = replace(
            current,
            name=entry.name if entry.name is not None else current.name,
            config={**current.config, **entry.config},
            disabled=entry.disabled if entry.disabled is not None else current.disabled,
        )
    return tuple(specs[pid] for pid in order)


def resolve_specs(base: Iterable[PluginSpec], manifests: Iterable[str | Path] = ()) -> tuple[PluginSpec, ...]:
    """Merge the base layer with one or more patch-layer manifest files."""
    merged = tuple(base)
    for path in manifests:
        merged = merge_plugins(merged, load_manifest(path))
    return merged
