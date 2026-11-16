"""Layer 1 of the TUI runtime: services, hooks, and schedule wiring."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

from deeppyro.hooks import SESSION_CREATED
from deeppyro.tui.runtime_state import _RuntimeState
from deeppyro.tui.runtime_support import await_maybe, logger


class ServiceLayer(_RuntimeState):
    """Layer 1 of the TUI runtime: services, hooks, and schedule wiring."""

    def configure_user_questions(self, handler: Any | None = None) -> None:
        """Route ask_user_question requests to a handler.

        No-op when the bundle did not load the user_questions service or when
        the service object has no configure hook, so older bundles keep working.
        """
        service = self.user_questions
        if service is None:
            return
        configure = getattr(service, "configure", None)
        if callable(configure):
            configure(handler)

    def configure_schedule_notice(self, handler: Callable[[str], None] | None = None) -> None:
        """Route scheduled-message notices to the UI."""
        self.schedule_notice = handler

    async def stop_schedule(self) -> None:
        """Stop the schedule timer and any background agent it spawned."""
        service = self.schedule
        if service is not None:
            stop = getattr(service, "stop", None)
            if callable(stop):
                # shutdown must not mask an exception
                with contextlib.suppress(Exception):
                    await await_maybe(stop())
        tasks = list(self._schedule_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            # shutdown must not mask an exception
            with contextlib.suppress(Exception):
                await asyncio.gather(*tasks, return_exceptions=True)
        self._schedule_tasks.clear()

    async def open_schedule_session(self, session_id: str) -> Any:
        """Open a persisted session for a schedule delivery."""
        return self.store.open(session_id)

    def _service(self, name: str) -> Any | None:
        app = self.kernel_app
        if app is None:
            return None
        try:
            return app.service(name)
        except LookupError:
            return None

    async def _session_created(self, session: Any, *, cwd: str, preset: str | None) -> None:
        session_id = str(getattr(session, "session_id", "") or "")
        if not session_id:
            return
        self._created_sessions.add(session_id)
        await self._emit_hook(SESSION_CREATED, {"sessionId": session_id, "cwd": cwd, "preset": preset})

    async def _emit_hook(self, name: str, payload: dict[str, Any]) -> None:
        hooks = self._service("hooks")
        if hooks is None or not hooks.has(name):
            return
        try:
            await hooks.emit(name, payload)
        except Exception:  # noqa: BLE001 - hook listeners may raise anything and must not break the TUI
            logger.exception("hook %r dispatch failed", name)

    def _pin_permission(self, session: Any) -> None:
        """Seed a new session with the default permission preset and knobs."""
        service = self._service("permissionPresets")
        pin = getattr(service, "pin_initial", None)
        if not callable(pin) or session is None:
            return
        # pinning must not brick a session
        with contextlib.suppress(Exception):
            pin(session)
