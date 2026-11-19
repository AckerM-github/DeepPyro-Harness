"""The four-column NiceGUI shell.

The shell is the only place that knows the whole page: the rail, the session
sidebar, the stage (chat or settings) and the inspector. Pages compose it; the
chat and settings modules never reach across into another region.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from nicegui import ui

from deeppyro.web.ui import theme
from deeppyro.web.ui.bridge import WebUiBridge, bridge_error

if TYPE_CHECKING:
    from deeppyro.web.ui.palette import CommandPalette


class ShellView:
    """Render one shell and keep its session catalogue and inspector fresh."""

    def __init__(
        self,
        bridge: WebUiBridge,
        *,
        session_id: str | None = None,
        open_palette: bool = False,
        navigate: Callable[[str], None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._session_id = session_id
        self._open_palette = open_palette
        self._navigate = navigate or (lambda path: ui.navigate.to(path))
        self._bootstrap: dict[str, Any] = {}
        self._sessions: list[dict[str, Any]] = []
        self._session_list: ui.element | None = None
        self._search = ""
        self._inspector: ui.element | None = None
        self._inspector_busy = False
        self._palette: CommandPalette | None = None
        self._palette_task: asyncio.Task[None] | None = None
        self._new_dialog: ui.dialog | None = None
        self._root: ui.element | None = None

    # -- lifecycle ---------------------------------------------------------

    async def mount(self, parent: ui.element) -> None:
        from deeppyro.web.ui.chat import ChatView
        from deeppyro.web.ui.palette import CommandPalette

        theme.install_head()
        self._bootstrap = await self._bridge.bootstrap()
        self._sessions = list(self._bootstrap.get("sessions") or [])
        self._root = parent
        with parent:
            with ui.element("div").classes("dp-app"):
                self._render_rail()
                self._render_sidebar()
                stage = ui.element("div").classes("dp-stage")
                active = self._session_id or self._bridge.active_session_id
                await ChatView(
                    self._bridge,
                    active,
                    on_session_change=self._navigate,
                ).mount(stage)
                self._render_inspector()
            self._render_new_session_dialog()
        self._palette = CommandPalette(self._bridge, on_navigate=self._navigate)
        await self._palette.mount(parent)
        if self._open_palette:
            await self._palette.open()
        ui.timer(1.6, self._refresh_inspector)

    # -- rail --------------------------------------------------------------

    def _render_rail(self) -> None:
        with ui.element("aside").classes("dp-rail"):
            with ui.element("a").classes("dp-brand-mark").on("click", lambda: self._navigate("/")):
                ui.image("/brand/cyborg-mark.svg").classes("dp-brand-image")
            ui.element("div").classes("dp-rail-sep")
            self._rail_button("sessions", "Sessions", lambda: self._navigate("/"))
            self._rail_button("commands", "Commands", self._open_command_palette)
            self._rail_button("plugins", "Plugins", lambda: self._navigate("/settings/plugins"))
            ui.element("div").classes("dp-rail-spacer")
            self._rail_button("add", "New session", self._open_new_session)
            ui.element("div").classes("dp-rail-sep")
            self._rail_button("settings", "Settings", lambda: self._navigate("/settings"))
            theme.avatar("Lin", colour="#6d5bd0", size=34).classes("dp-rail-avatar")

    def _rail_button(self, icon_name: str, label: str, handler: Callable[[], Any]) -> None:
        button = (
            ui.button(icon=theme.ICONS.get(icon_name, icon_name), on_click=handler)
            .props("flat round dense")
            .classes("dp-rail-button")
        )
        button.tooltip(label)
        button.props(f'aria-label="{label}"')

    # -- sidebar -----------------------------------------------------------

    def _render_sidebar(self) -> None:
        defaults = self._bootstrap.get("defaults") or {}
        with ui.element("aside").classes("dp-sidebar"):
            with ui.element("div").classes("dp-sidebar-head"):
                with ui.element("div").classes("dp-brand"):
                    ui.label("DeepPyro").classes("dp-brand-name")
                    ui.label(str(defaults.get("cwd") or "local runtime")).classes("dp-brand-sub")
                ui.button(icon="more_horiz").props("flat round dense").classes("dp-icon-btn")

            self._search_input = (
                ui.input(placeholder="Find a session")
                .props("outlined dense clearable borderless")
                .classes("dp-search")
            )
            self._search_input.on_value_change(self._on_search)

            self._nav_row("Sessions", "sessions", len(self._sessions), lambda: None)
            self._nav_row(
                "Commands",
                "commands",
                len(self._bootstrap.get("commands") or []),
                self._open_command_palette,
            )
            self._nav_row(
                "Plugins",
                "plugins",
                len(self._bootstrap.get("plugins") or []),
                lambda: self._navigate("/settings/plugins"),
            )

            with ui.element("div").classes("dp-sidebar-section"):
                with ui.element("div").classes("dp-section-head"):
                    ui.label("SESSIONS").classes("dp-section-title")
                    ui.button(icon="add").props("flat round dense").classes("dp-icon-btn").on(
                        "click", self._open_new_session
                    )
                self._session_list = ui.element("div").classes("dp-session-list")
                self._paint_sessions()

            with ui.element("div").classes("dp-sidebar-spacer"):
                pass
            with ui.element("div").classes("dp-user-bar"):
                theme.avatar("Lin", colour="#6d5bd0", size=32)
                with ui.element("div").classes("dp-user-text"):
                    ui.label("Lin").classes("dp-user-name")
                    ui.label("local runtime").classes("dp-user-sub")
                ui.button(icon="settings").props("flat round dense").classes("dp-icon-btn").on(
                    "click", lambda: self._navigate("/settings")
                )

    def _nav_row(
        self,
        label: str,
        icon_name: str,
        count: int,
        handler: Callable[[], Any],
    ) -> None:
        row = ui.element("div").classes("dp-nav").on("click", handler)
        with row:
            theme.icon(icon_name, size="17px")
            ui.label(label).classes("dp-nav-label")
            ui.label(str(count)).classes("dp-nav-count")

    def _on_search(self, event: Any) -> None:
        self._search = str(event.value or "").strip().lower()
        self._paint_sessions()

    def _paint_sessions(self) -> None:
        if self._session_list is None:
            return
        self._session_list.clear()
        rows = self._sessions
        if self._search:
            rows = [
                row
                for row in rows
                if self._search in str(row.get("title") or "").lower()
                or self._search in str(row.get("cwd") or "").lower()
                or self._search in str(row.get("session_id") or "").lower()
            ]
        with self._session_list:
            if not rows:
                ui.label("No sessions match.").classes("dp-empty-small")
                return
            for row in rows[:60]:
                self._session_row(row)

    def _session_row(self, row: dict[str, Any]) -> None:
        session_id = str(row.get("session_id") or "")
        active = session_id == (self._session_id or self._bridge.active_session_id)
        title = str(row.get("title") or "").strip() or "(untitled)"
        with (
            ui.element("div")
            .classes("dp-session" + (" is-active" if active else ""))
            .on("click", lambda _=None, sid=session_id: self._navigate(f"/c/{sid}"))
        ):
            ui.element("span").classes("dp-dot" + (" is-live" if active else ""))
            with ui.element("div").classes("dp-session-text"):
                ui.label(title).classes("dp-session-title")
                meta = f"{row.get('turns', 0)} turns · {row.get('preset') or 'standard'}"
                if not active:
                    meta += " · read only"
                ui.label(meta).classes("dp-session-meta")

    # -- inspector ---------------------------------------------------------

    def _render_inspector(self) -> None:
        self._inspector = ui.element("aside").classes("dp-inspector")
        self._paint_inspector_loading()

    def _paint_inspector_loading(self) -> None:
        if self._inspector is None:
            return
        self._inspector.clear()
        with self._inspector:
            ui.label("SESSION").classes("dp-inspector-title")
            ui.label("Loading session facts…").classes("dp-empty-small")

    async def _refresh_inspector(self) -> None:
        if self._inspector is None or self._inspector_busy:
            return
        session_id = self._session_id or self._bridge.active_session_id
        if not session_id:
            self._paint_inspector_empty()
            return
        self._inspector_busy = True
        try:
            timeline, facts = await asyncio.gather(
                self._bridge.timeline(session_id),
                self._bridge.facts(session_id),
            )
        finally:
            self._inspector_busy = False
        self._paint_inspector(timeline, facts)

    def _paint_inspector_empty(self) -> None:
        if self._inspector is None:
            return
        self._inspector.clear()
        with self._inspector:
            ui.label("SESSION").classes("dp-inspector-title")
            with ui.element("div").classes("dp-empty-card"):
                theme.icon("sessions", size="20px")
                ui.label("No session is attached.").classes("dp-empty-title")
                ui.button("New session", on_click=self._open_new_session).props("flat dense no-caps").classes(
                    "dp-link-btn"
                )

    def _paint_inspector(self, timeline: dict[str, Any], facts: dict[str, Any]) -> None:
        if self._inspector is None:
            return
        state = facts.get("state") or {}
        info = facts.get("info") or {}
        self._inspector.clear()
        with self._inspector:
            title = str(timeline.get("title") or info.get("title") or "(untitled)")
            with ui.element("div").classes("dp-inspector-head"):
                ui.label("SESSION").classes("dp-inspector-title")
                theme.badge(
                    "LIVE" if timeline.get("active") else "READ ONLY",
                    tone="ok" if timeline.get("active") else "muted",
                )
            with ui.element("div").classes("dp-inspector-session"):
                theme.avatar(title, size=34)
                with ui.element("div").classes("dp-inspector-session-text"):
                    ui.label(title).classes("dp-inspector-name")
                    ui.label(str(timeline.get("session_id") or "")).classes("dp-mono dp-faint")

            if timeline.get("error"):
                ui.label(str(timeline["error"])).classes("dp-error-text")
                return

            self._kv("MODEL", str(timeline.get("model") or "-"))
            self._kv("PROVIDER", str(timeline.get("provider") or "-"))
            self._kv("PRESET", str(timeline.get("preset") or "-"))
            self._kv("TURNS", str(_turn_count(timeline, state)))
            used = int(timeline.get("context_tokens") or 0)
            window = int(timeline.get("context_window") or 0)
            with ui.element("div").classes("dp-kv"):
                ui.label("CONTEXT").classes("dp-kv-key")
                ui.label(f"{used:,} / {window:,}" if window else "-").classes("dp-kv-value")
            if window:
                ui.linear_progress(value=min(used / window, 1.0), show_value=False).props(
                    "rounded size=6px color=cyan"
                ).classes("dp-context-bar")

            ui.element("div").classes("dp-inspector-rule")
            usage = timeline.get("usage") or {}
            with ui.element("div").classes("dp-inspector-subhead"):
                ui.label("USAGE").classes("dp-inspector-subtitle")
                ui.label("TOKENS").classes("dp-inspector-subtitle")
            self._kv("Input", f"{int(usage.get('input_tokens') or 0):,}")
            self._kv("Output", f"{int(usage.get('output_tokens') or 0):,}")
            self._kv("Cached input", f"{int(usage.get('cached_input_tokens') or 0):,}")

            if state:
                self._render_permissions(state)
                self._render_plan(state)
                self._render_tasks(state)
                self._render_goal(state)

            ui.element("div").classes("dp-inspector-rule")
            with ui.element("div").classes("dp-shortcuts"):
                ui.label("SHORTCUTS").classes("dp-inspector-subtitle")
                self._shortcut("Command palette", "⌘K")
                self._shortcut("Settings", "⌘,")
                self._shortcut("New session", "⌘N")

    def _render_permissions(self, state: dict[str, Any]) -> None:
        ui.element("div").classes("dp-inspector-rule")
        ui.label("ACCESS").classes("dp-inspector-subtitle")
        self._kv("Permission", str(state.get("permission_policy") or "-"))
        self._kv("Sandbox", str(state.get("sandbox_mode") or "-"))
        self._kv("Working dir", str(state.get("cwd") or "-"))

    def _render_plan(self, state: dict[str, Any]) -> None:
        todos = list(state.get("todos") or [])
        if not todos:
            return
        ui.element("div").classes("dp-inspector-rule")
        with ui.element("div").classes("dp-inspector-subhead"):
            ui.label("PLAN").classes("dp-inspector-subtitle")
            ui.label(
                f"{sum(1 for item in todos if item.get('status') == 'completed')} / {len(todos)}"
            ).classes("dp-inspector-subtitle")
        for item in todos[:10]:
            done = item.get("status") == "completed"
            with ui.element("div").classes("dp-todo"):
                theme.icon(
                    "check" if done else "chevron", size="14px", colour="#3ddc97" if done else "#5c6675"
                )
                ui.label(str(item.get("content") or "")).classes(
                    "dp-todo-text" + (" is-done" if done else "")
                )

    def _render_tasks(self, state: dict[str, Any]) -> None:
        tasks = list(state.get("tasks") or [])
        if not tasks:
            return
        ui.element("div").classes("dp-inspector-rule")
        ui.label("TASKS").classes("dp-inspector-subtitle")
        for task in tasks[:8]:
            with ui.element("div").classes("dp-task"):
                ui.label(str(task.get("status") or "open")).classes("dp-task-status")
                ui.label(str(task.get("objective") or task.get("task_id") or "")).classes("dp-task-text")

    def _render_goal(self, state: dict[str, Any]) -> None:
        goal = state.get("goal") or {}
        if not goal:
            return
        ui.element("div").classes("dp-inspector-rule")
        with ui.element("div").classes("dp-inspector-subhead"):
            ui.label("GOAL").classes("dp-inspector-subtitle")
            ui.label(str(goal.get("phase") or "")).classes("dp-inspector-subtitle")
        with ui.element("div").classes("dp-goal"):
            ui.label(str(goal.get("objective") or "")).classes("dp-goal-text")
            ui.label(f"round {state.get('rounds_started', 0)} / {goal.get('max_goal_rounds', 0)}").classes(
                "dp-faint"
            )

    def _kv(self, key: str, value: str) -> None:
        with ui.element("div").classes("dp-kv"):
            ui.label(key).classes("dp-kv-key")
            ui.label(value).classes("dp-kv-value")

    def _shortcut(self, label: str, key: str) -> None:
        with ui.element("div").classes("dp-shortcut"):
            ui.label(label).classes("dp-shortcut-label")
            ui.label(key).classes("dp-kbd")

    # -- dialogs -----------------------------------------------------------

    def _open_command_palette(self) -> None:
        if self._palette is not None:
            self._palette_task = asyncio.create_task(self._palette.open())

    def _open_new_session(self) -> None:
        if self._new_dialog is not None:
            self._new_dialog.open()

    def _render_new_session_dialog(self) -> None:
        presets = {item["id"]: item["name"] for item in (self._bootstrap.get("presets") or [])}
        default_preset = "standard" if "standard" in presets else (next(iter(presets), ""))
        recent: list[str] = []
        for row in self._sessions:
            cwd = str(row.get("cwd") or "")
            if cwd and cwd not in recent:
                recent.append(cwd)
        with ui.dialog() as dialog, ui.card().classes("dp-dialog-card dp-new-session"):
            self._new_dialog = dialog
            with ui.element("div").classes("dp-dialog-head"):
                with ui.element("div"):
                    ui.label("New session").classes("dp-dialog-title")
                    ui.label("Each session keeps the working directory it starts in.").classes(
                        "dp-dialog-sub"
                    )
                ui.button(icon="close", on_click=dialog.close).props("flat round dense").classes(
                    "dp-icon-btn"
                )
            cwd = (
                ui.input("Working directory", value=self._bridge.default_cwd)
                .props("outlined dense")
                .classes("dp-form-input")
            )
            title = ui.input("Title (optional)").props("outlined dense").classes("dp-form-input")
            preset = (
                ui.select(presets, value=default_preset, label="Preset")
                .props("outlined dense")
                .classes("dp-form-input")
                if presets
                else None
            )
            recent_row = ui.element("div").classes("dp-recent-row")
            with recent_row:
                ui.label("RECENT").classes("dp-field-note")
                for path in recent[:4]:
                    ui.button(path, on_click=lambda _=None, p=path: cwd.set_value(p)).props(
                        "flat dense no-caps"
                    ).classes("dp-recent-chip")
            error = ui.label("").classes("dp-error-text")
            with ui.element("div").classes("dp-dialog-actions"):
                ui.button("Cancel", on_click=dialog.close).props("flat dense no-caps").classes("dp-ghost-btn")
                submit = (
                    ui.button("Start session")
                    .props("unelevated dense no-caps")
                    .classes("dp-primary-btn")
                    .style(theme.PRIMARY_BUTTON_STYLE)
                )

            async def create() -> None:
                error.set_text("")
                submit.disable()
                try:
                    result = await self._bridge.create(
                        cwd=str(cwd.value or "").strip() or None,
                        title=str(title.value or "").strip() or None,
                        preset=str(preset.value) if preset is not None and preset.value else None,
                    )
                except Exception as exc:  # noqa: BLE001 - surface the server's message
                    error.set_text(bridge_error(exc))
                    submit.enable()
                    return
                self._navigate(f"/c/{result['session_id']}")

            submit.on("click", create)


def _turn_count(timeline: dict[str, Any], state: dict[str, Any]) -> int:
    """Count completed user turns for the inspector.

    A native session does not store a turn counter: one user message opens one
    turn, so the transcript is the authority. The turns field and the goal
    round count remain fallbacks for older payloads.
    """
    turns = timeline.get("turns")
    if isinstance(turns, int) and not isinstance(turns, bool):
        return turns
    messages = timeline.get("messages")
    if isinstance(messages, list):
        return sum(1 for message in messages if isinstance(message, dict) and message.get("role") == "user")
    rounds = state.get("rounds_started")
    return rounds if isinstance(rounds, int) and not isinstance(rounds, bool) else 0


__all__ = ["ShellView"]
