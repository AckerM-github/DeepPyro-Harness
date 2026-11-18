"""Layer 6 of the DeepPyro TUI app: User turn submission and interrupt layer."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from textual import on

from deeppyro.agent.budget import BudgetExceeded
from deeppyro.agent.errors import ProviderStreamError
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
        if getattr(self.runtime, "native", False):
            await self._run_native_turn(text)
            return
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

    async def _run_native_turn(self, text: str) -> None:
        """Run one native turn and emit the sink events the widgets consume.

        The native agent loop reports typed hooks; the agent host folds them
        into a RunResult. This layer owns the terminal events (final, usage,
        status, error) exactly once, including interrupt and provider failure.
        """
        sink = self._sink
        await self._compact_before_send()
        sink.begin_turn()
        agent = self.runtime.agent
        try:
            try:
                result = await agent.send(text)
            except asyncio.CancelledError:
                sink.emit({"type": "final", "text": self._last_assistant_text(agent)})
                sink.emit({"type": "status", "phase": "interrupted"})
            except (BudgetExceeded, ProviderStreamError) as exc:
                sink.emit({"type": "error", "message": str(exc) or type(exc).__name__})
            except Exception as exc:  # noqa: BLE001 - turn errors belong in the log
                sink.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            else:
                sink.emit({"type": "final", "text": str(getattr(result, "final_text", "") or "")})
                self._emit_native_usage(result)
                sink.emit({"type": "status", "phase": "done"})
                if str(getattr(result, "finish_reason", "") or "") == "length":
                    sink.emit(
                        {
                            "type": "error",
                            "message": "the model stopped at the output token limit (finish_reason: length)",
                        }
                    )
                await self._name_native_session(agent)
        finally:
            self._working = False
            self._phase = "idle"
            self.refresh_status()

    async def _compact_before_send(self) -> None:
        """Compact the native session first when the context threshold is crossed.

        The native agent loop never compacts by itself (design decision D4): the
        TUI owns the pre-send check. The compactor appends exactly one
        compaction.done fact and cuts on tool-pair boundaries only, so the
        folded transcript replays the summary instead of the covered messages.
        """
        compactor = getattr(self.runtime, "compactor", None)
        session = getattr(self.runtime, "session", None)
        pre_send = getattr(compactor, "pre_send_compact", None)
        should = getattr(compactor, "should_compact", None)
        if compactor is None or session is None or not callable(pre_send) or not callable(should):
            return
        view = session.fold() if callable(getattr(session, "fold", None)) else None
        messages = getattr(view, "messages", None)
        if messages is None:
            getter = getattr(session, "messages", None)
            messages = getter() if callable(getter) else None
        try:
            if messages is None or not should(messages):
                return
            self._phase = "compacting"
            self._sink.emit({"type": "status", "phase": "compacting"})
            self.refresh_status()
            compact = cast(Callable[..., Awaitable[Any]], pre_send)
            done = await compact(session, view=view)
        except Exception as exc:  # noqa: BLE001 - compaction must not kill the turn
            self._sink.emit({"type": "error", "message": f"compaction failed: {type(exc).__name__}: {exc}"})
            return
        finally:
            if self._phase == "compacting":
                self._phase = "thinking"
                self.refresh_status()
        if done is not None:
            covered = len(getattr(getattr(done, "region", None), "message_ids", ()) or ())
            self.conversation.add_info(f"compacted {covered} message(s) into a context summary")

    def _emit_native_usage(self, result: Any) -> None:
        """Emit the usage event from the live host counters (turn layer owns it).

        The host tracks tokens through its usage tap, so it is authoritative for
        the cumulative meter; the run result is only a fallback for agents that
        do not expose host counters.
        """
        counters = self._native_usage_counters()
        if counters is None:
            usage = getattr(result, "usage", None)
            counters = {
                "input": int(getattr(usage, "input_tokens", 0) or 0),
                "output": int(getattr(usage, "output_tokens", 0) or 0),
                "cached": int(getattr(result, "cached_input_tokens", 0) or 0),
            }
        self._sink.emit({"type": "usage", **counters})

    def _native_usage_counters(self) -> dict[str, int] | None:
        """Read the native host usage counters, or None when unavailable."""
        reader = getattr(getattr(self.runtime, "agent", None), "usage", None)
        if not callable(reader):
            return None
        try:
            usage = reader()
        except Exception:  # noqa: BLE001 - usage metering is a nicety
            return None
        if not isinstance(usage, dict):
            return None
        try:
            return {
                "input": int(usage.get("input") or 0),
                "output": int(usage.get("output") or 0),
                "cached": int(usage.get("cached") or 0),
            }
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _last_assistant_text(agent: Any) -> str:
        getter = getattr(agent, "last_assistant_text", None)
        if not callable(getter):
            return ""
        try:
            return str(getter() or "")
        except Exception:  # noqa: BLE001 - interrupted text is a nicety
            return ""

    async def _name_native_session(self, agent: Any) -> None:
        """Name the native session after its first completed turn (best effort)."""
        counter = getattr(agent, "turn_count", None)
        try:
            turns = int(cast(Any, counter())) if callable(counter) else 0
        except Exception:  # noqa: BLE001 - titling must not break the turn
            turns = 0
        if turns != 1:
            return
        namer = getattr(self.runtime, "name_session_title", None)
        if not callable(namer):
            return
        # titling is a nicety and must never fail the turn
        with contextlib.suppress(Exception):
            await cast(Any, namer())

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
            if getattr(self.runtime, "native", False):
                interrupted = bool(await self.runtime.agent.interrupt())
                self.notify("interrupt requested" if interrupted else "nothing to interrupt")
                return
            await self.runtime.agent.interrupt()
        except Exception as exc:  # noqa: BLE001
            self.conversation.add_error(f"interrupt failed: {exc}")
            return
        self.notify("interrupt requested")
