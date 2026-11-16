"""DeepPyro terminal UI application.

The app is the composition root of the TUI. It owns the TuiSink, boots the
kernel-backed runtime (or receives a test double), forwards agent events to
the conversation widgets and routes user intent either to the agent or to the
slash-command layer.

Rendering never decides anything and the domain layers never know about
Textual: DeepPyroApp is the only place where both worlds meet.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from deeppyro import __version__
from deeppyro.tui.app_panels import PanelLayer
from deeppyro.tui.app_support import _HOOK_INVOKED_RECORD as _HOOK_INVOKED_RECORD
from deeppyro.tui.app_support import _HOOK_RESULT_RECORD as _HOOK_RESULT_RECORD
from deeppyro.tui.app_support import CUSTOM_WORKSPACE as CUSTOM_WORKSPACE
from deeppyro.tui.app_support import CUSTOM_WORKSPACE_LABEL as CUSTOM_WORKSPACE_LABEL
from deeppyro.tui.app_support import HOOK_TRACE_LIMIT as HOOK_TRACE_LIMIT
from deeppyro.tui.app_support import RuntimeFactory as RuntimeFactory
from deeppyro.tui.app_support import _emit_goal as _emit_goal
from deeppyro.tui.app_support import _goal_service as _goal_service
from deeppyro.tui.app_support import _hook_rows_from_records as _hook_rows_from_records
from deeppyro.tui.app_support import _normalize_hook_rows as _normalize_hook_rows
from deeppyro.tui.app_support import _record_field as _record_field
from deeppyro.tui.history import reasoning_from_session
from deeppyro.tui.screens.splash import SplashScreen
from deeppyro.tui.screens.thinking import ThinkingScreen
from deeppyro.tui.sink import TuiSink
from deeppyro.tui.slots import SlotRegistry
from deeppyro.tui.widgets.activity_bar import AGENTS, JOBS, PLAN, ActivityBar
from deeppyro.tui.widgets.agent_process import AgentProcess
from deeppyro.tui.widgets.conversation import ConversationView
from deeppyro.tui.widgets.plugin_slots import PluginStatusBar
from deeppyro.tui.widgets.prompt_area import PromptArea
from deeppyro.tui.widgets.status_bar import StatusBar


class DeepPyroApp(PanelLayer):
    """Full-screen terminal client for one DeepPyro session."""

    CSS_PATH = "theme.tcss"
    TITLE = "DeepPyro Harness"
    SUB_TITLE = "python"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("ctrl+c", "cancel_or_quit", "Interrupt / quit", show=False),
        Binding("ctrl+d", "quit", "Quit", show=True),
        Binding("ctrl+p", "focus_prompt", "Prompt", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        runtime_factory: RuntimeFactory | None = None,
        splash: bool = True,
    ) -> None:
        super().__init__()
        self._cwd = cwd
        self._provider = provider
        self._model = model
        self._runtime_factory = runtime_factory
        self._splash_enabled = splash
        self.runtime: Any = None
        self._sink = TuiSink(self.handle_event)
        self._prompt: PromptArea | None = None
        self._slots: SlotRegistry | None = None
        self._working = False
        self._phase = "idle"
        self._context_used = 0
        self._last_ctrl_c = 0.0
        self._ui: Any = None
        self._ui_timer: Any = None
        self._ui_status: dict[str, str] = {}

    # -- composition -----------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ActivityBar()
        yield ConversationView()
        yield StatusBar()
        yield PluginStatusBar()
        yield PromptArea()
        yield Footer()

    async def on_mount(self) -> None:
        self._prompt = self.query_one(PromptArea)
        self._prompt.set_commands_provider(self.command_list)
        if self._splash_enabled:
            self.push_screen(
                SplashScreen(
                    version=__version__,
                    cwd=str(self._cwd or Path.cwd()),
                    provider=self._provider or "",
                    model=self._model or "",
                ),
                callback=self._on_splash_closed,
            )
        else:
            self._prompt.focus()
        try:
            if self._runtime_factory is not None:
                self.runtime = await self._runtime_factory(self)
            else:
                from deeppyro.tui.runtime import TuiRuntime

                self.runtime = await TuiRuntime.boot(
                    sink=self._sink,
                    ask=self.ask_approval,
                    cwd=self._cwd,
                    provider=self._provider,
                    model=self._model,
                )
        except Exception as exc:  # noqa: BLE001 - a broken boot must stay visible
            self.conversation.add_error(f"failed to start: {type(exc).__name__}: {exc}")
            self._dismiss_splash()
            self.refresh_status()
            return
        self._configure_user_questions()
        self._configure_schedule()
        await self._start_schedule()
        self._bind_slots()
        self._bind_ui()
        self._update_splash()
        self._render_welcome()
        self._refresh_activity()
        self.set_interval(1.0, self._refresh_activity)
        self.refresh_status()

    async def on_unmount(self) -> None:
        self._unbind_ui()
        runtime = self.runtime
        if runtime is None:
            return
        # shutdown never raises into Textual
        with contextlib.suppress(Exception):
            await runtime.close()

    # -- splash ----------------------------------------------------------
    def _splash_screen(self) -> SplashScreen | None:
        for screen in self.screen_stack:
            if isinstance(screen, SplashScreen):
                return screen
        return None

    def _on_splash_closed(self, result: Any = None) -> None:
        if self._prompt is not None:
            self._prompt.focus()

    def _update_splash(self) -> None:
        screen = self._splash_screen()
        runtime = self.runtime
        if screen is None or runtime is None:
            return
        screen.set_runtime(runtime.provider_name, runtime.model, cwd=str(runtime.cwd))

    def _dismiss_splash(self) -> None:
        screen = self._splash_screen()
        if screen is not None:
            screen.action_dismiss_splash()

    @on(ActivityBar.SegmentClicked)
    def _on_activity_segment(self, event: ActivityBar.SegmentClicked) -> None:
        if event.segment == PLAN:
            self.run_worker(self._open_plan(), group="panel")
        elif event.segment == JOBS:
            self.run_worker(self._open_jobs(), group="panel")
        elif event.segment == AGENTS:
            self.run_worker(self._open_team(), group="panel")

    @on(AgentProcess.ThinkingRequested)
    def _on_thinking_requested(self, event: AgentProcess.ThinkingRequested) -> None:
        """Open the full-screen viewer when a collapsed step line is clicked."""
        self.push_screen(ThinkingScreen([event.reasoning]))

    async def thinking_command(self, argument: str) -> None:
        """Open the full-screen reasoning viewer (/thinking [turns])."""
        session = getattr(self.runtime, "session", None)
        turns: list[Any] = []
        if session is not None:
            spec = argument.strip().lower()
            if spec in ("", "current"):
                turns = list(reasoning_from_session(session, last_turns=1))
            elif spec in ("all", "*"):
                turns = list(reasoning_from_session(session, last_turns=None))
            else:
                try:
                    count = int(spec)
                except ValueError:
                    self.conversation.add_error("usage: /thinking [number of turns|all]")
                    return
                if count <= 0:
                    self.conversation.add_error("usage: /thinking [number of turns|all]")
                    return
                turns = list(reasoning_from_session(session, last_turns=count))
        if not turns:
            current = self.conversation.current_reasoning
            if current is not None and current.has_reasoning:
                turns = [current]
        if not turns:
            self.conversation.add_info("no reasoning recorded for this session yet")
            return
        try:
            await self.push_screen_wait(ThinkingScreen(turns))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            steps = sum(len(turn.steps) for turn in turns)
            self.conversation.add_info(
                f"reasoning available: {len(turns)} turn(s), {steps} step(s) (use /thinking)"
            )


def run_app(**kwargs: Any) -> None:
    """Create and run the app (used by the console-script entry point)."""
    DeepPyroApp(**kwargs).run()


__all__ = ["DeepPyroApp", "RuntimeFactory", "run_app"]
