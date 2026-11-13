"""DeepPyro terminal UI application.

The app is the composition root of the TUI. It owns the TuiSink, boots the
kernel-backed runtime (or receives a test double), forwards agent events to
the conversation widgets and routes user intent either to the agent or to the
slash-command layer.

Rendering never decides anything and the domain layers never know about
Textual: DeepPyroApp is the only place where both worlds meet.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from deeppyro import __version__
from deeppyro.sandbox.types import APPROVALS, PRESETS
from deeppyro.tui.commands import dispatch, help_text, live_commands
from deeppyro.tui.format import context_percent, short_session_id
from deeppyro.tui.history import process_map, reasoning_from_session
from deeppyro.tui.screens.approval import ALLOW_ALWAYS, ApprovalScreen
from deeppyro.tui.screens.jobs import JobsPanelScreen
from deeppyro.tui.screens.path_prompt import PathPromptScreen
from deeppyro.tui.screens.picker import PickerScreen
from deeppyro.tui.screens.plan import PlanScreen, plan_summary
from deeppyro.tui.screens.plugin_panel import open_plugin_panel
from deeppyro.tui.screens.question import QuestionScreen
from deeppyro.tui.screens.splash import SplashScreen
from deeppyro.tui.screens.team import TaskBoardScreen, TeamPanelScreen
from deeppyro.tui.screens.thinking import ThinkingScreen
from deeppyro.tui.sink import TuiSink
from deeppyro.tui.slots import (
    SlotRegistry,
    apply_conversation_slots,
    build_slot_context,
    resolve_slot_registry,
)
from deeppyro.tui.widgets.activity_bar import AGENTS, JOBS, PLAN, ActivityBar
from deeppyro.tui.widgets.agent_process import AgentProcess
from deeppyro.tui.widgets.conversation import ConversationView
from deeppyro.tui.widgets.plugin_slots import PluginStatusBar
from deeppyro.tui.widgets.prompt_area import PromptArea
from deeppyro.tui.widgets.status_bar import StatusBar
from deeppyro.user_questions.types import Answer, AskRequest

RuntimeFactory = Callable[["DeepPyroApp"], Awaitable[Any]]

#: Sentinel returned by the /cwd picker when the user wants a free-text path.
CUSTOM_WORKSPACE = "\x00custom-workspace"
CUSTOM_WORKSPACE_LABEL = "Enter path..."


class DeepPyroApp(App[None]):
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
        self._update_splash()
        self._render_welcome()
        self._refresh_activity()
        self.set_interval(1.0, self._refresh_activity)
        self.refresh_status()

    def _configure_user_questions(self) -> None:
        """Route ask_user_question requests to the TUI modal handler."""
        configure = getattr(self.runtime, "configure_user_questions", None)
        if callable(configure):
            configure(self.ask_question)

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
        try:
            result = start()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001, S110 - scheduling is best effort
            pass

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
            try:
                await wait()
            except Exception:  # noqa: BLE001, S110 - the turn already reports errors
                pass
        if not getattr(self.runtime, "busy", False):
            self._working = False
            self._phase = "idle"
            self.refresh_status()

    def _bind_slots(self) -> None:
        """Attach the optional `slots` service to the plugin chrome."""
        self._slots = resolve_slot_registry(self.runtime)
        if self._slots is None:
            return
        try:
            bar = self.query_one(PluginStatusBar)
            bar.bind(self._slots, lambda: build_slot_context(self))
        except Exception:  # noqa: BLE001, S110 - optional chrome never blocks boot
            pass

    async def on_unmount(self) -> None:
        runtime = self.runtime
        if runtime is None:
            return
        try:
            await runtime.close()
        except Exception:  # noqa: BLE001, S110 - shutdown never raises into Textual
            pass

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

    # -- agent events ----------------------------------------------------
    def handle_event(self, event: dict[str, Any]) -> None:
        """Render one agent event (called by TuiSink on the UI loop)."""
        kind = str(event.get("type") or "")
        try:
            conversation = self.conversation
            if kind == "text":
                conversation.stream_text(str(event.get("text") or ""))
            elif kind == "thinking":
                conversation.stream_thinking(str(event.get("text") or ""))
            elif kind == "step":
                conversation.begin_step(int(event.get("turn") or 0), int(event.get("step") or 0))
            elif kind == "tool_call":
                conversation.add_tool_call(
                    str(event.get("callId") or ""),
                    str(event.get("name") or ""),
                    str(event.get("arguments") or ""),
                )
            elif kind == "tool_result":
                conversation.add_tool_result(
                    str(event.get("callId") or ""),
                    bool(event.get("isError")),
                    str(event.get("text") or ""),
                )
            elif kind == "usage":
                self._context_used = int(event.get("input") or 0)
            elif kind == "final":
                conversation.finish_turn(str(event.get("text") or ""))
            elif kind == "error":
                conversation.finish_turn(None)
                conversation.add_error(str(event.get("message") or "unknown error"))
            elif kind == "status":
                self._phase = str(event.get("phase") or "idle")
            apply_conversation_slots(conversation, event, self._slots, context=build_slot_context(self))
        except Exception:  # noqa: BLE001, S110 - rendering must never kill a turn
            pass
        self._refresh_activity()
        self.refresh_status()

    # -- input -----------------------------------------------------------
    @on(PromptArea.Submitted)
    def _on_submit(self, event: PromptArea.Submitted) -> None:
        text = str(event.text or "").strip()
        if not text:
            return
        if self._working:
            self.notify("a turn is already running", severity="warning")
            return
        self.conversation.add_user(text)
        if text.startswith("/"):
            self.run_worker(self._run_command(text), group="command")
            return
        self._working = True
        self._phase = "thinking"
        self.refresh_status()
        self.run_worker(self._run_turn(text), group="turn", exclusive=True)

    async def _run_turn(self, text: str) -> None:
        self._sink.begin_turn()
        try:
            await self.runtime.agent.send(text)
        except Exception as exc:  # noqa: BLE001 - turn errors belong in the log
            self.conversation.add_error(f"{type(exc).__name__}: {exc}")
        finally:
            if getattr(self.runtime, "busy", False):
                self.run_worker(self._await_schedule_turn(), group="schedule")
            else:
                self._working = False
                self._phase = "idle"
                self.refresh_status()

    async def _run_command(self, line: str) -> None:
        try:
            handled = await dispatch(self, line)
        except Exception as exc:  # noqa: BLE001 - command errors stay in the log
            self.conversation.add_error(f"{type(exc).__name__}: {exc}")
            return
        if not handled:
            name = line.split(maxsplit=1)[0]
            self.conversation.add_error(f"unknown command: {name} (try /help)")

    # -- bindings --------------------------------------------------------
    def action_focus_prompt(self) -> None:
        if self._prompt is not None:
            self._prompt.focus()

    def action_quit(self) -> None:
        self.exit()

    def action_cancel_or_quit(self) -> None:
        now = time.monotonic()
        if self._working:
            self._last_ctrl_c = now
            self.run_worker(self._interrupt_turn(), group="interrupt")
            return
        prompt = self._prompt
        if prompt is not None and prompt.text:
            prompt.clear()
            self._last_ctrl_c = 0.0
            self.notify("input cleared")
            return
        if now - self._last_ctrl_c < 1.2:
            self.exit()
            return
        self._last_ctrl_c = now
        self.notify("press Ctrl+C again to quit")

    async def _interrupt_turn(self) -> None:
        try:
            await self.runtime.agent.interrupt()
        except Exception as exc:  # noqa: BLE001
            self.conversation.add_error(f"interrupt failed: {exc}")
            return
        self.notify("interrupt requested")

    # -- approval --------------------------------------------------------
    async def ask_approval(self, request: Any) -> bool:
        """Serve one permission request with the approval modal."""
        try:
            decision = await self.push_screen_wait(ApprovalScreen(request))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            return False
        if decision == ALLOW_ALWAYS:
            permission = getattr(self.runtime, "permission", None)
            setter = getattr(permission, "set_approval", None)
            if setter is not None:
                setter("always")
            self.notify("approval policy: always")
            self.refresh_status()
        return decision in (ALLOW_ALWAYS, "once")

    # -- questions -------------------------------------------------------
    async def ask_question(self, request: AskRequest) -> list[Answer]:
        """Serve one ask_user_question request with a modal per question.

        Escape/Cancel in a modal stops the request: answers already collected
        are returned and the remaining questions are not shown, which the
        user_questions service normalises into skipped answers. A failing or
        closed modal never raises into the agent turn: the answers collected
        so far (possibly none) are returned instead.
        """
        questions = tuple(getattr(request, "questions", ()) or ())
        answers: list[Answer] = []
        if not questions:
            return answers
        total = len(questions)
        for index, question in enumerate(questions):
            try:
                answer = await self.push_screen_wait(QuestionScreen(question, index=index, total=total))
            except asyncio.CancelledError:
                # An interrupted turn cancels the wait: drop the orphaned modal
                # so the next turn does not start behind a stale screen.
                self._dismiss_question_screen()
                return answers
            except Exception:  # noqa: BLE001 - UI failure must not hang the agent
                return answers
            if answer is None:
                break
            answers.append(answer)
        return answers

    def _dismiss_question_screen(self) -> None:
        """Pop the question modal if a cancelled wait left it on top."""
        try:
            screen = self.screen
        except Exception:  # noqa: BLE001 - the screen stack may already be tearing down
            return
        if not isinstance(screen, QuestionScreen):
            return
        try:
            self.pop_screen()
        except Exception:  # noqa: BLE001 - a modal already gone must not raise
            return

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
        context_used = int(context_reader()) if callable(context_reader) else usage["input"]
        status.show(
            session_id=runtime.session_id,
            provider=runtime.provider_name,
            model=runtime.model,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cached_tokens=usage["cached"],
            context_window=window,
            phase=self._phase,
            preset=self._current_preset(),
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
        try:
            self.query_one(ActivityBar).update_state(state)
        except Exception:  # noqa: BLE001, S110 - composition not ready yet
            pass

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

    # -- policy / approval / workspace commands --------------------------
    def _agent_preset(self) -> str:
        return str(getattr(self.runtime, "agent_preset", "default") or "default")

    def _agent_mode(self) -> str:
        reader = getattr(self.runtime, "agent_mode", "default")
        try:
            return str(reader or "default")
        except Exception:  # noqa: BLE001 - status display must never fail
            return "default"

    def _current_preset(self) -> str:
        permission = getattr(self.runtime, "permission", None)
        return str(getattr(permission, "preset", "workspace-write"))

    def _current_approval(self) -> str:
        permission = getattr(self.runtime, "permission", None)
        return str(getattr(permission, "approval", "ask"))

    async def policy_command(self, argument: str) -> None:
        """Show or switch the sandbox permission preset (/policy)."""
        presets = [str(item) for item in PRESETS]
        if not argument:
            if self._working:
                self.notify("cannot change policy during a running turn", severity="warning")
                return
            current = self._current_preset()
            choice = await self._pick(
                "Select permission preset",
                [(item, item, "current" if item == current else "") for item in presets],
            )
            if choice is None:
                self.conversation.add_info(f"current permission preset: {current}")
                self.conversation.add_info("available: " + ", ".join(presets))
                self.conversation.add_info("use /policy <preset>")
                return
            argument = choice
        if argument not in presets:
            self.conversation.add_error(
                f"unknown permission preset: {argument} (choose from {', '.join(presets)})"
            )
            return
        await self._apply_preset(argument)

    async def approval_command(self, argument: str) -> None:
        """Show or switch the approval policy (/approval)."""
        approvals = [str(item) for item in APPROVALS]
        if not argument:
            if self._working:
                self.notify("cannot change approval during a running turn", severity="warning")
                return
            current = self._current_approval()
            choice = await self._pick(
                "Select approval policy",
                [(item, item, "current" if item == current else "") for item in approvals],
            )
            if choice is None:
                self.conversation.add_info(f"current approval policy: {current}")
                self.conversation.add_info("available: " + ", ".join(approvals))
                self.conversation.add_info("use /approval <policy>")
                return
            argument = choice
        if argument not in approvals:
            self.conversation.add_error(
                f"unknown approval policy: {argument} (choose from {', '.join(approvals)})"
            )
            return
        await self._apply_approval(argument)

    async def cwd_command(self, argument: str) -> None:
        """Show or switch the workspace directory (/cwd)."""
        if self._working:
            self.notify("cannot switch workspace during a running turn", severity="warning")
            return
        runtime = self.runtime
        current = str(getattr(runtime, "cwd", "") or "")
        if not argument:
            known = getattr(runtime, "known_workspaces", None)
            workspaces = list(known() if callable(known) else [])
            options: list[tuple[str, str, str]] = []
            for path in [current, *workspaces]:
                if path and path not in [item[0] for item in options]:
                    options.append((path, path, "current" if path == current else ""))
            options.append((CUSTOM_WORKSPACE, CUSTOM_WORKSPACE_LABEL, ""))
            choice = await self._pick("Select workspace", options)
            if choice is None:
                self.conversation.add_info(f"current workspace: {current}")
                if workspaces:
                    self.conversation.add_info("recent: " + ", ".join(workspaces))
                self.conversation.add_info("use /cwd <path>")
                return
            argument = choice
        if argument == CUSTOM_WORKSPACE:
            path = await self._ask_workspace_path()
            if not path:
                return
            argument = path
        await self._apply_workspace(argument)

    async def _apply_preset(self, preset: str) -> None:
        if self._working:
            self.notify("cannot change policy during a running turn", severity="warning")
            self.refresh_status()
            return
        try:
            await self.runtime.set_preset(preset)
        except Exception as exc:  # noqa: BLE001 - surface, then resync the view
            self.notify(f"policy switch failed: {exc}", severity="error")
        else:
            self.notify(f"permission preset: {preset}")
        finally:
            self.refresh_status()

    async def _apply_approval(self, approval: str) -> None:
        if self._working:
            self.notify("cannot change approval during a running turn", severity="warning")
            self.refresh_status()
            return
        try:
            await self.runtime.set_approval(approval)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"approval switch failed: {exc}", severity="error")
        else:
            self.notify(f"approval policy: {approval}")
        finally:
            self.refresh_status()

    async def _apply_workspace(self, path: str | Path) -> None:
        if self._working:
            self.notify("cannot switch workspace during a running turn", severity="warning")
            self.refresh_status()
            return
        runtime = self.runtime
        try:
            workspace = await runtime.set_workspace(path)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"workspace switch failed: {exc}", severity="error")
        else:
            self.conversation.add_info(f"workspace switched to {workspace}")
            self.notify(f"cwd: {workspace}")
        finally:
            self.refresh_status()

    async def _ask_workspace_path(self) -> str | None:
        runtime = self.runtime
        current = str(getattr(runtime, "cwd", "") or self._cwd or "")
        try:
            return await self.push_screen_wait(PathPromptScreen(current))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            return None

    # -- session rendering ----------------------------------------------
    def _render_welcome(self) -> None:
        runtime = self.runtime
        conversation = self.conversation
        conversation.add_info("DeepPyro Harness — python core, terminal UI")
        conversation.add_info(
            f"{runtime.provider_name}/{runtime.model}  ·  agent preset {self._agent_preset()}"
            f" ({self._agent_mode()} mode)  ·  /model and /provider switch the model;"
            " /preset switches the agent preset"
        )
        conversation.add_info("/policy, /approval and /cwd change the session settings")
        conversation.add_info("Type a message. /help lists commands. Ctrl+C interrupts, Ctrl+D quits.")

    def _render_history(self) -> None:
        runtime = self.runtime
        session = getattr(runtime, "session", None)
        conversation = self.conversation
        if session is None:
            return
        try:
            messages = session.derive_messages()
        except Exception as exc:  # noqa: BLE001 - a bad log must not brick the UI
            conversation.add_error(f"cannot derive session history: {exc}")
            return
        processes = process_map(session)
        turn_number = 0
        for message in messages:
            role = str(getattr(message, "role", "") or "")
            text = message.text() if hasattr(message, "text") else ""
            if role == "user":
                turn_number += 1
                conversation.add_user(text)
                # Replay the collapsed thinking and tool lines for this turn.
                turn_process = processes.get(turn_number)
                if turn_process is not None:
                    conversation.add_history_process(turn_process)
            elif role == "assistant" and text.strip():
                conversation.stream_text(text)
                conversation.finish_assistant(text)
            elif role == "tool":
                # Tool calls and results are already inside the process block.
                continue

    # -- commands (called by deeppyro.tui.commands) ----------------------
    def command_list(self) -> list[Any] | None:
        """Live command list (plugins included), or None for the fallback."""
        return live_commands(self)

    def show_help(self) -> None:
        self.conversation.add_info(help_text(self.command_list()))

    async def new_session(self) -> None:
        if self._working:
            self.notify("cannot start a session during a turn", severity="warning")
            return
        await self.runtime.new_session()
        self.conversation.clear()
        self._context_used = 0
        self._render_welcome()
        self.refresh_status()

    async def _pick(self, title: str, items: list[tuple[str, str, str]]) -> str | None:
        """Show a modal picker inside a worker; None outside a worker or on cancel."""
        if not items:
            return None
        try:
            return await self.push_screen_wait(PickerScreen(title, items))
        except Exception:  # noqa: BLE001 - push_screen_wait needs a worker
            return None

    def _session_items(self, sessions: list[Any]) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        for info in sessions:
            session_id = str(getattr(info, "id", "") or "")
            title = str(getattr(info, "title", None) or "(untitled)")
            cwd = str(getattr(info, "cwd", "") or "")
            items.append((session_id, short_session_id(session_id), f"{title}  {cwd}"))
        return items

    async def resume(self, argument: str) -> None:
        if self._working:
            self.notify("cannot switch session during a turn", severity="warning")
            return
        sessions = self.runtime.recent_sessions(limit=50)
        if not argument:
            if not sessions:
                self.conversation.add_info("no previous sessions")
                return
            choice = await self._pick("Resume session", self._session_items(sessions))
            if choice is None:
                self.conversation.add_info("recent sessions:")
                for index, info in enumerate(sessions, start=1):
                    title = getattr(info, "title", None) or "(untitled)"
                    self.conversation.add_info(
                        f"  {index}. {short_session_id(getattr(info, 'id', ''))}  {title}  {getattr(info, 'cwd', '')}"
                    )
                self.conversation.add_info("use /resume <index|id>")
                return
            argument = choice
        target = None
        if argument.isdigit():
            index = int(argument)
            if 1 <= index <= len(sessions):
                target = sessions[index - 1]
        else:
            bare = argument.removeprefix("session-")
            for info in sessions:
                info_id = str(getattr(info, "id", "") or "")
                if info_id == argument or info_id.removeprefix("session-").startswith(bare):
                    target = info
                    break
        if target is None:
            self.conversation.add_error(f"session not found: {argument}")
            return
        session_id = str(getattr(target, "id", "") or "")
        await self.runtime.open_session(session_id)
        self.conversation.clear()
        self._context_used = 0
        self._render_welcome()
        self.conversation.add_info(f"resumed {short_session_id(session_id)}")
        self._render_history()
        self.refresh_status()

    def _provider_names(self) -> list[str]:
        """Configured provider names, falling back to the active one."""
        getter = getattr(self.runtime, "providers", None)
        if callable(getter):
            try:
                values = [str(name) for name in getter()]
                if values:
                    return values
            except Exception:  # noqa: BLE001, S110 - the command must keep working
                pass
        active = str(getattr(self.runtime, "provider_name", "") or "")
        return [active] if active else []

    def _model_choices(self) -> list[tuple[str, str]]:
        """Every configured (provider, model) pair, if the runtime knows them."""
        getter = getattr(self.runtime, "model_choices", None)
        if callable(getter):
            try:
                return [(str(provider), str(model)) for provider, model in getter()]
            except Exception:  # noqa: BLE001, S110
                pass
        getter = getattr(self.runtime, "models", None)
        if callable(getter):
            try:
                provider = str(getattr(self.runtime, "provider_name", "") or "")
                return [(provider, str(model)) for model in getter()]
            except Exception:  # noqa: BLE001, S110
                pass
        return []

    def _model_value(self, provider: str, model: str) -> str:
        """Picker value: bare model with one provider, provider/model otherwise."""
        return f"{provider}/{model}" if len(self._provider_names()) > 1 else model

    def _report_persist_error(self) -> None:
        message = getattr(self.runtime, "last_persist_error", None)
        if message:
            self.conversation.add_info(f"could not save the default provider/model: {message}")

    async def _refresh_model_catalog(self) -> None:
        """Refresh the llm catalog before showing model choices (best effort).

        Discovered models are a convenience: when the refresh fails the picker
        simply keeps showing the configured models.
        """
        refresher = getattr(self.runtime, "refresh_models", None)
        if not callable(refresher):
            return
        try:
            await refresher()
        except Exception:  # noqa: BLE001 - the configured catalog stays usable
            return

    async def provider_command(self, argument: str) -> None:
        """Show or switch the active LLM provider (/provider)."""
        if self._working:
            self.notify("cannot switch provider during a running turn", severity="warning")
            return
        runtime = self.runtime
        providers = self._provider_names()
        current = str(getattr(runtime, "provider_name", "") or "")
        if not argument:
            if len(providers) > 1:
                items: list[tuple[str, str, str]] = []
                has_key = getattr(runtime, "has_api_key", None)
                for name in providers:
                    note = "current" if name == current else ""
                    if note != "current" and callable(has_key):
                        try:
                            note = "" if has_key(name) else "missing API key"
                        except Exception:  # noqa: BLE001 - notes are cosmetic
                            note = ""
                    items.append((name, name, note))
                choice = await self._pick("Select provider", items)
                if choice is None:
                    self.conversation.add_info(f"current provider: {current}")
                    self.conversation.add_info("available: " + ", ".join(providers))
                    self.conversation.add_info("use /provider <name>")
                    return
                argument = choice
            else:
                self.conversation.add_info(f"current provider: {current}")
                self.conversation.add_info("use /provider <name>")
                return
        argument = argument.strip()
        if argument not in providers:
            self.conversation.add_error(f"unknown provider: {argument} (choose from {', '.join(providers)})")
            return
        try:
            provider_name, model_id = await runtime.set_provider(argument)
        except Exception as exc:  # noqa: BLE001 - show the runtime message
            self.conversation.add_error(f"provider switch failed: {exc}")
            return
        self.conversation.add_info(f"provider switched to {provider_name} · {model_id}")
        self._report_persist_error()
        self.refresh_status()

    async def model_command(self, argument: str) -> None:
        """Show or switch the model, including across providers (/model)."""
        if self._working:
            self.notify("cannot switch model during a running turn", severity="warning")
            return
        runtime = self.runtime
        await self._refresh_model_catalog()
        providers = self._provider_names()
        choices = self._model_choices()
        current = (
            str(getattr(runtime, "provider_name", "") or ""),
            str(getattr(runtime, "model", "") or ""),
        )
        if not argument:
            if len(choices) > 1:
                items: list[tuple[str, str, str]] = []
                for provider_name, model_id in choices:
                    value = self._model_value(provider_name, model_id)
                    label = model_id if len(providers) == 1 else value
                    note = "current" if (provider_name, model_id) == current else ""
                    items.append((value, label, note))
                choice = await self._pick("Select model", items)
                if choice is None:
                    self.conversation.add_info(f"current model: {current[0]}/{current[1]}")
                    self.conversation.add_info("available: " + ", ".join(value for value, _, _ in items))
                    self.conversation.add_info("use /model <id> or /model <provider>/<id>")
                    return
                argument = choice
            else:
                self.conversation.add_info(f"current model: {current[0]}/{current[1]}")
                self.conversation.add_info("use /model <id>")
                return
        argument = argument.strip()
        target: tuple[str, str] | None = None
        if "/" in argument:
            provider_name, _, model_id = argument.partition("/")
            provider_name = provider_name.strip()
            model_id = model_id.strip()
            if provider_name not in providers:
                self.conversation.add_error(
                    f"unknown provider: {provider_name} (choose from {', '.join(providers)})"
                )
                return
            offered = [model for provider, model in choices if provider == provider_name]
            if model_id not in offered:
                self.conversation.add_error(
                    f"unknown model for provider {provider_name}: {model_id}"
                    f" (available: {', '.join(offered) or 'none'})"
                )
                return
            target = (provider_name, model_id)
        else:
            if argument in [model for _, model in choices]:
                target = (current[0], argument)
            else:
                matches = [pair for pair in choices if pair[1] == argument]
                if len(matches) == 1:
                    target = matches[0]
                elif len(matches) > 1:
                    labels = ", ".join(f"{provider}/{model}" for provider, model in matches)
                    self.conversation.add_error(
                        f"model {argument} is available on several providers: {labels}"
                    )
                    return
                else:
                    available = (
                        ", ".join(self._model_value(provider, model) for provider, model in choices) or "none"
                    )
                    self.conversation.add_error(f"unknown model: {argument} (available: {available})")
                    return
        assert target is not None
        target_provider, target_model = target
        try:
            if target_provider == runtime.provider_name:
                await runtime.set_model(target_model)
            else:
                await runtime.set_provider(target_provider, model=target_model)
        except Exception as exc:  # noqa: BLE001 - show the runtime message
            self.conversation.add_error(f"model switch failed: {exc}")
            return
        self.conversation.add_info(f"model switched to {target_provider}/{target_model}")
        self._report_persist_error()
        self.refresh_status()

    def _preset_specs(self) -> list[Any]:
        value = getattr(self.runtime, "presets", None)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - the command must keep working
                return []
        return list(value or [])

    async def preset_command(self, argument: str) -> None:
        presets = self._preset_specs()
        current = str(getattr(self.runtime, "agent_preset", "default") or "default")
        if self._working:
            self.notify("cannot switch agent preset during a turn", severity="warning")
            return
        if not argument:
            if len(presets) > 1:
                items: list[tuple[str, str, str]] = []
                for spec in presets:
                    preset_id = str(getattr(spec, "id", "") or "")
                    label = str(getattr(spec, "name", "") or preset_id)
                    note = "current" if preset_id == current else str(getattr(spec, "description", "") or "")
                    items.append((preset_id, label, note))
                choice = await self._pick("Select agent preset", items)
                if choice is None:
                    self.conversation.add_info(f"current preset: {current}")
                    self.conversation.add_info(
                        "available: " + ", ".join(str(getattr(s, "id", "")) for s in presets)
                    )
                    self.conversation.add_info("use /preset <id>")
                    return
                argument = choice
            else:
                self.conversation.add_info(f"current preset: {current}")
                if presets:
                    self.conversation.add_info(
                        "available: " + ", ".join(str(getattr(s, "id", "")) for s in presets)
                    )
                self.conversation.add_info("use /preset <id>")
                return
        try:
            selected = await self.runtime.set_agent_preset(argument)
        except Exception as exc:  # noqa: BLE001 - show the service message
            self.conversation.add_error(f"preset switch failed: {exc}")
            return
        mode = self._agent_mode()
        hint = "team tools enabled for the lead" if mode == "team" else "subagent delegation enabled"
        self.conversation.add_info(f"agent preset switched to {selected} · {mode} mode ({hint})")
        self.refresh_status()

    def show_status(self) -> None:
        runtime = self.runtime
        usage = runtime.session_usage()
        session = getattr(runtime, "session", None)
        records = len(getattr(session, "records", []) or [])
        permission = getattr(runtime, "permission", None)
        policy = f"{getattr(permission, 'preset', 'n/a')} · {getattr(permission, 'approval', 'n/a')}"
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
            f"preset   {self._agent_preset()}  ({self._agent_mode()} mode)",
            f"policy   {policy}",
            f"tokens   input {usage['input']}  output {usage['output']}  cached {usage['cached']}",
            f"context  {context_percent(self._context_used, runtime.context_window)}% of {runtime.context_window}",
            f"records  {records}  turns {getattr(session, 'turn_count', lambda: 0)()}",
            f"phase    {self._phase}",
        ]
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
                try:
                    target.extend(getter())
                except Exception:  # noqa: BLE001, S110 - a broken store must not crash the UI
                    pass
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
        panels = [row.id for row in registry.list("panel")]
        if panels:
            self.conversation.add_info("plugin panels: " + ", ".join(panels))
            self.conversation.add_info("use /panel <id>")
        else:
            self.conversation.add_info("no plugin panels are registered")

    async def show_plugins(self) -> None:
        """Open the external plugin manager, or list kernel plugins as fallback."""
        try:
            from deeppyro.tui.screens.plugins import PluginScreen

            manager = None
            service = getattr(self.runtime, "_service", None)
            if callable(service):
                plugin_manager = service("plugin_manager")
                manager = getattr(plugin_manager, "manager", None)
            await self.push_screen_wait(PluginScreen(manager))
            return
        except Exception:  # noqa: BLE001, S110 - outside a worker or manager failure
            pass
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


def run_app(**kwargs: Any) -> None:
    """Create and run the app (used by the console-script entry point)."""
    DeepPyroApp(**kwargs).run()


__all__ = ["DeepPyroApp", "RuntimeFactory", "run_app"]
