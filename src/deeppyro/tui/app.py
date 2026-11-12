"""DeepPyro terminal UI application.

The app is the composition root of the TUI. It owns the TuiSink, boots the
kernel-backed runtime (or receives a test double), forwards agent events to
the conversation widgets and routes user intent either to the agent or to the
slash-command layer.

Rendering never decides anything and the domain layers never know about
Textual: DeepPyroApp is the only place where both worlds meet.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from deeppyro import __version__
from deeppyro.tui.commands import HELP_TEXT, dispatch
from deeppyro.tui.format import compact_text, context_percent, short_session_id
from deeppyro.tui.screens.approval import ALLOW_ALWAYS, ApprovalScreen
from deeppyro.tui.screens.picker import PickerScreen
from deeppyro.tui.screens.splash import SplashScreen
from deeppyro.tui.sink import TuiSink
from deeppyro.tui.widgets.conversation import ConversationView
from deeppyro.tui.widgets.prompt_area import PromptArea
from deeppyro.tui.widgets.status_bar import StatusBar

RuntimeFactory = Callable[["DeepPyroApp"], Awaitable[Any]]


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
        self._working = False
        self._phase = "idle"
        self._context_used = 0
        self._last_ctrl_c = 0.0

    # -- composition -----------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield ConversationView()
        yield StatusBar()
        yield PromptArea()
        yield Footer()

    async def on_mount(self) -> None:
        self._prompt = self.query_one(PromptArea)
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
        self._update_splash()
        self._render_welcome()
        self.refresh_status()

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
                conversation.finish_assistant(str(event.get("text") or ""))
            elif kind == "error":
                conversation.add_error(str(event.get("message") or "unknown error"))
            elif kind == "status":
                self._phase = str(event.get("phase") or "idle")
        except Exception:  # noqa: BLE001, S110 - rendering must never kill a turn
            pass
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
        return decision in (ALLOW_ALWAYS, "once")

    # -- status ----------------------------------------------------------
    def refresh_status(self) -> None:
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
                cwd=str(self._cwd or Path.cwd()),
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                context_window=0,
                approval="ask",
                phase="starting",
            )
            return
        usage = runtime.session_usage()
        window = int(getattr(runtime, "context_window", 0) or 0)
        permission = getattr(runtime, "permission", None)
        status.show(
            session_id=runtime.session_id,
            provider=runtime.provider_name,
            model=runtime.model,
            cwd=str(runtime.cwd),
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cached_tokens=usage["cached"],
            context_window=window,
            approval=str(getattr(permission, "approval", "ask")),
            phase=self._phase,
        )
        self.sub_title = f"{runtime.provider_name}/{runtime.model}"
        # Never steal focus from an overlay (splash, picker, approval modal).
        if self._prompt is not None and len(self.screen_stack) <= 1:
            self._prompt.focus()

    # -- session rendering ----------------------------------------------
    def _render_welcome(self) -> None:
        runtime = self.runtime
        conversation = self.conversation
        conversation.add_info("DeepPyro Harness — python core, terminal UI")
        conversation.add_info(f"{runtime.provider_name}/{runtime.model}  ·  cwd {runtime.cwd}")
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
        for message in messages:
            role = str(getattr(message, "role", "") or "")
            text = message.text() if hasattr(message, "text") else ""
            if role == "user":
                conversation.add_user(text)
            elif role == "assistant" and text.strip():
                conversation.stream_text(text)
                conversation.finish_assistant(text)
            elif role == "tool":
                preview = compact_text(text, max_lines=2, max_chars=160)
                conversation.add_info(f"[tool result] {preview}")

    # -- commands (called by deeppyro.tui.commands) ----------------------
    def show_help(self) -> None:
        self.conversation.add_info(HELP_TEXT)

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

    async def model_command(self, argument: str) -> None:
        available = self.runtime.models()
        if not argument:
            current = self.runtime.model
            if len(available) > 1:
                items = [(model, model, "current" if model == current else "") for model in available]
                choice = await self._pick("Select model", items)
                if choice is None:
                    self.conversation.add_info(f"current model: {current}")
                    self.conversation.add_info("available: " + ", ".join(available))
                    self.conversation.add_info("use /model <id>")
                    return
                argument = choice
            else:
                self.conversation.add_info(f"current model: {current}")
                if available:
                    self.conversation.add_info("available: " + ", ".join(available))
                self.conversation.add_info("use /model <id>")
                return
        if available and argument not in available:
            self.conversation.add_error(f"unknown model: {argument}")
            return
        await self.runtime.set_model(argument)
        self.conversation.add_info(f"model switched to {argument}")
        self.refresh_status()

    def show_status(self) -> None:
        runtime = self.runtime
        usage = runtime.session_usage()
        session = getattr(runtime, "session", None)
        records = len(getattr(session, "records", []) or [])
        lines = [
            f"session  {runtime.session_id}",
            f"model    {runtime.provider_name}/{runtime.model}",
            f"cwd      {runtime.cwd}",
            f"tokens   input {usage['input']}  output {usage['output']}  cached {usage['cached']}",
            f"context  {context_percent(self._context_used, runtime.context_window)}% of {runtime.context_window}",
            f"records  {records}  turns {getattr(session, 'turn_count', lambda: 0)()}",
            f"phase    {self._phase}",
        ]
        for line in lines:
            self.conversation.add_info(line)

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

    async def show_plugins(self) -> None:
        """Open the external plugin manager, or list kernel plugins as fallback."""
        try:
            from deeppyro.tui.screens.plugins import PluginScreen

            await self.push_screen_wait(PluginScreen())
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
