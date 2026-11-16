"""Layer 11 of the DeepPyro TUI app: Jobs, team, hooks and settings panel command layer."""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable
from typing import Any

from deeppyro.tui.app_goal import GoalLayer
from deeppyro.tui.app_support import HOOK_TRACE_LIMIT, _hook_rows_from_records, _normalize_hook_rows
from deeppyro.tui.screens.hook_trace import EMPTY_TEXT as HOOK_TRACE_EMPTY_TEXT
from deeppyro.tui.screens.hook_trace import HookTraceScreen, hook_trace_line
from deeppyro.tui.screens.jobs import JobsPanelScreen
from deeppyro.tui.screens.plugin_panel import open_plugin_panel
from deeppyro.tui.screens.slot_screen import open_slot_screen
from deeppyro.tui.screens.team import TaskBoardScreen, TeamPanelScreen
from deeppyro.tui.slots import SCREEN, SETTINGS, build_slot_context


class PanelLayer(GoalLayer):
    """Jobs, team, hooks and settings panel command layer."""

    def _job_rows(self) -> list[Any]:
        """Background jobs from the activity projection, else the live service."""
        state = self._activity_state()
        rows = list(getattr(state, "jobs", ()) or ())
        if rows:
            return rows
        jobs = getattr(self.runtime, "jobs", None) if self.runtime is not None else None
        lister = getattr(jobs, "list", None)
        if lister is None:
            return []
        try:
            return list(lister())
        except Exception:  # noqa: BLE001 - a broken registry must not crash the UI
            return []

    async def _open_jobs(self) -> None:
        """Open the background-jobs panel with the current snapshot."""
        rows = self._job_rows()
        try:
            await self.push_screen_wait(JobsPanelScreen(rows))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            if not rows:
                self.conversation.add_info("no background jobs")
                return
            for job in rows:
                line = (
                    f"{getattr(job, 'id', '?')}  {getattr(job, 'status', '?')}  {getattr(job, 'label', '')}"
                )
                self.conversation.add_info(line)

    async def jobs_command(self, argument: str = "") -> None:
        """Open the background-jobs panel (/jobs)."""
        await self._open_jobs()

    def _team_snapshot(self) -> tuple[list[Any], list[Any], str]:
        """Members, tasks and team id from the team service, tolerating absence."""
        team = getattr(self.runtime, "team", None) if self.runtime is not None else None
        members: list[Any] = []
        tasks: list[Any] = []
        team_id = ""
        if team is not None:
            for getter_name, target in (("members", members), ("tasks", tasks)):
                getter = getattr(team, getter_name, None)
                if not callable(getter):
                    continue
                read: Callable[[], Any] = getter
                # a broken store must not crash the UI
                with contextlib.suppress(Exception):
                    target.extend(read())
            team_id = str(getattr(team, "team_id", "") or "")
        return members, tasks, team_id

    async def _open_team(self, argument: str = "") -> None:
        """Open the team roster or the shared task board (/team [on|off|board])."""
        members, tasks, team_id = self._team_snapshot()
        if self._agent_mode() != "team" and not members and not tasks:
            self.conversation.add_info("agent team mode is off; use /team on to enable team tools")
            return
        spec = argument.strip().lower()
        if spec in ("board", "tasks", "task-board"):
            try:
                await self.push_screen_wait(TaskBoardScreen(tasks))
            except Exception:  # noqa: BLE001 - outside a worker there is no modal
                self.conversation.add_info(f"task board: {len(tasks)} task(s)")
            return
        if spec not in ("", "members", "team"):
            self.conversation.add_error("usage: /team [on|off|board]")
            return
        try:
            await self.push_screen_wait(TeamPanelScreen(members, tasks, team_id))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            if not members and not tasks:
                self.conversation.add_info("no team members or tasks yet")
                return
            self.conversation.add_info(f"team: {len(members)} member(s) · {len(tasks)} task(s)")

    async def team_command(self, argument: str = "") -> None:
        """Enter/leave team mode or open the roster/task board (/team)."""
        spec = argument.strip().lower()
        if spec in ("on", "enable", "start"):
            await self._team_mode(True)
            return
        if spec in ("off", "disable", "stop"):
            await self._team_mode(False)
            return
        await self._open_team(spec)

    async def _team_mode(self, enabled: bool) -> None:
        """Switch the current session between default and team mode."""
        if self._working:
            self.notify("cannot switch team mode during a turn", severity="warning")
            return
        try:
            selected = await self.runtime.set_team_mode(enabled)
        except Exception as exc:  # noqa: BLE001 - show the runtime/service message
            self.conversation.add_error(f"team mode switch failed: {exc}")
            return
        if enabled:
            self.conversation.add_info(
                "agent team mode on · lead has team tools; subagent delegation is hidden"
            )
        else:
            self.conversation.add_info(
                f"agent team mode off · preset {selected}; subagent delegation enabled"
            )
        self.refresh_status()

    def _session_query(self) -> Any:
        """The session-query service, when the kernel provides one."""
        runtime = self.runtime
        if runtime is None:
            return None
        query = getattr(runtime, "session_query", None)
        if query is not None:
            return query
        kernel_app = getattr(runtime, "kernel_app", None)
        service = getattr(kernel_app, "service", None)
        if not callable(service):
            return None
        try:
            return service("session_query")
        except Exception:  # noqa: BLE001 - the service is optional
            return None

    async def _hook_rows(self) -> list[dict[str, Any]]:
        """Paired hook rows for the current session, newest last."""
        runtime = self.runtime
        session = getattr(runtime, "session", None) if runtime is not None else None
        trace = getattr(session, "hook_trace", None)
        if callable(trace):
            # fall back to the raw records
            with contextlib.suppress(Exception):
                rows = trace(limit=HOOK_TRACE_LIMIT)
                if inspect.isawaitable(rows):
                    rows = await rows
                return _normalize_hook_rows(rows)
        session_id = str(getattr(session, "session_id", "") or "")
        query = self._session_query()
        trace = getattr(query, "hook_trace", None)
        if callable(trace) and session_id:
            # fall back to the raw records
            with contextlib.suppress(Exception):
                rows = trace(session_id, limit=HOOK_TRACE_LIMIT)
                if inspect.isawaitable(rows):
                    rows = await rows
                return _normalize_hook_rows(rows)
        return _hook_rows_from_records(session)

    async def hooks_command(self, argument: str = "") -> None:
        """Open the recent hook decisions of the current session (/hooks)."""
        rows = await self._hook_rows()
        if not rows:
            self.conversation.add_info(HOOK_TRACE_EMPTY_TEXT)
            return
        try:
            await self.push_screen_wait(HookTraceScreen(rows))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            for row in rows:
                self.conversation.add_info(hook_trace_line(row))

    async def compact(self) -> None:
        if self._working:
            self.notify("cannot compact during a turn", severity="warning")
            return
        changed = await self.runtime.compact_now()
        self.conversation.add_info("session compacted" if changed else "nothing to compact")
        self.refresh_status()

    def clear_conversation(self) -> None:
        self.conversation.clear()
        self._render_welcome()

    async def panel_command(self, argument: str) -> None:
        """Open a plugin-contributed panel by id (or the first one)."""
        registry = self._slots
        if registry is None:
            self.conversation.add_info("plugin panels are not available in this session")
            return
        screen = open_plugin_panel(
            self,
            registry,
            slot_id=argument.strip() or None,
            context=build_slot_context(self),
        )
        if screen is not None:
            return
        screen = open_slot_screen(
            self,
            registry,
            slot_id=argument.strip() or None,
            context=build_slot_context(self),
        )
        if screen is not None:
            return
        panels = [row.id for row in registry.list("panel")]
        screens = [row.id for row in registry.list(SCREEN)]
        if panels:
            self.conversation.add_info("plugin panels: " + ", ".join(panels))
        if screens:
            self.conversation.add_info("plugin screens: " + ", ".join(screens))
        if panels or screens:
            self.conversation.add_info("use /panel <id>")
        else:
            self.conversation.add_info("no plugin panels or screens are registered")

    async def settings_command(self, argument: str) -> None:
        """Open a plugin-contributed settings page by id, or list them."""
        registry = self._slots
        if registry is None:
            self.conversation.add_info("plugin settings are not available in this session")
            return
        pages = registry.settings_pages()
        if not pages:
            self.conversation.add_info("no plugin settings pages are registered")
            return
        wanted = argument.strip()
        if not wanted and len(pages) == 1:
            wanted = pages[0].id
        if wanted:
            screen = open_plugin_panel(
                self,
                registry,
                kind=SETTINGS,
                slot_id=wanted,
                context=build_slot_context(self),
            )
            if screen is not None:
                return
            self.conversation.add_info(f"no plugin settings page: {wanted}")
        labels = [f"{page.id} ({page.title})" if page.title else page.id for page in pages]
        self.conversation.add_info("plugin settings: " + ", ".join(labels))
        self.conversation.add_info("use /settings <id>")

    async def show_plugins(self) -> None:
        """Open the external plugin manager, or list kernel plugins as fallback."""
        # outside a worker or manager failure
        with contextlib.suppress(Exception):
            from deeppyro.tui.screens.plugins import PluginScreen

            manager = None
            service = getattr(self.runtime, "_service", None)
            if callable(service):
                plugin_manager = service("plugin_manager")
                manager = getattr(plugin_manager, "manager", None)
            health = None
            if manager is not None:
                try:
                    from deeppyro.tui.health import scan_health

                    health = scan_health()
                except Exception:  # noqa: BLE001 - health is optional detail
                    health = None
            await self.push_screen_wait(PluginScreen(manager, health=health))
            return
        kernel = getattr(self.runtime, "kernel_app", None)
        specs = list(getattr(kernel, "specs", []) or [])
        started = dict(getattr(kernel, "started", {}) or {})
        if not specs:
            self.conversation.add_info("no plugins loaded")
            return
        self.conversation.add_info("plugins:")
        for spec in specs:
            state = started.get(getattr(spec, "id", ""))
            state_name = getattr(state, "name", str(state) if state is not None else "off")
            disabled = " (disabled)" if getattr(spec, "disabled", False) else ""
            self.conversation.add_info(
                f"  {getattr(spec, 'id', '?')}  {getattr(spec, 'name', '')}  {state_name}{disabled}"
            )
