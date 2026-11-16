"""Layer 0 of the DeepPyro TUI app: Base layer: shared app state and the small accessors every layer uses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.app import App

from deeppyro.api import DEFAULT_PRESET_ID
from deeppyro.tui.app_support import RuntimeFactory
from deeppyro.tui.screens.picker import PickerScreen
from deeppyro.tui.sink import TuiSink
from deeppyro.tui.slots import SlotRegistry
from deeppyro.tui.widgets.conversation import ConversationView
from deeppyro.tui.widgets.prompt_area import PromptArea


class AppBase(App[None]):
    """Base layer: shared app state and the small accessors every layer uses."""

    _cwd: str | Path | None
    _provider: str | None
    _model: str | None
    _runtime_factory: RuntimeFactory | None
    _splash_enabled: bool
    runtime: Any
    _sink: TuiSink
    _prompt: PromptArea | None
    _slots: SlotRegistry | None
    _working: bool
    _phase: str
    _context_used: int
    _last_ctrl_c: float
    _ui: Any
    _ui_timer: Any
    _ui_status: dict[str, str]

    # -- public accessors ------------------------------------------------
    @property
    def sink(self) -> TuiSink:
        return self._sink

    @property
    def conversation(self) -> ConversationView:
        return self.query_one(ConversationView)

    @property
    def busy(self) -> bool:
        return self._working

    # -- policy / approval / workspace commands --------------------------
    def _agent_preset(self) -> str:
        return str(getattr(self.runtime, "agent_preset", DEFAULT_PRESET_ID) or DEFAULT_PRESET_ID)

    def _agent_presentation(self) -> str:
        """Tool presentation of the active agent preset (native or ptc)."""
        value = getattr(self.runtime, "agent_presentation", "")
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - status display must never fail
                value = ""
        return str(value or "native")

    def _agent_preset_display(self) -> str:
        """Display name of the active agent preset, falling back to its id."""
        value = getattr(self.runtime, "agent_preset_display", "")
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - status display must never fail
                value = ""
        return str(value or "") or self._agent_preset()

    def _agent_mode(self) -> str:
        reader = getattr(self.runtime, "agent_mode", "default")
        try:
            return str(reader or "default")
        except Exception:  # noqa: BLE001 - status display must never fail
            return "default"

    def _current_permission(self) -> str:
        runtime = self.runtime
        reader = getattr(runtime, "permission_preset", None)
        if callable(reader):
            try:
                return str(reader())
            except Exception:
                self.log.debug("permission preset unavailable", exc_info=True)
        permission = getattr(runtime, "permission", None)
        return str(getattr(permission, "preset", "workspace-write"))

    def _current_approval(self) -> str:
        permission = getattr(self.runtime, "permission", None)
        return str(getattr(permission, "approval", "ask"))

    async def _pick(self, title: str, items: list[tuple[str, str, str]]) -> str | None:
        """Show a modal picker inside a worker; None outside a worker or on cancel."""
        if not items:
            return None
        try:
            return await self.push_screen_wait(PickerScreen(title, items))
        except Exception:  # noqa: BLE001 - push_screen_wait needs a worker
            return None
