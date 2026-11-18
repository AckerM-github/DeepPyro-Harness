"""Layer 1 of the TUI runtime: services, hooks, and schedule wiring."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from deeppyro.agent.native_factory import PromptFacts
from deeppyro.hooks import SESSION_CREATED, HookService
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.sandbox.policy import approval_policy_text, sandbox_policy_text
from deeppyro.tui.native_facts import native_view
from deeppyro.tui.runtime_state import _RuntimeState
from deeppyro.tui.runtime_support import _session_header_id, await_maybe, logger


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
        if getattr(self, "native", False):
            return self.sessions.native_open(session_id)
        return self.store.open(session_id)

    def _service(self, contract: type[Any]) -> Any | None:
        """Resolve one optional service contract (None when unavailable)."""
        app = self.kernel_app
        if app is None:
            return None
        try:
            return app.get(contract)
        except (AttributeError, LookupError):
            return None

    async def _session_created(self, session: Any, *, cwd: str, preset: str | None) -> None:
        session_id = _session_header_id(session)
        if not session_id:
            return
        self._created_sessions.add(session_id)
        await self._emit_hook(SESSION_CREATED, {"sessionId": session_id, "cwd": cwd, "preset": preset})

    async def _emit_hook(self, name: str, payload: dict[str, Any]) -> None:
        hooks = self._service(HookService)
        if hooks is None or not hooks.has(name):
            return
        try:
            await hooks.emit(name, payload)
        except Exception:  # noqa: BLE001 - hook listeners may raise anything and must not break the TUI
            logger.exception("hook %r dispatch failed", name)

    def _pin_permission(self, session: Any) -> None:
        """Seed a new session with the default permission preset and knobs."""
        if session is None:
            return
        if getattr(self, "native", False):
            self._pin_permission_native(session)
            return
        service = self._service(PermissionPresetsService)
        pin = getattr(service, "pin_initial", None)
        if not callable(pin):
            return
        # pinning must not brick a session
        with contextlib.suppress(Exception):
            pin(session)

    def _pin_permission_native(self, session: Any) -> None:
        """Seed a native session with the default sandbox and approval facts.

        Schema 1 has no permission/preset record: the named preset is derived
        from the sandbox mode and approval policy facts, so only those two are
        appended and only when the log does not carry them yet.
        """
        service = self._service(PermissionPresetsService)
        getter = getattr(service, "get", None)
        default = str(getattr(service, "default", "") or "workspace-write")
        if not callable(getter):
            return
        # pinning must not brick a session
        with contextlib.suppress(Exception):
            spec = cast(Any, getter(default))
            state = native_view(session).fold().state
            if state.sandbox_mode is None:
                session.append("sandbox/mode", {"mode": spec.sandbox})
            if state.permission_policy is None:
                session.append("approval/policy", {"policy": spec.approval})

    def _prompt_facts(self, session: Any) -> PromptFacts:
        """Prompt facts for one native session (cwd, model, policies).

        Lives on the lowest layer so both the session layer and the schedule
        layer can build native agent hosts from the same facts.
        """
        view = native_view(session)
        state = view.fold().state if view is not None else None
        cwd_value = getattr(state, "cwd", None) if state is not None else None
        header = getattr(view, "header", None)
        cwd = Path(str(cwd_value or getattr(header, "cwd", None) or self.cwd))
        approval = str(getattr(self.permission, "approval", "ask") or "ask")
        policy = getattr(self.permission, "policy", None)
        sandbox_text = sandbox_policy_text(policy) if policy is not None else ""
        # the permission preset lives on the status layer; this lowest layer
        # must stay importable without it, so resolve it through getattr.
        preset_getter = getattr(self, "permission_preset", None)
        preset_name = str(preset_getter()) if callable(preset_getter) else "custom"
        return PromptFacts(
            cwd=cwd,
            model=self.model,
            permission_preset=preset_name,
            approval_policy=approval,
            sandbox_policy_text=sandbox_text,
            approval_policy_text=approval_policy_text(approval),
        )
