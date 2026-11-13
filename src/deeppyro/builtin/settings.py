"""The settings service: describe, read and write plugin configuration.

Configuration edits are persisted as user manifest patches and only take
effect on the next boot; the service is a thin facade over SettingsStore so
plugins and agent tools can reach it through cordis (provide = "settings").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Service
from deeppyro.plugins.settings_store import SettingsStore


class SettingsPlugin(Service):
    """Service facade over deeppyro.plugins.settings_store.SettingsStore."""

    provide = "settings"
    plugin_id = "settings"
    version = "0.1.0"
    description = "plugin configuration store with CAS and secret masking"
    inject: ClassVar[list[str]] = []

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.store = SettingsStore()

    @property
    def revision(self) -> str:
        return self.store.revision

    def describe(self, plugin_id: str) -> dict[str, Any]:
        return self.store.describe(plugin_id)

    def read(self, plugin_id: str) -> dict[str, Any]:
        return self.store.read(plugin_id)

    def write(
        self,
        plugin_id: str,
        patch: Mapping[str, Any],
        expected_revision: str,
    ) -> dict[str, Any]:
        return self.store.write(plugin_id, patch, expected_revision)
