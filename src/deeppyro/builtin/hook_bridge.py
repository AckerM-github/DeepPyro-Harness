"""Builtin hook bridge: republish hook dispatches on the cordis event bus.

The bridge is the transport half of the P2-1 hook surface: HookJournalPlugin
writes the session log, and this plugin publishes the same lifecycle as
hook/invoked and hook/result cordis events. A listener subscribes to the event
stream and filters by the HookEventScope this-argument.

The plugin is stateless beyond its registration. The hook service isolates
observer failures, so a broken listener cannot change a hook result, and the
kernel disposes the observer when the plugin fiber unloads.
"""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.kernel.hook_bridge import HookBridge

__all__ = ["HookBridgePlugin"]


class HookBridgePlugin(Plugin):
    """Bridge HookService observations onto the cordis event bus."""

    plugin_id = "hook-bridge"
    version = "0.1.0"
    description = "republish runtime hook dispatches as cordis events"
    inject: ClassVar[list[str]] = ["hooks"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self._bridge: HookBridge | None = None

    def setup(self, ctx: Any, config: Any) -> None:
        """Register the bridge as a hook observer for this plugin fiber."""
        hooks = ctx.service("hooks")
        self._bridge = HookBridge(ctx.emit)
        ctx.effect(lambda: hooks.add_observer(self._bridge), label="hook-bridge")
