"""Layer 10 of the DeepPyro TUI app: Goal and plan command layer."""

from __future__ import annotations

from deeppyro.goal import GoalError
from deeppyro.tui.app_models import ModelLayer
from deeppyro.tui.app_support import _emit_goal, _goal_service
from deeppyro.tui.format import context_percent
from deeppyro.tui.screens.plan import PlanScreen, plan_summary
from deeppyro.tui.session_view import GOAL_USAGE, goal_summary_line


class GoalLayer(ModelLayer):
    """Goal and plan command layer."""

    def show_status(self) -> None:
        runtime = self.runtime
        usage = runtime.session_usage()
        session = getattr(runtime, "session", None)
        records = len(getattr(session, "records", []) or [])
        permission = getattr(runtime, "permission", None)
        sandbox_mode = getattr(getattr(permission, "policy", None), "mode", None)
        policy = self._current_permission()
        if sandbox_mode:
            policy = f"{policy} · sandbox {sandbox_mode}"
        has_key = getattr(runtime, "has_api_key", None)
        key_state = "n/a"
        if callable(has_key):
            try:
                key_state = "api key ok" if has_key(runtime.provider_name) else "missing api key"
            except Exception:  # noqa: BLE001 - status must never fail
                key_state = "n/a"
        lines = [
            f"session  {runtime.session_id}",
            f"provider {runtime.provider_name}  ({key_state})",
            "providers " + ", ".join(self._provider_names()),
            f"model    {runtime.provider_name}/{runtime.model}",
            f"cwd      {runtime.cwd}",
            f"preset   {self._agent_preset_display()}  ({self._agent_presentation()} · {self._agent_mode()} mode)",
            f"policy   {policy}",
            f"tokens   input {usage['input']}  output {usage['output']}  cached {usage['cached']}",
            f"context  {context_percent(self._context_used, runtime.context_window)}% of {runtime.context_window}",
            f"records  {records}  turns {getattr(session, 'turn_count', lambda: 0)()}",
            f"phase    {self._phase}",
        ]
        goal_view = None
        goal_service = _goal_service(runtime)
        if goal_service is not None:
            try:
                goal_view = goal_service.view(session)
            except Exception:  # noqa: BLE001 - /status must never fail
                goal_view = None
        summary = goal_summary_line(goal_view)
        if summary:
            lines.append(summary)
        for line in lines:
            self.conversation.add_info(line)

    async def _open_plan(self) -> None:
        """Open the plan viewer with the current todo snapshot."""
        state = self._activity_state()
        todos = tuple(getattr(state, "todos", ()) or ())
        if not todos:
            self.conversation.add_info("no plan yet")
            return
        try:
            await self.push_screen_wait(PlanScreen(todos))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            self.conversation.add_info("plan: " + plan_summary(todos))

    async def plan_command(self, argument: str = "") -> None:
        """Open the plan viewer (/plan)."""
        await self._open_plan()

    async def goal_command(self, argument: str = "") -> None:
        """Show or manage the current session goal (/goal)."""
        runtime = getattr(self, "runtime", None)
        service = _goal_service(runtime)
        session = getattr(runtime, "session", None)
        if service is None:
            self.conversation.add_error("goal service is not available in this session")
            return
        if session is None:
            self.conversation.add_error("no active session")
            return
        try:
            view = service.view(session)
        except Exception as exc:  # noqa: BLE001 - reading a goal must not break the UI
            self.conversation.add_error(f"could not read the goal: {exc}")
            return
        raw = (argument or "").strip()
        if not raw:
            _emit_goal(self.conversation, view)
            return
        verb, _, rest = raw.partition(" ")
        verb = verb.lower()
        rest = rest.strip()
        if verb not in {"edit", "pause", "resume", "complete", "clear"}:
            verb, rest = "", raw
        if verb == "edit":
            if not rest:
                self.conversation.add_info(GOAL_USAGE)
                return
            if view is None:
                _emit_goal(self.conversation, view)
                return
        elif verb:
            if rest:
                self.conversation.add_info(GOAL_USAGE)
                return
            if view is None:
                _emit_goal(self.conversation, view)
                return
        try:
            if verb == "edit" and view is not None:
                view = await service.edit(session, view.ref, objective=rest)
            elif verb == "pause" and view is not None:
                view = await service.pause(session, view.ref)
            elif verb == "resume" and view is not None:
                view = await service.resume(session, view.ref)
                service.arm(session)
            elif verb == "complete" and view is not None:
                view = await service.complete(session, view.ref)
            elif verb == "clear" and view is not None:
                await service.clear(session, view.ref)
                self.conversation.add_info("Goal cleared.")
                self.refresh_status()
                return
            else:
                view = await service.create(session, raw)
        except GoalError as exc:
            if exc.code == "GOAL_ALREADY_EXISTS":
                current = service.view(session)
                goal = getattr(current, "goal", None)
                phase = str(getattr(goal, "phase", "active"))
                self.conversation.add_error(
                    f"A goal is already {phase}. Use /goal edit <objective> to change it "
                    "or /goal clear before replacing it."
                )
            elif exc.code == "GOAL_NOT_FOUND":
                _emit_goal(self.conversation, None)
            else:
                self.conversation.add_error(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - command errors stay in the log
            self.conversation.add_error(f"goal command failed: {exc}")
            return
        _emit_goal(self.conversation, view)
        self.refresh_status()
