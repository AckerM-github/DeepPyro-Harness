"""The presets service: named per-session agent compositions.

A preset is a scope label selected when a session is created. Tools and prompt
sections registered with scope=<preset id> are visible only to agents created
with that preset; unscoped contributions stay visible to every agent. Plugins
add presets through PluginContext.register_preset, so a bundle can ship its own
agent composition without patching the core.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from deeppyro.api import PluginDependencyError, PresetSpec, Service

DEFAULT_PRESET_ID = "default"


class PresetsPlugin(Service):
    """Registry of agent presets; always offers the host-wide default."""

    provide = "presets"
    plugin_id = "presets"
    version = "0.1.0"
    description = "agent preset registry"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self._presets: dict[str, PresetSpec] = {
            DEFAULT_PRESET_ID: PresetSpec(
                id=DEFAULT_PRESET_ID,
                name="Default",
                description="host-wide tools and prompt sections",
                order=0,
                plugin_id="presets",
            )
        }

    def add(self, preset: PresetSpec, *, plugin_id: str = "") -> Any:
        """Register one preset; the returned callable removes it."""
        if not isinstance(preset, PresetSpec):
            raise PluginDependencyError(f"expected PresetSpec, got {type(preset).__name__}")
        preset_id = (preset.id or "").strip()
        if not preset_id:
            raise PluginDependencyError("preset id must not be empty")
        current = self._presets.get(preset_id)
        if current is not None:
            raise PluginDependencyError(
                f"preset {preset_id!r} is already registered by {current.plugin_id!r}"
            )
        stored = replace(preset, id=preset_id, plugin_id=preset.plugin_id or plugin_id)
        self._presets[preset_id] = stored

        def remove() -> None:
            self._presets.pop(preset_id, None)

        return remove

    def get(self, preset_id: str | None) -> PresetSpec | None:
        if not preset_id:
            return None
        return self._presets.get(preset_id)

    def resolve(self, preset_id: str | None = None) -> PresetSpec:
        """Return the named preset or the default one; unknown ids raise."""
        wanted = (preset_id or DEFAULT_PRESET_ID).strip() or DEFAULT_PRESET_ID
        spec = self._presets.get(wanted)
        if spec is None:
            raise PluginDependencyError(f"unknown agent preset: {wanted!r}")
        return spec

    def default(self) -> PresetSpec:
        return self._presets[DEFAULT_PRESET_ID]

    def all(self) -> list[PresetSpec]:
        return sorted(self._presets.values(), key=lambda item: (item.order, item.id))

    def ids(self) -> list[str]:
        return [item.id for item in self.all()]


__all__ = ["DEFAULT_PRESET_ID", "PresetsPlugin"]
