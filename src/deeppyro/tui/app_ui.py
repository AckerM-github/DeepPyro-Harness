"""Layer 4 of the DeepPyro TUI app: Plugin UI slot/action routing layer."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

from textual.notifications import SeverityLevel

from deeppyro.tui.app_approval import ApprovalLayer
from deeppyro.tui.screens.plugin_panel import open_plugin_panel
from deeppyro.tui.screens.slot_screen import open_slot_screen
from deeppyro.tui.slots import build_slot_context, resolve_slot_registry
from deeppyro.tui.ui import (
    ADD_ROW,
    NOTIFY,
    OPEN_PANEL,
    OPEN_SCREEN,
    REFRESH,
    SET_STATUS,
    UiAction,
    resolve_ui_service,
)
from deeppyro.tui.widgets.plugin_slots import PluginStatusBar


class UiLayer(ApprovalLayer):
    """Plugin UI slot/action routing layer."""

    def _bind_slots(self) -> None:
        """Attach the optional `slots` service to the plugin chrome."""
        self._slots = resolve_slot_registry(self.runtime)
        if self._slots is None:
            return
        # optional chrome never blocks boot
        with contextlib.suppress(Exception):
            bar = self.query_one(PluginStatusBar)
            bar.bind(self._slots, lambda: build_slot_context(self))

    def _bind_ui(self) -> None:
        """Attach the optional `ui` service and drain its queue on this thread."""
        ui = resolve_ui_service(self.runtime)
        if ui is None:
            return
        try:
            ui.bind(self)
            self._ui = ui
            self._ui_timer = self.set_interval(0.1, self._drain_ui)
        except Exception:  # noqa: BLE001 - optional chrome never blocks boot
            self._ui = None
            self._ui_timer = None

    def _drain_ui(self) -> None:
        """Deliver queued plugin actions; always runs on the Textual thread."""
        ui = self._ui
        if ui is None:
            return
        # one drain failure cannot break the timer
        with contextlib.suppress(Exception):
            ui.drain()

    def _unbind_ui(self) -> None:
        """Stop the drain timer and detach before the runtime closes."""
        timer = self._ui_timer
        self._ui_timer = None
        if timer is not None:
            # a dead timer is already stopped
            with contextlib.suppress(Exception):
                timer.stop()
        ui = self._ui
        self._ui = None
        if ui is None:
            return
        for step in (ui.unbind, ui.clear):
            # shutdown is best effort
            with contextlib.suppress(Exception):
                step()

    def handle_ui_action(self, action: UiAction) -> None:
        """Handle one `ui` action on the Textual thread (the drain backend)."""
        payload = action.payload
        kind = action.kind
        if kind == NOTIFY:
            self._notify_ui(payload)
            return
        if kind == OPEN_SCREEN:
            self._open_ui_screen(payload)
            return
        if kind == OPEN_PANEL:
            self._open_ui_panel(payload)
            return
        if kind == SET_STATUS:
            self._set_ui_status(payload)
            return
        if kind == ADD_ROW:
            self._add_ui_row(payload)
            return
        if kind == REFRESH:
            self.refresh_status()
            return
        self.notify(f"unknown plugin ui action: {kind}", severity="warning")

    def _notify_ui(self, payload: Mapping[str, Any]) -> None:
        level = str(payload.get("level") or "info").lower()
        severities: dict[str, SeverityLevel] = {"warning": "warning", "error": "error"}
        severity = severities.get(level, "information")
        text = str(payload.get("text") or "").strip()
        if text:
            self.notify(text, severity=severity)

    def _open_ui_screen(self, payload: Mapping[str, Any]) -> None:
        registry = self._slots
        if registry is None:
            self.notify("plugin screens are not available in this session", severity="warning")
            return
        slot_id = str(payload.get("slot_id") or "").strip() or None
        screen = open_slot_screen(self, registry, slot_id=slot_id, context=build_slot_context(self))
        if screen is None:
            self.notify(f"no plugin screen: {slot_id or 'default'}", severity="warning")

    def _open_ui_panel(self, payload: Mapping[str, Any]) -> None:
        registry = self._slots
        if registry is None:
            self.notify("plugin panels are not available in this session", severity="warning")
            return
        slot_id = str(payload.get("slot_id") or "").strip() or None
        screen = open_plugin_panel(self, registry, slot_id=slot_id, context=build_slot_context(self))
        if screen is None:
            self.notify(f"no plugin panel: {slot_id or 'default'}", severity="warning")

    def _set_ui_status(self, payload: Mapping[str, Any]) -> None:
        slot_id = str(payload.get("id") or "").strip()
        if not slot_id:
            return
        text = str(payload.get("text") or "").strip()
        if text:
            self._ui_status[slot_id] = text
        else:
            self._ui_status.pop(slot_id, None)
        self._refresh_plugin_status()

    def _add_ui_row(self, payload: Mapping[str, Any]) -> None:
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        if str(payload.get("level") or "info").lower() == "error":
            self.conversation.add_error(text)
        else:
            self.conversation.add_info(text)
