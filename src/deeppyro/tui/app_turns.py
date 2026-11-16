"""Layer 6 of the DeepPyro TUI app: User turn submission and interrupt layer."""

from __future__ import annotations

import time

from textual import on

from deeppyro.tui.app_schedule import ScheduleLayer
from deeppyro.tui.commands import dispatch
from deeppyro.tui.widgets.prompt_area import PromptArea


class TurnLayer(ScheduleLayer):
    """User turn submission and interrupt layer."""

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
