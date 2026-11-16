"""Layer 1 of the DeepPyro TUI app: Status bar and activity rendering layer."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from deeppyro.tui.app_base import AppBase
from deeppyro.tui.slots import build_slot_context
from deeppyro.tui.widgets.activity_bar import ActivityBar
from deeppyro.tui.widgets.plugin_slots import PluginStatusBar
from deeppyro.tui.widgets.status_bar import StatusBar


class StatusLayer(AppBase):
    """Status bar and activity rendering layer."""

    # -- status ----------------------------------------------------------
    def refresh_status(self) -> None:
        self._refresh_plugin_status()
        try:
            status = self.query_one(StatusBar)
        except Exception:  # noqa: BLE001 - composition not ready yet
            return
        runtime = self.runtime
        if runtime is None:
            status.show(
                session_id="-",
                provider="boot",
                model="starting",
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                context_window=0,
                phase="starting",
            )
            return
        usage = runtime.session_usage()
        window = int(getattr(runtime, "context_window", 0) or 0)
        context_reader = getattr(runtime, "context_tokens", None)
        if callable(context_reader):
            read: Callable[[], Any] = context_reader
            context_used = int(read())
        else:
            context_used = usage["input"]
        status.show(
            session_id=runtime.session_id,
            provider=runtime.provider_name,
            model=runtime.model,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cached_tokens=usage["cached"],
            context_window=window,
            phase=self._phase,
            preset=self._current_permission(),
            approval=self._current_approval(),
            context_tokens=context_used,
        )
        suffix = " · team" if self._agent_mode() == "team" else ""
        self.sub_title = f"{runtime.provider_name}/{runtime.model}{suffix}"
        # Never steal focus from an overlay (splash, picker, approval modal).
        if self._prompt is not None and len(self.screen_stack) <= 1:
            self._prompt.focus()

    def _refresh_plugin_status(self) -> None:
        """Re-render plugin status items; the line hides itself when empty."""
        try:
            bar = self.query_one(PluginStatusBar)
        except Exception:  # noqa: BLE001 - composition not ready yet
            return
        bar.refresh_items(build_slot_context(self))

    # -- activity bar ----------------------------------------------------
    def _activity_state(self) -> Any:
        """Refresh and return the current activity projection, if available."""
        runtime = self.runtime
        service = getattr(runtime, "activity", None) if runtime is not None else None
        if service is None:
            return None
        try:
            return service.refresh(getattr(runtime, "session", None))
        except Exception:  # noqa: BLE001 - the bar is cosmetic chrome
            return None

    def _refresh_activity(self) -> None:
        """Render the plan/jobs/agents strip; hidden when every segment is empty."""
        state = self._activity_state()
        if state is None:
            return
        # composition not ready yet
        with contextlib.suppress(Exception):
            self.query_one(ActivityBar).update_state(state)
