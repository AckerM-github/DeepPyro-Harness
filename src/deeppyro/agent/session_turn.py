"""The turn driver and public entry points of the agent session."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from typing import Any

from deeppyro.agent.inbox import TARGET_NEXT_STEP, TARGET_NEXT_TURN
from deeppyro.core.errors import AbortedError, LlmError, SessionError
from deeppyro.core.protocols import ToolResult
from deeppyro.hooks.types import AGENT_PRE_STEP, AGENT_STATUS, AGENT_TURN_STOPPING

from .session_stream import StreamLayer
from .session_support import (
    HOOK_RECORD_TYPES,
    TOOL_ABORTED,
    TOOL_ABORTED_BEFORE_DISPATCH,
    _abort_error,
    _new_user_message,
    _truncation_message,
    logger,
)


class TurnLayer(StreamLayer):
    """The turn driver and public entry points of the agent session."""

    async def send(self, text: str) -> str:
        """Queue a next-turn user message and run the loop until idle."""
        self._inbox.push(TARGET_NEXT_TURN, _new_user_message(text))
        if self._driving:
            return ""
        task = self._driver_task
        if task is not None and not task.done():
            return ""
        return await self._drive()

    def followup(self, text: str, *, source: Mapping[str, Any] | None = None) -> None:
        """Queue a user message for the next turn and wake the driver if idle."""
        self._enqueue(TARGET_NEXT_TURN, text, wake=True, source=source)

    def steer(self, text: str, *, source: Mapping[str, Any] | None = None) -> None:
        """Queue a user message for the next step boundary and wake the driver."""
        self._enqueue(TARGET_NEXT_STEP, text, wake=True, source=source)

    def inject(self, text: str, *, source: Mapping[str, Any] | None = None) -> None:
        """Queue a user message for the next step boundary without waking the driver."""
        self._enqueue(TARGET_NEXT_STEP, text, wake=False, source=source)

    async def wait_for_idle(self) -> None:
        """Wait until the driver finished every turn it was woken for."""
        while self._driving or self._driver_pending():
            await self._idle.wait()

    async def interrupt(self, *, clear_inbox: bool = True) -> None:
        """Cancel the running turn, recording a user abort and preserving partial output."""
        self._interrupt_requested = True
        self._cancel_event.set()
        if clear_inbox:
            try:
                self._inbox.clear()
            except Exception:  # noqa: BLE001 - inbox.clear() appends to the durable session log and notifies the event sink; interrupt must never raise
                logger.warning("failed to clear the agent inbox on interrupt", exc_info=True)
        current = asyncio.current_task()
        task = self._driver_task
        if task is not None and task is not current and not task.done():
            task.cancel()
        if self._driving:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._idle.wait(), timeout=5.0)

    def record_hook_event(self, record_type: str, data: Mapping[str, Any]) -> int:
        """Persist one hook observation and publish it to the live sink.

        The hook journal observes every hook dispatch and calls this for each
        one; the durable record is appended before the event reaches the sink,
        so a replayed log and the live NDJSON/TUI stream agree.

        Only the two hook record types are accepted.  They are plain, non-surface
        records: derivation, the canonical surface fold and compaction never see
        them, so journaling cannot change what the model saw.

        Args:
            record_type: `hook/invoked` or `hook/result`.
            data: the observation payload (already shaped by the journal).

        Returns:
            The seq of the appended record.

        Raises:
            SessionError: when *record_type* is not a hook record type.
        """
        if record_type not in HOOK_RECORD_TYPES:
            expected = ", ".join(sorted(HOOK_RECORD_TYPES))
            raise SessionError(f"record_hook_event only accepts {expected}, got {record_type!r}")
        payload = dict(data)
        seq = self._append(record_type, payload)
        event: dict[str, Any] = {"type": record_type, "seq": seq}
        event.update(payload)
        self._emit(event)
        return seq

    def _enqueue(
        self,
        target: str,
        text: str,
        *,
        wake: bool,
        source: Mapping[str, Any] | None = None,
    ) -> None:
        self._inbox.push(target, _new_user_message(text, source=source))
        if wake:
            self._wake()

    def _driver_pending(self) -> bool:
        task = self._driver_task
        return task is not None and not task.done()

    def _wake(self) -> None:
        if self._driving or self._driver_pending():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # A previous drive leaves the idle gate set; clear it before the new
        # task can start so wait_for_idle does not spin on the stale event.
        self._idle.clear()
        self._driver_task = loop.create_task(self._drive_background())

    async def _drive_background(self) -> None:
        try:
            await self._drive()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the agent loop runs plugin hooks, tools and LLM calls
            logger.exception("agent driver failed")

    async def _drive(self) -> str:
        self._driving = True
        self._driver_task = asyncio.current_task()
        self._interrupt_requested = False
        self._cancel_event.clear()
        self._idle.clear()
        last_text = ""
        try:
            await self._set_phase("running")
            while not self._interrupt_requested and self._inbox.has_pending:
                last_text = await self._run_turn()
        finally:
            self._driving = False
            self._driver_task = None
            self._idle.set()
            try:
                await self._set_phase("idle")
            except Exception:  # noqa: BLE001 - _set_phase emits through plugin hook listeners
                logger.warning("failed to publish the idle phase", exc_info=True)
        return last_text

    async def _set_phase(self, phase: str) -> None:
        if self._phase == phase:
            return
        self._phase = phase
        if not self._hook_enabled(AGENT_STATUS):
            return
        assert self._hooks is not None
        await self._hooks.emit(AGENT_STATUS, {"agent": self, "status": phase}, scope=self._hook_scope())

    async def _run_turn(self) -> str:
        turn = self._count_turns() + 1
        self._turn = turn
        self._step = 0
        self._turn_conclusion = None
        self._append("turn/start", {"turn": turn})
        self._turn_open = True
        self._prepare_step_state()
        try:
            return await self._turn_body(turn)
        except asyncio.CancelledError:
            if self._interrupt_requested:
                return await self._abort_turn("user")
            await self._abort_turn("disposed")
            raise
        except LlmError as exc:
            if self._turn_open:
                await self._failure_turn(exc, phase="request")
            raise
        except AbortedError:
            if self._turn_open:
                await self._abort_turn("disposed")
            raise
        # boundary: turn bodies run plugin tools/hooks; bookkeeping must see every error before re-raising
        except Exception as exc:
            if self._turn_open:
                await self._failure_turn(exc, phase=self._failure_phase)
            raise

    def _prepare_step_state(self) -> None:
        self._acc = None
        self._stream = None
        self._assistant_seq = None
        self._assistant_appended = False
        self._attempt_appended = False
        self._pending_calls = []
        self._undispatched_calls = []
        self._call_seqs = {}
        self._failure_phase = "turn"

    async def _turn_body(self, turn: int) -> str:
        claimed = self._inbox.claim(TARGET_NEXT_TURN, turn)
        if not claimed:
            await self._close_turn({"kind": "completed"}, final_text=None)
            return ""
        admitted = list(claimed)
        starts_series = True
        pre_step_installed = self._hook_enabled(AGENT_PRE_STEP)
        if not admitted and not pre_step_installed:
            await self._close_turn({"kind": "completed"}, final_text=None)
            return ""

        last_text = ""
        step = 0
        while True:
            if self._interrupt_requested:
                return await self._abort_turn("user")
            if self._options.max_steps is not None and step >= self._options.max_steps:
                await self._close_turn({"kind": "max-steps"}, final_text=last_text)
                return last_text
            step += 1
            self._step = step
            first = step == 1
            step_messages = admitted if first else self._inbox.claim(TARGET_NEXT_STEP, turn)
            # DSH fires agent/pre-step once per step, with only the messages that
            # step claimed; the decision replaces them (or rejects the step).
            decision = await self._pre_step_hook(turn, step, step_messages)
            if decision is None and pre_step_installed:
                # A listener neither continued the chain nor returned a decision:
                # the dispatch is vetoed, so the built-in step entry never runs.
                await self._blocked_turn(turn, "agent/pre-step hook vetoed the turn")
                return ""
            if decision is not None:
                kind = decision.get("kind")
                if kind == "reject":
                    await self._blocked_turn(turn, decision.get("reason"))
                    return ""
                if kind != "enter":
                    raise ValueError(f"agent/pre-step returned unsupported kind {kind!r}")
                raw_messages = decision.get("messages")
                if raw_messages is not None:
                    step_messages = self._admit_messages(raw_messages)
                flag = decision.get("startsRequestSeries")
                if isinstance(flag, bool):
                    starts_series = flag
            if first and not step_messages:
                await self._close_turn({"kind": "completed"}, final_text=None)
                return ""
            # The step is admitted: open it in the log *after* the hook decided,
            # so a rejected step leaves no step/start behind (DSH semantics).
            self._append("step/start", {"turn": turn, "step": step})
            # UI-only hint so a TUI can start a fresh reasoning step; the durable
            # step/start record above already carries the same information.
            self._emit({"type": "step", "turn": turn, "step": step})
            self._step_open = True
            self._prepare_step_state()
            # The chosen model is adopted before the prompt and the request are
            # built, so the system prompt, request/header, request/context and the
            # actual request all describe the same model.
            await self._commit_system_prompt(turn, step, starts_series=starts_series and first)
            for message in step_messages:
                self._append_user_message(message)
            if self._runtime_context:
                self._commit_runtime_context(turn, step)
            self._write_request_records(turn, step, starts_series=starts_series and first)
            self._emit_status("thinking")
            await self._maybe_compact()
            request = self._build_request(self._plan_request_images())
            request = await self._request_hook(request, turn, step)
            self._failure_phase = "request"
            accumulator, finish = await self._stream_step(request, turn, step)
            last_text = accumulator.text_value() or last_text

            if finish.kind == "aborted":
                if accumulator.has_content() and not self._assistant_appended:
                    # The stream was cut before its tool calls could run, so keep
                    # only text/reasoning: an orphan tool call would corrupt the
                    # history derived for the next request.
                    self._append_assistant(
                        turn, step, accumulator, include_tool_calls=False, interrupted=True
                    )
                self._close_step()
                await self._close_turn({"kind": "aborted", "reason": {"kind": "provider"}}, final_text=None)
                return last_text

            if finish.kind == "length":
                truncated_calls = accumulator.ordered_calls()
                if accumulator.has_content() and not self._assistant_appended:
                    # These tool calls never ran: persisting them would leave a
                    # tool-call block without a matching tool/result message.
                    self._append_assistant(turn, step, accumulator, include_tool_calls=False)
                self._emit(
                    {
                        "type": "error",
                        "message": _truncation_message(self._options.max_tokens, len(truncated_calls)),
                    }
                )
                self._close_step()
                await self._close_turn({"kind": "max-tokens"}, final_text=None)
                return last_text

            # A failed attempt earlier in this step already wrote assistant/attempt;
            # the successful attempt still owns the final assistant/message.
            if accumulator.has_content() and not self._assistant_appended:
                self._append_assistant(turn, step, accumulator)
            if self._interrupt_requested:
                return await self._abort_turn("user")

            calls = accumulator.ordered_calls()
            if calls:
                self._failure_phase = "tool"
                await self._run_tools(turn, step, calls)
                self._failure_phase = "turn"
                self._close_step()
                conclusion = self._take_turn_conclusion()
                if conclusion is not None:
                    await self._close_turn(conclusion, final_text=last_text)
                    return last_text
                continue
            self._close_step()
            if self._inbox.next_step:
                continue
            if not await self._should_stop(turn, last_text):
                continue
            await self._close_turn({"kind": "completed"}, final_text=last_text)
            return last_text

    async def _should_stop(self, turn: int, final_text: str) -> bool:
        """Run the turn-stopping hook; return False when a listener steered."""
        if not self._hook_enabled(AGENT_TURN_STOPPING):
            return True
        hooks = self._hooks
        assert hooks is not None
        await hooks.serial(
            AGENT_TURN_STOPPING,
            {
                "agent": self,
                "turn": turn,
                "signal": self._cancel_event,
                "finalText": final_text,
            },
            scope=self._hook_scope(),
        )
        return not self._inbox.next_step

    async def _close_turn(self, reason: dict[str, Any], *, final_text: str | None) -> None:
        if not self._turn_open:
            return
        self._append("turn/end", {"turn": self._turn, "reason": reason})
        self._turn_open = False
        if final_text is not None:
            self._emit({"type": "final", "text": final_text})
        self._emit_status("done")

    async def _blocked_turn(self, turn: int, reason: Any) -> None:
        nested: dict[str, Any] = {"kind": "pre-step"}
        if reason is not None:
            text = str(reason).strip()
            if text:
                nested["reason"] = text
        if not self._turn_open:
            return
        self._append("turn/end", {"turn": turn, "reason": {"kind": "blocked", "reason": nested}})
        self._turn_open = False
        self._emit_status("done")

    async def _abort_turn(self, reason_kind: str) -> str:
        """Record a cancellation and return the partial assistant text."""
        text = ""
        if not self._turn_open:
            return text
        accumulator = self._acc
        if accumulator is not None:
            text = accumulator.text_value()
            if accumulator.has_content() and not self._assistant_appended:
                # Nothing streamed here has run as a tool yet; keep the history
                # valid by dropping unfinished tool-call parts.
                self._append_assistant(
                    self._turn,
                    self._step,
                    accumulator,
                    include_tool_calls=False,
                    interrupted=True,
                )
        # A dispatched call is aborted by the pipeline; a call whose batch never
        # started still gets its own tool/call + tool/result pair, mirroring the
        # reference skip path so the history stays pairable.
        for call in list(self._pending_calls):
            self._append_tool_result(
                self._turn,
                self._step,
                call,
                ToolResult(
                    text="Error: tool call aborted",
                    is_error=True,
                    error=_abort_error(TOOL_ABORTED),
                ),
                call_seq=self._call_seqs.get(call.call_id),
            )
        self._pending_calls = []
        for call in list(self._undispatched_calls):
            call_seq = self._append(
                "tool/call",
                {
                    "turn": self._turn,
                    "step": self._step,
                    "callId": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments or "{}",
                },
            )
            self._append_tool_result(
                self._turn,
                self._step,
                call,
                ToolResult(
                    text="Error: tool call aborted before dispatch",
                    is_error=True,
                    error=_abort_error(TOOL_ABORTED_BEFORE_DISPATCH),
                ),
                call_seq=call_seq,
            )
            self._call_seqs[call.call_id] = call_seq
        self._undispatched_calls = []
        self._close_step()
        await self._close_turn({"kind": "aborted", "reason": {"kind": reason_kind}}, final_text=None)
        return text

    async def _failure_turn(self, exc: BaseException, *, phase: str = "turn") -> None:
        """Record a failed turn after the assistant message (or attempt) is known."""
        if not self._turn_open:
            return
        accumulator = self._acc
        if accumulator is not None and accumulator.has_content() and not self._assistant_appended:
            if not self._attempt_appended:
                self._append_assistant(self._turn, self._step, accumulator)
        elif not self._assistant_appended and not self._attempt_appended:
            self._append_assistant_attempt(self._turn, self._step)
        self._emit({"type": "error", "message": str(exc)})
        await self._emit_agent_error(exc, phase)
        self._close_step()
        error: dict[str, Any] = {"message": str(exc)}
        code = getattr(exc, "code", None)
        if code is not None:
            error["code"] = code
        await self._close_turn({"kind": "error", "error": error}, final_text=None)

    async def _maybe_compact(self) -> None:
        try:
            messages = self._derive_messages()
            if not self.compactor.should_compact(messages):
                return
            await self.compactor.compact(self._session, messages)
        except asyncio.CancelledError:
            raise
        # boundary: compaction calls the LLM provider; auto-compaction never fails the turn
        except Exception as exc:  # noqa: BLE001 - auto compaction never fails the turn
            self.last_compaction_error = exc
            logger.warning("step compaction failed: %s; continuing the turn", exc)
