"""The settings service: describe, read and write plugin configuration.

Configuration edits are persisted as user manifest patches. Non-volatile
fields take effect on the next boot; fields declared in the plugin class
volatile attribute are applied to the mounted plugin through the kernel
service when it exposes apply_plugin_config, and the write result reports
applied_live/restart_required accordingly. The service is a thin facade over
SettingsStore so plugins and agent tools can reach it through cordis
(provide = "settings").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Service
from deeppyro.plugins.settings_store import APPLY_LIVE, APPLY_NEXT_BOOT, SettingsStore


def _kernel_service(ctx: Any) -> Any:
    """Look up the optional kernel service without requiring it to exist."""
    getter = getattr(ctx, "get", None)
    if not callable(getter):
        return None
    try:
        return getter("kernel")
    except Exception:  # noqa: BLE001 - live apply is best effort
        return None


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
        """Persist a patch, then live-apply volatile fields when possible."""
        written = self.store.write(plugin_id, patch, expected_revision)
        written["applied_live"] = False
        volatile = set(written.get("volatile_fields", ()))
        wants_live = any(key in volatile for key in written.get("changed", ()))
        if not wants_live:
            return written
        kernel = _kernel_service(self.ctx)
        apply_config = getattr(kernel, "apply_plugin_config", None)
        if not callable(apply_config):
            return written
        try:
            applied = bool(apply_config(plugin_id, self.store.effective_config(plugin_id)))
        except Exception:  # noqa: BLE001 - a failed live apply must not undo the persisted edit
            applied = False
        if not applied:
            return written
        written["applied_live"] = True
        remaining = [key for key in written.get("changed", ()) if key not in volatile]
        if remaining:
            written["restart_required"] = True
            written["apply"] = APPLY_NEXT_BOOT
        else:
            written["restart_required"] = False
            written["apply"] = APPLY_LIVE
        return written
