"""Layer 5 of the DeepPyro TUI app: Schedule-driven turn layer."""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable
from typing import Any

from deeppyro.tui.app_ui import UiLayer


class ScheduleLayer(UiLayer):
    """Schedule-driven turn layer."""

    def _configure_schedule(self) -> None:
        """Route scheduled-message notices into the conversation."""
        configure = getattr(self.runtime, "configure_schedule_notice", None)
        if callable(configure):
            configure(self._on_schedule_message)

    async def _start_schedule(self) -> None:
        """Start the schedule timer when the runtime loaded the service."""
        start = getattr(self.runtime, "start_schedule", None)
        if not callable(start):
            return
        # scheduling is best effort
        with contextlib.suppress(Exception):
            result = start()
            if inspect.isawaitable(result):
                await result

    def _on_schedule_message(self, text: str) -> None:
        """Show a scheduled user message and keep the busy indicator honest."""
        self.conversation.add_user(text)
        if self._working:
            return
        self._working = True
        self._phase = "thinking"
        self.refresh_status()
        self.run_worker(self._await_schedule_turn(), group="schedule")

    async def _await_schedule_turn(self) -> None:
        agent = getattr(self.runtime, "agent", None)
        wait = getattr(agent, "wait_for_idle", None)
        if callable(wait):
            read: Callable[[], Any] = wait
            # the turn already reports errors
            with contextlib.suppress(Exception):
                await read()
        if not getattr(self.runtime, "busy", False):
            self._working = False
            self._phase = "idle"
            self.refresh_status()
