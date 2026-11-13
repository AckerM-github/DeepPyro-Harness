"""The agent turn/step loop (P0: inbox, hooks, prompt projections, retries).

Public entry points:

* :meth:`AgentSession.send` enqueues a next-turn message and drives the loop
  until the inbox is empty, returning the final assistant text of the last turn.
* :meth:`AgentSession.followup` / :meth:`AgentSession.steer` /
  :meth:`AgentSession.inject` enqueue messages for the next turn or the next
  step boundary; :meth:`AgentSession.wait_for_idle` waits for the driver.
* :meth:`AgentSession.interrupt` cancels the running turn and (by default)
  clears the pending inbox.

A turn is driven by inbox claims, mirroring the reference loop:

    turn/start
      [agent/pre-step waterfall]              (once, before the first step)
      step/start
        system/message (projection)           (each step; append or replace)
        user/message  x claimed messages      (first step)
        user/message  runtime context         (each step; no-op when unchanged)
        request/header request/context        (when the request changed)
        [assistant/message | assistant/attempt]
        tool/call ... -> tool/result ...      (grouped by parallel barrier)
      step/end
      step/start ...
      step/end
      [agent/turn-stopping serial]            (when no next-step is pending)
    turn/end

Events are emitted only after the matching durable record has been appended.
Transient text/thinking frames are emitted live while the provider streams.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.agent.images import plan_request_images
from deeppyro.agent.inbox import TARGET_NEXT_STEP, TARGET_NEXT_TURN, Inbox
from deeppyro.agent.options import AgentOptions
from deeppyro.agent.request import (
    context_data,
    latest_header,
    latest_header_seq,
    plan_header,
    tool_change_data,
    tool_changes,
)
from deeppyro.agent.retry import decide_retry
from deeppyro.agent.stream import AssistantStreamAccumulator
from deeppyro.compact.compactor import Compactor
from deeppyro.core.errors import AbortedError, LlmError, SessionError
from deeppyro.core.ids import new_message_id, now_ms
from deeppyro.core.protocols import (
    BlockEnd,
    BlockStart,
    EventSink,
    Finish,
    LlmProvider,
    LlmRequest,
    ReasoningDelta,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    ToolContext,
    ToolResult,
    Usage,
)
from deeppyro.core.wire import Message, ReasoningPart, TextPart, ToolCallPart
from deeppyro.hooks.types import (
    AGENT_ASSISTANT_STREAM,
    AGENT_PRE_STEP,
    AGENT_REQUEST,
    AGENT_REQUEST_ERROR,
    AGENT_STATUS,
    AGENT_TURN_STOPPING,
)
from deeppyro.prompt import PromptContext, build_system_prompt, collect_instructions
from deeppyro.prompt.projection import (
    ProjectionUpdate,
    RuntimeContextProjection,
    SystemPromptProjection,
)
from deeppyro.prompt.runtime_context import render_runtime_context, runtime_context_sections
from deeppyro.session.image_projection import IMAGE_OFFLOAD_EVENT

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolCallDraft:
    """A tool call while its argument fragments are still streaming in."""

    call_id: str
    name: str
    arguments: str = ""


@dataclass(slots=True)
class StreamAccumulator:
    """Mutable state for one provider stream attempt."""

    reasoning: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    calls: dict[str, ToolCallDraft] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    usage: Usage | None = None
    finish: Finish | None = None

    def start_call(self, call_id: str, name: str) -> None:
        if call_id not in self.calls:
            self.calls[call_id] = ToolCallDraft(call_id=call_id, name=name)
            self.order.append(call_id)

    def add_arguments(self, call_id: str, delta: str) -> None:
        draft = self.calls.get(call_id)
        if draft is None:
            raise LlmError(f"provider sent argument fragments for unknown tool call {call_id}")
        draft.arguments += delta

    def text_value(self) -> str:
        return "".join(self.text)

    def has_content(self) -> bool:
        return bool(self.reasoning or self.text or self.order)

    def parts(self, *, include_tool_calls: bool = True) -> list[Any]:
        parts: list[Any] = []
        reasoning = "".join(self.reasoning)
        if reasoning:
            parts.append(ReasoningPart(text=reasoning))
        text = "".join(self.text)
        if text:
            parts.append(TextPart(text=text))
        if include_tool_calls:
            for call_id in self.order:
                draft = self.calls[call_id]
                parts.append(
                    ToolCallPart(id=draft.call_id, name=draft.name, arguments=draft.arguments or "{}")
                )
        return parts

    def ordered_calls(self) -> list[ToolCallDraft]:
        return [self.calls[call_id] for call_id in self.order]


@dataclass(slots=True)
class _BatchOutcome:
    """Fallback tool batch result (the tools pipeline is normally available)."""

    call_id: str
    result: ToolResult
    call_seq: int | None = None


def _usage_dict(usage: Usage) -> dict[str, int]:
    payload: dict[str, int] = {
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
        "cacheReadTokens": usage.cached_input_tokens or 0,
        "cacheWriteTokens": usage.cache_write_tokens or 0,
        "totalTokens": usage.total_tokens,
    }
    if usage.reasoning_tokens is not None:
        payload["reasoningTokens"] = usage.reasoning_tokens
    return payload


def _truncation_message(max_tokens: int | None, dropped_calls: int) -> str:
    """Explain a max-tokens stop instead of silently ending the turn."""
    limit = max_tokens if max_tokens is not None else "the configured limit"
    message = f"response truncated at maxTokens={limit}"
    if dropped_calls:
        message += f"; {dropped_calls} tool call(s) were not executed"
    return message + "; raise maxTokens or split the work into smaller steps"


def _finish_reason(finish: Finish) -> dict[str, Any]:
    reason: dict[str, Any] = {"kind": finish.kind}
    if finish.failure is not None:
        reason["failure"] = dict(finish.failure)
    if finish.replay_state is not None:
        reason["replayState"] = dict(finish.replay_state)
    return reason


def _dump_messages(messages: list[Message]) -> list[dict[str, Any]]:
    return [message.model_dump(exclude_none=True) for message in messages]


def _coerce_message(value: Any) -> Message:
    if isinstance(value, Message):
        return value
    if isinstance(value, Mapping):
        return Message.model_validate(dict(value))
    raise SessionError(f"agent/pre-step message must be a Message, got {type(value).__name__}")


def _new_user_message(text: str, *, source: Mapping[str, Any] | None = None) -> Message:
    payload = dict(source) if source is not None else {"kind": "user"}
    return Message(role="user", content=[TextPart(text=text)], id=new_message_id(), source=payload)


class AgentSession:
    """Drive one conversation: inbox, turn/step records, provider stream, tools."""

    def __init__(
        self,
        *,
        session: Any,
        provider: LlmProvider,
        registry: Any,
        options: AgentOptions,
        sink: EventSink,
        permission: Any | None = None,
        cwd: Path,
        services: dict[str, Any] | None = None,
        compactor: Compactor | None = None,
        prompt_builder: Any | None = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._registry = registry
        self._options = options
        self._sink = sink
        self._permission = permission
        self._cwd = Path(cwd)
        self._services = dict(services or {})
        # ToolContext.services always exposes the live session so tools that need
        # durable session access (for example compress) never have to reopen it.
        self._services.setdefault("session", session)
        # PTC and future transports dispatch nested tools through the agent's
        # composed registry, narrowed to this agent's preset.
        self._services.setdefault("tool_registry", registry)
        self._prompt_builder = prompt_builder or build_system_prompt
        hooks = self._services.get("hooks")
        self._hooks: Any | None = hooks if hooks is not None else None
        self._retry_policy: Any | None = self._services.get("retryPolicy")
        if self._retry_policy is None:
            self._retry_policy = self._services.get("retry_policy")
        self._model = options.model or str(getattr(provider, "model", "") or "") or "unknown"
        self._instructions = [*collect_instructions(self._cwd), *options.instructions]
        self._append_supports_envelope = self._detect_envelope_support()
        self._inbox = Inbox.from_records(list(self._records()), log=self._session, emit=self._inbox_event)
        self._header_logged = False
        self._phase = "idle"
        self._driving = False
        self._driver_task: asyncio.Task[Any] | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._interrupt_requested = False
        self._cancel_event = asyncio.Event()
        self._turn_open = False
        self._step_open = False
        self._turn = 0
        self._step = 0
        self._acc: StreamAccumulator | None = None
        self._stream: AssistantStreamAccumulator | None = None
        self._assistant_seq: int | None = None
        self._assistant_appended = False
        self._attempt_appended = False
        self._pending_calls: list[ToolCallDraft] = []
        self.compactor = compactor or Compactor(
            provider=provider,
            model=self._model,
            keep_recent_tokens=8000,
            context_window=options.context_window,
            threshold=options.compact_threshold,
        )
        self.last_compaction_error: BaseException | None = None

    # -- introspection -----------------------------------------------------
    @property
    def busy(self) -> bool:
        """True while the driver is running (including tool calls and streams)."""
        return self._driving

    @property
    def model(self) -> str:
        return self._model

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def step(self) -> int:
        return self._step

    @property
    def inbox(self) -> Inbox:
        return self._inbox

    # -- public API --------------------------------------------------------
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
            except Exception:
                logger.warning("failed to clear the agent inbox on interrupt", exc_info=True)
        current = asyncio.current_task()
        task = self._driver_task
        if task is not None and task is not current and not task.done():
            task.cancel()
        if self._driving:
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

    # -- driver ------------------------------------------------------------
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
        self._driver_task = loop.create_task(self._drive_background())

    async def _drive_background(self) -> None:
        try:
            await self._drive()
        except asyncio.CancelledError:
            raise
        except Exception:
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
            except Exception:
                logger.warning("failed to publish the idle phase", exc_info=True)
        return last_text

    async def _set_phase(self, phase: str) -> None:
        if self._phase == phase:
            return
        self._phase = phase
        hooks = self._hooks
        if hooks is None or not hooks.has(AGENT_STATUS):
            return
        await hooks.emit(AGENT_STATUS, {"status": phase})

    # -- turn orchestration ------------------------------------------------
    async def _run_turn(self) -> str:
        turn = self._count_turns() + 1
        self._turn = turn
        self._step = 0
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
                await self._failure_turn(exc)
            raise
        except AbortedError:
            if self._turn_open:
                await self._abort_turn("disposed")
            raise
        except Exception as exc:
            if self._turn_open:
                await self._failure_turn(exc)
            raise

    def _prepare_step_state(self) -> None:
        self._acc = None
        self._stream = None
        self._assistant_seq = None
        self._assistant_appended = False
        self._attempt_appended = False
        self._pending_calls = []

    async def _turn_body(self, turn: int) -> str:
        claimed = self._inbox.claim(TARGET_NEXT_TURN, turn)
        if not claimed:
            await self._close_turn({"kind": "completed"}, final_text=None)
            return ""
        admitted = list(claimed)
        starts_series = True
        decision = await self._pre_step_hook(turn, claimed)
        if decision is not None:
            kind = decision.get("kind")
            if kind == "reject":
                await self._blocked_turn(turn, decision.get("reason"))
                return ""
            if kind != "enter":
                raise ValueError(f"agent/pre-step returned unsupported kind {kind!r}")
            raw_messages = decision.get("messages")
            if raw_messages is not None:
                admitted = self._admit_messages(raw_messages)
            flag = decision.get("startsRequestSeries")
            if isinstance(flag, bool):
                starts_series = flag
        if not admitted:
            await self._close_turn({"kind": "completed"}, final_text=None)
            return ""

        last_text = ""
        step = 0
        while True:
            if self._interrupt_requested:
                return await self._abort_turn("user")
            if step >= self._options.max_steps:
                await self._close_turn({"kind": "max-steps"}, final_text=last_text)
                return last_text
            step += 1
            self._step = step
            self._append("step/start", {"turn": turn, "step": step})
            # UI-only hint so a TUI can start a fresh reasoning step; the durable
            # step/start record above already carries the same information.
            self._emit({"type": "step", "turn": turn, "step": step})
            self._step_open = True
            self._prepare_step_state()
            first = step == 1
            step_messages = admitted if first else self._inbox.claim(TARGET_NEXT_STEP, turn)
            self._commit_system_prompt(turn, step, starts_series=starts_series and first)
            for message in step_messages:
                self._append_user_message(message)
            self._commit_runtime_context(turn, step)
            self._write_request_records(turn, step, starts_series=starts_series and first)
            self._emit_status("thinking")
            await self._maybe_compact()
            request = self._build_request(self._plan_request_images())
            request = await self._request_hook(request, turn, step)
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
                await self._run_tools(turn, step, calls)
                self._close_step()
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
        hooks = self._hooks
        if hooks is None or not hooks.has(AGENT_TURN_STOPPING):
            return True
        await hooks.serial(
            AGENT_TURN_STOPPING,
            {
                "agent": self,
                "turn": turn,
                "signal": self._cancel_event,
                "finalText": final_text,
            },
        )
        return not self._inbox.next_step

    # -- hooks -------------------------------------------------------------
    async def _pre_step_hook(self, turn: int, claimed: list[Message]) -> dict[str, Any] | None:
        hooks = self._hooks
        if hooks is None or not hooks.has(AGENT_PRE_STEP):
            return None
        payload = {
            "agent": self,
            "messages": [*self._derive_messages(), *claimed],
            "turn": turn,
            "step": 1,
            "signal": self._cancel_event,
            "modelSelection": {"provider": self._provider.name, "model": self._model},
            "tools": [schema.model_dump() for schema in self._registry.schemas()],
        }
        fallback = {"kind": "enter", "messages": _dump_messages(claimed)}
        result = await hooks.waterfall(AGENT_PRE_STEP, payload, fallback=dict(fallback))
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise SessionError(f"agent/pre-step must return a mapping, got {type(result).__name__}")
        return dict(result)

    def _admit_messages(self, raw: Any) -> list[Message]:
        if not isinstance(raw, list):
            raise SessionError("agent/pre-step messages must be a list")
        admitted: list[Message] = []
        for item in raw:
            message = _coerce_message(item)
            if message.role != "user":
                raise SessionError(f"agent/pre-step may only admit user messages, got role {message.role!r}")
            admitted.append(message)
        return admitted

    async def _request_hook(self, request: LlmRequest, turn: int, step: int) -> LlmRequest:
        hooks = self._hooks
        if hooks is None or not hooks.has(AGENT_REQUEST):
            return request
        controls: dict[str, Any] = {
            "provider": request.provider,
            "model": request.model,
            "reasoningEffort": None,
            "maxTokens": request.max_tokens,
            "temperature": request.temperature,
            "contextWindow": request.context_window,
        }
        payload = {
            "agent": self,
            "request": request,
            "turn": turn,
            "step": step,
            "signal": self._cancel_event,
        }
        result = await hooks.waterfall(AGENT_REQUEST, payload, fallback=dict(controls))
        if result is None or result is request:
            return request
        if isinstance(result, LlmRequest):
            if result.provider != request.provider or result.model != request.model:
                raise SessionError("agent/request hook may not change the provider or model")
            return result
        if not isinstance(result, Mapping):
            raise SessionError(
                f"agent/request must return controls mapping or LlmRequest, got {type(result).__name__}"
            )
        data = dict(result)
        if data.get("provider", request.provider) != request.provider:
            raise SessionError("agent/request hook may not change the provider")
        if data.get("model", request.model) != request.model:
            raise SessionError("agent/request hook may not change the model")
        if data.get("reasoningEffort") not in (None, controls["reasoningEffort"]):
            raise SessionError("agent/request hook may not change the reasoning effort")
        return LlmRequest(
            provider=request.provider,
            model=request.model,
            messages=request.messages,
            system=request.system,
            tools=request.tools,
            max_tokens=data.get("maxTokens", request.max_tokens),
            temperature=data.get("temperature", request.temperature),
            context_window=data.get("contextWindow", request.context_window),
            session_id=request.session_id,
        )

    async def _request_error_hook(self, turn: int, step: int, failure: dict[str, Any]) -> Any:
        hooks = self._hooks
        if hooks is None or not hooks.has(AGENT_REQUEST_ERROR):
            return None
        payload = {
            "agent": self,
            "turn": turn,
            "step": step,
            "provider": self._provider.name,
            "failure": failure,
            "retryPolicy": self._retry_policy,
            "signal": self._cancel_event,
        }
        return await hooks.waterfall(AGENT_REQUEST_ERROR, payload, fallback=lambda: None)

    async def _stream_frame(self, frame: dict[str, Any]) -> None:
        hooks = self._hooks
        if hooks is None or not hooks.has(AGENT_ASSISTANT_STREAM):
            return
        await hooks.emit(AGENT_ASSISTANT_STREAM, {"frame": frame})

    # -- images ------------------------------------------------------------
    def _image_budget(self) -> Any:
        """Return the route image budget: injectable service first, then provider."""
        budget = self._services.get("image_budget")
        if budget is not None:
            return budget
        return getattr(self._provider, "image_budget", None)

    def _plan_request_images(self) -> list[Message]:
        """Expand attachment references and persist one image/offload if needed.

        The returned messages are exactly what the provider sees for this step:
        references are resolved to inline data URLs in memory, and occurrences
        selected by the route budget already carry the offload placeholder.  The
        durable log only receives the targets -- appended once, before the
        request is built, so the record is visible to later derivations.
        """
        plan = plan_request_images(
            self._records(),
            attachments=self._services.get("attachments"),
            budget=self._image_budget(),
        )
        data = plan.record_data()
        if data:
            self._append(IMAGE_OFFLOAD_EVENT, data)
        return plan.messages

    # -- streaming ---------------------------------------------------------
    def _build_request(self, messages: list[Message]) -> LlmRequest:
        return LlmRequest(
            provider=self._provider.name,
            model=self._model,
            messages=messages,
            system=None,
            tools=self._registry.schemas(),
            max_tokens=self._options.max_tokens,
            temperature=self._options.temperature,
            context_window=self._options.context_window,
            session_id=self._session.session_id,
        )

    async def _stream_step(
        self, request: LlmRequest, turn: int, step: int
    ) -> tuple[StreamAccumulator, Finish]:
        while True:
            accumulator = StreamAccumulator()
            self._acc = accumulator
            self._stream = AssistantStreamAccumulator()
            await self._stream_frame({"type": "start", "turn": turn, "step": step})
            try:
                finish = await self._consume_stream(request, accumulator)
            except LlmError as exc:
                status = getattr(exc, "status", None)
                failure: dict[str, Any] = {
                    "message": str(exc) or "provider failed",
                    "code": str(getattr(exc, "code", "") or "PROVIDER_ERROR"),
                }
                if isinstance(status, int) and not isinstance(status, bool):
                    failure["status"] = status
                finish = Finish(kind="error", failure=failure)
                accumulator.finish = finish
                if self._stream is not None:
                    self._stream.push({"type": "finish", "reason": _finish_reason(finish)})
            if finish.kind != "error":
                return accumulator, finish

            await self._stream_frame({"type": "end", "finish": finish.kind})
            failure = self._sanitize_failure(finish.failure)
            self._append_assistant_attempt(turn, step)
            decision = await self._request_error_hook(turn, step, failure)
            if isinstance(decision, Mapping) and decision.get("kind") == "retry":
                retry = decide_retry(
                    failure=failure,
                    policy=self._retry_policy,
                    previous=self._previous_retry(),
                    provider=self._provider.name,
                    turn=turn,
                    step=step,
                )
                if retry.should_retry:
                    if retry.record is not None:
                        self._append("llm/retry", retry.record)
                    if retry.delay_ms:
                        await asyncio.sleep(float(retry.delay_ms) / 1000.0)
                    if retry.started_record is not None:
                        self._append("llm/retry-started", retry.started_record)
                    continue
            status = failure.get("status")
            raise LlmError(
                str(failure["message"]),
                provider=self._provider.name,
                status=status if isinstance(status, int) and not isinstance(status, bool) else None,
            )

    @staticmethod
    def _sanitize_failure(raw: Mapping[str, Any] | None) -> dict[str, Any]:
        failure = dict(raw or {})
        message = failure.get("message")
        if not isinstance(message, str) or not message:
            failure["message"] = "provider failed"
        code = failure.get("code")
        if not isinstance(code, str) or not code:
            failure["code"] = "PROVIDER_ERROR"
        return failure

    def _previous_retry(self) -> dict[str, Any] | None:
        for record in reversed(self._records()):
            if getattr(record, "type", None) != "llm/retry":
                continue
            data = getattr(record, "data", None)
            if not isinstance(data, dict):
                continue
            retry = data.get("retry")
            retry_id = data.get("retryId")
            if isinstance(retry, int) and isinstance(retry_id, str):
                return {"retry": retry, "retryId": retry_id}
        return None

    async def _consume_stream(self, request: LlmRequest, accumulator: StreamAccumulator) -> Finish:
        stream = self._stream
        tool_indexes: dict[str, int] = {}
        tool_names: dict[str, str] = {}
        async for event in self._provider.stream(request):
            if isinstance(event, BlockStart):
                if stream is not None:
                    stream.push({"type": "block-start", "index": event.index, "blockType": event.block_type})
            elif isinstance(event, BlockEnd):
                if stream is not None:
                    stream.push({"type": "block-end", "index": event.index, "block": dict(event.block)})
            elif isinstance(event, TextDelta):
                accumulator.text.append(event.text)
                if stream is not None:
                    stream.push({"type": "text-delta", "index": event.index, "text": event.text})
                sink_event = {"type": "text", "text": event.text}
                self._emit(sink_event)
                await self._stream_frame({"type": "chunk", "event": sink_event})
            elif isinstance(event, ReasoningDelta):
                accumulator.reasoning.append(event.text)
                if stream is not None:
                    stream.push({"type": "reasoning-delta", "index": event.index, "text": event.text})
                sink_event = {"type": "thinking", "text": event.text}
                self._emit(sink_event)
                await self._stream_frame({"type": "chunk", "event": sink_event})
            elif isinstance(event, ToolCallStart):
                accumulator.start_call(event.call_id, event.name)
                index = event.index
                tool_indexes[event.call_id] = index
                tool_names[event.call_id] = event.name
                if stream is not None:
                    stream.push(
                        {
                            "type": "tool-call-delta",
                            "index": index,
                            "id": event.call_id,
                            "name": event.name,
                            "argumentsDelta": "",
                        }
                    )
            elif isinstance(event, ToolCallArgsDelta):
                accumulator.add_arguments(event.call_id, event.delta)
                if stream is not None:
                    chunk: dict[str, Any] = {
                        "type": "tool-call-delta",
                        "index": tool_indexes.get(event.call_id, event.index),
                        "id": event.call_id,
                        "argumentsDelta": event.delta,
                    }
                    name = tool_names.get(event.call_id)
                    if name is not None:
                        chunk["name"] = name
                    stream.push(chunk)
            elif isinstance(event, Usage):
                accumulator.usage = event
                if stream is not None:
                    stream.push({"type": "usage", "usage": _usage_dict(event)})
            elif isinstance(event, Finish):
                accumulator.finish = event
                if stream is not None:
                    stream.push({"type": "finish", "reason": _finish_reason(event)})
                await self._stream_frame({"type": "end", "finish": event.kind})
                return event
            else:
                raise LlmError(f"provider emitted an unknown stream event: {event!r}")
        raise LlmError("provider stream ended without a finish event", provider=self._provider.name)

    # -- tools -------------------------------------------------------------
    async def _run_tools(self, turn: int, step: int, calls: list[ToolCallDraft]) -> None:
        self._emit_status("tool")
        for group in self._group_calls(calls):
            if self._interrupt_requested:
                raise asyncio.CancelledError()
            self._pending_calls = list(group)
            requests = [_tool_request(call.call_id, call.name, call.arguments or "{}") for call in group]
            outcomes = await self._execute_batch(requests, turn, step)
            by_id = {outcome.call_id: outcome for outcome in outcomes}
            for call in group:
                outcome = by_id.get(call.call_id)
                if outcome is None:
                    result = ToolResult(text=f"Error: tool {call.name!r} produced no result", is_error=True)
                    self._append_tool_result(turn, step, call, result)
                else:
                    self._append_tool_result(turn, step, call, outcome.result, call_seq=outcome.call_seq)
                self._pending_calls = [
                    pending for pending in self._pending_calls if pending.call_id != call.call_id
                ]
        self._pending_calls = []

    def _group_calls(self, calls: list[ToolCallDraft]) -> list[list[ToolCallDraft]]:
        groups: list[list[ToolCallDraft]] = []
        current: list[ToolCallDraft] = []
        for call in calls:
            tool = self._registry.get(call.name)
            parallel = True if tool is None else bool(getattr(tool, "parallel_safe", False))
            if parallel:
                current.append(call)
            else:
                if current:
                    groups.append(current)
                    current = []
                groups.append([call])
        if current:
            groups.append(current)
        return groups

    def _current_cwd(self) -> Path:
        service = self._services.get("working_directory")
        getter = getattr(service, "get", None)
        if callable(getter):
            try:
                current = getter(self._session)
            except Exception:
                logger.warning("failed to read the working directory service", exc_info=True)
            else:
                if current is not None:
                    return Path(current)
        return self._cwd

    def _tool_context(self, call_id: str, turn: int, step: int) -> ToolContext:
        return ToolContext(
            cwd=self._current_cwd(),
            session_id=self._session.session_id,
            turn=turn,
            step=step,
            call_id=call_id,
            cancel=self._cancel_event,
            log=self._tool_log,
            permission=self._permission,
            services=self._services,
        )

    def _tool_log(self, record_type: str, data: dict[str, Any]) -> int:
        return self._append(record_type, data)

    def _tool_call_log(self, turn: int | None, step: int | None, payload: dict[str, Any]) -> int:
        """Append one tool/call record (called by the tools pipeline) and emit its event."""
        data = {
            "turn": turn,
            "step": step,
            "callId": payload["callId"],
            "name": payload["name"],
            "arguments": payload["arguments"],
        }
        seq = self._append("tool/call", data)
        self._emit(
            {
                "type": "tool_call",
                "callId": payload["callId"],
                "name": payload["name"],
                "arguments": payload["arguments"],
            }
        )
        return seq

    async def _execute_batch(self, requests: list[Any], turn: int, step: int) -> list[Any]:
        try:
            from deeppyro.tools.pipeline import run_tool_batch
        except Exception:  # noqa: BLE001 - tools workstream may be missing in isolation
            return await self._execute_batch_fallback(requests, turn, step)

        def factory(request: Any) -> ToolContext:
            return self._tool_context(request.call_id, turn, step)

        try:
            return await run_tool_batch(
                requests,
                registry=self._registry,
                ctx_factory=factory,
                hooks=self._hooks,
                turn=turn,
                step=step,
                log=self._tool_call_log,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            return await self._execute_batch_fallback(requests, turn, step)

    async def _execute_batch_fallback(self, requests: list[Any], turn: int, step: int) -> list[_BatchOutcome]:
        semaphore = asyncio.Semaphore(8)
        seqs = [
            self._tool_call_log(
                turn,
                step,
                {"callId": request.call_id, "name": request.name, "arguments": request.arguments},
            )
            for request in requests
        ]

        async def run_one(index: int, request: Any) -> _BatchOutcome:
            call_seq = seqs[index]
            tool = self._registry.get(request.name)
            if tool is None:
                return _BatchOutcome(
                    request.call_id,
                    ToolResult(text=f"Error: unknown tool {request.name!r}", is_error=True),
                    call_seq,
                )
            try:
                args = json.loads(request.arguments or "{}")
            except json.JSONDecodeError as exc:
                return _BatchOutcome(
                    request.call_id,
                    ToolResult(
                        text=f"Error: invalid JSON arguments for tool {request.name!r}: {exc}",
                        is_error=True,
                    ),
                    call_seq,
                )
            if not isinstance(args, dict):
                return _BatchOutcome(
                    request.call_id,
                    ToolResult(
                        text=f"Error: arguments for tool {request.name!r} must be a JSON object",
                        is_error=True,
                    ),
                    call_seq,
                )
            ctx = self._tool_context(request.call_id, turn, step)
            try:
                async with semaphore:
                    result = await tool.run(args, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - same contract as the tools pipeline
                return _BatchOutcome(
                    request.call_id, ToolResult(text=f"Error: {exc}", is_error=True), call_seq
                )
            if not isinstance(result, ToolResult):
                return _BatchOutcome(
                    request.call_id,
                    ToolResult(
                        text=f"Error: tool {request.name!r} did not return a ToolResult", is_error=True
                    ),
                    call_seq,
                )
            return _BatchOutcome(request.call_id, result, call_seq)

        pairs = await asyncio.gather(*(run_one(index, request) for index, request in enumerate(requests)))
        return list(pairs)

    # -- records -----------------------------------------------------------
    def _detect_envelope_support(self) -> bool:
        append = getattr(self._session, "append", None)
        if append is None:
            return False
        try:
            signature = inspect.signature(append)
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )

    def _append(self, record_type: str, data: dict[str, Any], **envelope: Any) -> int:
        if self._append_supports_envelope and envelope:
            return int(self._session.append(record_type, data, **envelope))
        return int(self._session.append(record_type, data))

    def _emit(self, event: dict[str, Any]) -> None:
        self._sink.emit(event)

    def _emit_status(self, phase: str) -> None:
        self._emit({"type": "status", "phase": phase})

    def _inbox_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event: dict[str, Any] = {"type": event_type}
        event.update(payload)
        self._emit(event)

    def _records(self) -> list[Any]:
        records = getattr(self._session, "records", None)
        return records if isinstance(records, list) else []

    def _derive_messages(self) -> list[Message]:
        derive = getattr(self._session, "derive_messages", None)
        if not callable(derive):
            raise SessionError("session object has no derive_messages()")
        return list(derive())

    def _count_turns(self) -> int:
        return sum(1 for record in self._records() if getattr(record, "type", None) == "turn/start")

    # -- prompt projections ------------------------------------------------
    def _prompt_context(self) -> PromptContext:
        header = getattr(self._session, "header", None)
        started_at = getattr(header, "createdAt", None)
        if not isinstance(started_at, int) or isinstance(started_at, bool):
            started_at = now_ms()
        return PromptContext(
            cwd=self._current_cwd(),
            platform=sys.platform,
            session_id=self._session.session_id,
            started_at=started_at,
            permission_preset=self._permission_preset(),
            approval_policy=self._approval_policy(),
            model=self._model,
            instructions=list(self._instructions),
            env={},
        )

    def _commit_system_prompt(self, turn: int, step: int, *, starts_series: bool) -> None:
        records = list(self._records())
        rendered = self._prompt_builder(self._registry.schemas(), self._prompt_context())
        if not isinstance(rendered, str):
            raise SessionError(f"prompt builder returned {type(rendered).__name__}, expected str")
        projection = SystemPromptProjection(records)
        for update in projection.project(rendered, in_history=True, starts_series=starts_series):
            self._commit_projection("system/message", update, turn, step)

    def _commit_runtime_context(self, turn: int, step: int) -> None:
        records = list(self._records())
        context = self._prompt_context()
        sections = runtime_context_sections(context)
        rendered = render_runtime_context(sections)
        projection = RuntimeContextProjection(records)
        update = projection.project(rendered, sections)
        if update is not None:
            self._commit_projection("user/message", update, turn, step)

    def _commit_projection(self, record_type: str, update: ProjectionUpdate, turn: int, step: int) -> None:
        message = update.message
        if message is None:
            raise SessionError(f"{record_type} projection produced no message")
        dump = message.model_dump(exclude_none=True)
        if record_type == "user/message":
            data: dict[str, Any] = dump
        else:
            data = {"turn": turn, "step": step, "message": dump}
        if update.kind == "append":
            self._append(record_type, data, surfaceOp="append")
            return
        if update.kind != "replace":
            raise SessionError(f"{record_type} projection produced unsupported kind {update.kind!r}")
        seqs = sorted({seq for seq, _ in update.replace})
        if not seqs:
            raise SessionError(f"{record_type} replace projection has no target records")
        self._append(
            record_type,
            data,
            surfaceOp={"op": "replace", "startSeq": seqs[0], "endSeq": seqs[-1]},
            sourceEventSeqs=seqs,
        )

    def _permission_preset(self) -> str:
        if self._options.permission_preset:
            return self._options.permission_preset
        return str(getattr(self._permission, "preset", "") or "workspace-write")

    def _approval_policy(self) -> str:
        if self._options.approval_policy:
            return self._options.approval_policy
        return str(getattr(self._permission, "approval", "") or "ask")

    def _append_user_message(self, message: Message) -> None:
        # user/message stores the wire message directly (fixtures shape); the
        # surface fold validates it as a Message, unlike the nested message
        # records used by system/assistant/tool.
        self._append("user/message", message.model_dump(exclude_none=True), surfaceOp="append")

    def _write_request_records(self, turn: int, step: int, *, starts_series: bool) -> None:
        records = list(self._records())
        tools = [schema.model_dump() for schema in self._registry.schemas()]
        previous = latest_header(records)
        plan = plan_header(
            records,
            provider=self._provider.name,
            model=self._model,
            max_tokens=self._options.max_tokens,
            tools=tools,
            starts_series=starts_series,
            logged=self._header_logged,
        )
        header_seq: int | None = None
        if plan.write:
            data: dict[str, Any] = {"turn": turn, "step": step, "header": plan.header}
            if plan.reason is not None:
                data["reason"] = plan.reason
            header_seq = self._append("request/header", data)
            self._header_logged = True
        change = tool_changes(previous, tools)
        if previous is not None and change.changed:
            change_data = tool_change_data(
                turn,
                step,
                change,
                header_seq=header_seq if header_seq is not None else latest_header_seq(self._records()),
            )
            if change_data is not None:
                self._append("developer/message", change_data, surfaceOp="append")
        payload = context_data(
            self._records(),
            provider=self._provider.name,
            model=self._model,
            context_window=self._options.context_window,
            turn=turn,
            step=step,
        )
        if payload is not None:
            self._append("request/context", payload)

    # -- assistant + tool records -----------------------------------------
    def _append_assistant(
        self,
        turn: int,
        step: int,
        accumulator: StreamAccumulator,
        *,
        include_tool_calls: bool = True,
        interrupted: bool = False,
    ) -> int:
        message = Message(
            role="assistant",
            content=accumulator.parts(include_tool_calls=include_tool_calls),
            id=new_message_id(),
            source={"kind": "model", "provider": self._provider.name, "model": self._model},
        )
        data: dict[str, Any] = {
            "turn": turn,
            "step": step,
            "message": message.model_dump(exclude_none=True),
            "stream": self._stream.snapshot() if self._stream is not None else [],
        }
        if interrupted:
            data["interrupted"] = True
        if accumulator.usage is not None:
            data["usage"] = _usage_dict(accumulator.usage)
        seq = self._append("assistant/message", data, surfaceOp="append")
        self._assistant_seq = seq
        self._assistant_appended = True
        if accumulator.usage is not None:
            self._emit(
                {
                    "type": "usage",
                    "input": accumulator.usage.input_tokens,
                    "output": accumulator.usage.output_tokens,
                    "cached": accumulator.usage.cached_input_tokens,
                }
            )
        return seq

    def _append_tool_result(
        self,
        turn: int,
        step: int,
        call: ToolCallDraft,
        result: ToolResult,
        *,
        call_seq: int | None = None,
    ) -> int:
        message = Message(
            role="tool",
            content=[TextPart(text=result.text or "")],
            id=new_message_id(),
            source={"kind": "tool", "callId": call.call_id},
            toolCallId=call.call_id,
            isError=bool(result.is_error),
        )
        data: dict[str, Any] = {
            "turn": turn,
            "step": step,
            "message": message.model_dump(exclude_none=True),
        }
        if result.meta:
            data["meta"] = result.meta
        envelope: dict[str, Any] = {"surfaceOp": "append"}
        if call_seq is not None:
            envelope["sourceEventSeqs"] = [call_seq]
        seq = self._append("tool/result", data, **envelope)
        self._emit(
            {
                "type": "tool_result",
                "callId": call.call_id,
                "isError": bool(result.is_error),
                "text": result.text,
            }
        )
        return seq

    def _append_assistant_attempt(self, turn: int, step: int) -> None:
        chunks = self._stream.snapshot() if self._stream is not None else []
        if not chunks:
            chunks = [
                {
                    "type": "chunk",
                    "time": now_ms(),
                    "chunk": {"type": "finish", "reason": {"kind": "error"}},
                }
            ]
        self._append("assistant/attempt", {"turn": turn, "step": step, "stream": chunks})
        self._attempt_appended = True

    # -- closing -----------------------------------------------------------
    def _close_step(self) -> None:
        if not self._step_open:
            return
        self._append("step/end", {"turn": self._turn, "step": self._step})
        self._step_open = False

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
        for call in list(self._pending_calls):
            self._append_tool_result(
                self._turn,
                self._step,
                call,
                ToolResult(text="Error: interrupted", is_error=True),
            )
        self._pending_calls = []
        self._close_step()
        await self._close_turn({"kind": "aborted", "reason": {"kind": reason_kind}}, final_text=None)
        return text

    async def _failure_turn(self, exc: BaseException) -> None:
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
        self._close_step()
        error: dict[str, Any] = {"message": str(exc)}
        code = getattr(exc, "code", None)
        if code is not None:
            error["code"] = code
        await self._close_turn({"kind": "error", "error": error}, final_text=None)

    # -- compaction --------------------------------------------------------
    async def _maybe_compact(self) -> None:
        try:
            messages = self._derive_messages()
            if not self.compactor.should_compact(messages):
                return
            await self.compactor.compact(self._session, messages)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - auto compaction never fails the turn
            self.last_compaction_error = exc
            logger.warning("step compaction failed: %s; continuing the turn", exc)


def _tool_request(call_id: str, name: str, arguments: str) -> Any:
    """Build a tools.pipeline ToolCallRequest without importing it eagerly."""
    from deeppyro.tools.pipeline import ToolCallRequest

    return ToolCallRequest(call_id=call_id, name=name, arguments=arguments)
