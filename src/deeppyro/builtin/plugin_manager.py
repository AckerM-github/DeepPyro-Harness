"""The plugin manager service: external plugin and user bundle lifecycle.

The service wraps PluginManager so other plugins and agent tools can reach it
through the cordis service layer (provide = "plugin_manager") instead of
importing the manager directly.
"""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Service
from deeppyro.plugins import (
    BundleInfo,
    PluginInfo,
    PluginManager,
    ValidationResult,
)


class PluginManagerPlugin(Service):
    """Service facade over deeppyro.plugins.PluginManager."""

    provide = "plugin_manager"
    plugin_id = "plugin-manager"
    version = "0.1.0"
    description = "external plugin and user bundle lifecycle manager"
    inject: ClassVar[list[str]] = []

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.manager = PluginManager()

    # -- external plugins --------------------------------------------------

    @property
    def errors(self) -> tuple[str, ...]:
        return self.manager.errors

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.manager.warnings

    def refresh(self) -> list[PluginInfo]:
        return self.manager.refresh()

    def list(self) -> list[PluginInfo]:
        return self.manager.list()

    def validate(self, plugin_id: str) -> ValidationResult:
        return self.manager.validate(plugin_id)

    def enable(self, plugin_id: str) -> PluginInfo:
        return self.manager.enable(plugin_id)

    def disable(self, plugin_id: str) -> PluginInfo:
        return self.manager.disable(plugin_id)

    def remove(self, plugin_id: str) -> None:
        self.manager.remove(plugin_id)

    # -- bundles -----------------------------------------------------------

    def list_bundles(self) -> list[BundleInfo]:
        return self.manager.list_bundles()

    def install_bundle(self, source: str, *, force: bool = False) -> BundleInfo:
        return self.manager.install_bundle(source, force=force)

    def remove_bundle(self, bundle_id: str) -> BundleInfo:
        return self.manager.remove_bundle(bundle_id)

    def set_bundle(self, bundle_id: str, enabled: bool) -> BundleInfo:
        return self.manager.set_bundle(bundle_id, enabled)
