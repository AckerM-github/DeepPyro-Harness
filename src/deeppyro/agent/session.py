"""The agent turn/step loop.

One call to AgentSession.send owns exactly one turn:

    turn/start
      step/start
        [system/message] [user/message]      (first step only)
        request/header [request/context]     (first step of the turn)
        assistant/message
        tool/call ... -> tool/result ...     (zero or more groups)
      step/end
      step/start ...
      step/end
    turn/end

Every durable record is appended before the matching UI event is emitted.
The assistant message is appended before tool calls run, tool results are
appended before their events, and turn/end is the last record of the turn.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.agent.options import AgentOptions
from deeppyro.compact.compactor import Compactor
from deeppyro.core.errors import AbortedError, LlmError, SessionError
from deeppyro.core.ids import new_message_id, now_ms
from deeppyro.core.protocols import (
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
from deeppyro.prompt import PromptContext, build_system_prompt, collect_instructions

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ToolCallDraft:
    """A tool call while its argument fragments are still streaming in."""

    call_id: str
    name: str
    arguments: str = ""


@dataclass(slots=True)
class StreamAccumulator:
    """Mutable state for one provider stream, kept alive across cancellations."""

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

    def parts(self) -> list[Any]:
        parts: list[Any] = []
        reasoning = "".join(self.reasoning)
        if reasoning:
            parts.append(ReasoningPart(text=reasoning))
        text = "".join(self.text)
        if text:
            parts.append(TextPart(text=text))
        for call_id in self.order:
            draft = self.calls[call_id]
            parts.append(ToolCallPart(id=draft.call_id, name=draft.name, arguments=draft.arguments or "{}"))
        return parts

    def ordered_calls(self) -> list[ToolCallDraft]:
        return [self.calls[call_id] for call_id in self.order]


def _usage_dict(usage: Usage) -> dict[str, int]:
    return {
        "inputTokens": usage.input_tokens,
        "outputTokens": usage.output_tokens,
        "cacheReadTokens": usage.cached_input_tokens or 0,
        "cacheWriteTokens": 0,
        "totalTokens": usage.total_tokens,
    }


class AgentSession:
    """Drive one conversation: turn/step records, provider stream, tools."""

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
        self._prompt_builder = prompt_builder or build_system_prompt
        self._busy = False
        self._interrupt_requested = False
        self._turn_task: asyncio.Task[Any] | None = None
        self._turn_done: asyncio.Event | None = None
        self._cancel_event = asyncio.Event()
        self._turn_open = False
        self._step_open = False
        self._turn = 0
        self._step = 0
        self._acc: StreamAccumulator | None = None
        self._assistant_seq: int | None = None
        self._assistant_appended = False
        self._pending_calls: list[ToolCallDraft] = []
        self._append_supports_envelope = self._detect_envelope_support()
        self._model = options.model or str(getattr(provider, "model", "") or "") or "unknown"
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
        """True while a turn is executing (including tool calls and streams)."""
        return self._busy

    @property
    def model(self) -> str:
        return self._model

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def step(self) -> int:
        return self._step

    # -- public API --------------------------------------------------------
    async def send(self, text: str) -> str:
        """Run one complete turn for the user message and return the final text."""
        if self._busy:
            raise SessionError("agent is busy with another turn")
        self._busy = True
        self._interrupt_requested = False
        self._cancel_event.clear()
        self._turn_done = asyncio.Event()
        self._turn_task = asyncio.current_task()
        try:
            turn = self._count_turns() + 1
            self._turn = turn
            self._append("turn/start", {"turn": turn})
            self._turn_open = True
            return await self._run_turn(turn, text)
        except asyncio.CancelledError:
            if self._interrupt_requested:
                return self._abort_turn("user")
            self._abort_turn("disposed")
            raise
        except LlmError as exc:
            if self._turn_open:
                self._failure_turn(exc)
            raise
        except AbortedError:
            if self._turn_open:
                self._abort_turn("disposed")
            raise
        except Exception as exc:
            if self._turn_open:
                self._failure_turn(exc)
            raise
        finally:
            self._busy = False
            self._turn_task = None
            if self._turn_done is not None:
                self._turn_done.set()

    async def interrupt(self) -> None:
        """Cancel the running turn, recording a user abort and preserving partial output."""
        self._interrupt_requested = True
        self._cancel_event.set()
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
        done = self._turn_done
        if done is not None and not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

    # -- turn orchestration ------------------------------------------------
    async def _run_turn(self, turn: int, text: str) -> str:
        last_text = ""
        step = 0
        while True:
            if self._interrupt_requested:
                return self._abort_turn("user")
            if step >= self._options.max_steps:
                self._close_turn({"kind": "max-steps"}, final_text=last_text)
                return last_text
            step += 1
            self._step = step
            self._append("step/start", {"turn": turn, "step": step})
            # UI-only hint so a TUI can start a fresh reasoning step; the durable
            # step/start record above already carries the same information.
            self._emit({"type": "step", "turn": turn, "step": step})
            self._step_open = True
            self._acc = None
            self._assistant_appended = False
            self._assistant_seq = None
            self._pending_calls = []
            if step == 1:
                self._ensure_system_message(turn, step)
                self._append_user_message(turn, step, text)
                self._write_request_header(turn, step)
                self._write_request_context(turn, step)
            self._emit_status("thinking")
            await self._maybe_compact()
            messages = self._derive_messages()
            request = LlmRequest(
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
            accumulator = StreamAccumulator()
            self._acc = accumulator
            finish = await self._consume_stream(request, accumulator)

            if finish.kind == "error":
                failure = finish.failure or {}
                status = failure.get("status")
                exc = LlmError(
                    str(failure.get("message", "provider failed")),
                    provider=self._provider.name,
                    status=status if isinstance(status, int) else None,
                )
                self._failure_turn(exc)
                raise exc
            if finish.kind == "aborted":
                if accumulator.has_content() and not self._assistant_appended:
                    self._append_assistant(turn, step, accumulator)
                last_text = accumulator.text_value() or last_text
                self._close_step()
                self._close_turn({"kind": "aborted", "reason": {"kind": "provider"}}, final_text=None)
                return last_text

            if accumulator.has_content() and not self._assistant_appended:
                self._append_assistant(turn, step, accumulator)
            last_text = accumulator.text_value() or last_text
            if finish.kind == "length":
                self._close_step()
                self._close_turn({"kind": "max-tokens"}, final_text=accumulator.text_value())
                return last_text

            calls = accumulator.ordered_calls()
            if calls:
                await self._run_tools(turn, step, calls)
                self._close_step()
                continue
            self._close_step()
            self._close_turn({"kind": "completed"}, final_text=last_text)
            return last_text

    async def _consume_stream(self, request: LlmRequest, accumulator: StreamAccumulator) -> Finish:
        async for event in self._provider.stream(request):
            if isinstance(event, TextDelta):
                accumulator.text.append(event.text)
                self._emit({"type": "text", "text": event.text})
            elif isinstance(event, ReasoningDelta):
                accumulator.reasoning.append(event.text)
                self._emit({"type": "thinking", "text": event.text})
            elif isinstance(event, ToolCallStart):
                accumulator.start_call(event.call_id, event.name)
            elif isinstance(event, ToolCallArgsDelta):
                accumulator.add_arguments(event.call_id, event.delta)
            elif isinstance(event, Usage):
                accumulator.usage = event
            elif isinstance(event, Finish):
                accumulator.finish = event
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
            for call in group:
                arguments = call.arguments or "{}"
                self._append(
                    "tool/call",
                    {
                        "turn": turn,
                        "step": step,
                        "callId": call.call_id,
                        "name": call.name,
                        "arguments": arguments,
                    },
                )
                self._emit(
                    {
                        "type": "tool_call",
                        "callId": call.call_id,
                        "name": call.name,
                        "arguments": arguments,
                    }
                )
            self._pending_calls = list(group)
            results = await self._execute_calls(group, turn, step)
            for call in group:
                result = results.get(call.call_id)
                if result is None:
                    result = ToolResult(text=f"Error: tool {call.name!r} produced no result", is_error=True)
                self._append_tool_result(turn, step, call, result)
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

    def _tool_context(self, call_id: str, turn: int, step: int) -> ToolContext:
        return ToolContext(
            cwd=self._cwd,
            session_id=self._session.session_id,
            turn=turn,
            step=step,
            call_id=call_id,
            cancel=self._cancel_event,
            log=self._tool_log,
            permission=self._permission,
            services=self._services,
        )

    def _tool_log(self, record_type: str, data: dict[str, Any]) -> None:
        self._append(record_type, data)

    async def _execute_calls(self, group: list[ToolCallDraft], turn: int, step: int) -> dict[str, ToolResult]:
        try:
            from deeppyro.tools.pipeline import ToolCallRequest, run_tool_batch
        except Exception:  # noqa: BLE001 - tools workstream not available yet
            return await self._execute_calls_fallback(group, turn, step)
        requests = [
            ToolCallRequest(call_id=call.call_id, name=call.name, arguments=call.arguments or "{}")
            for call in group
        ]

        def factory(request: ToolCallRequest) -> ToolContext:
            return self._tool_context(request.call_id, turn, step)

        outcomes = await run_tool_batch(requests, registry=self._registry, ctx_factory=factory)
        return {outcome.call_id: outcome.result for outcome in outcomes}

    async def _execute_calls_fallback(
        self, group: list[ToolCallDraft], turn: int, step: int
    ) -> dict[str, ToolResult]:
        semaphore = asyncio.Semaphore(8)

        async def run_one(call: ToolCallDraft) -> tuple[str, ToolResult]:
            tool = self._registry.get(call.name)
            if tool is None:
                return call.call_id, ToolResult(text=f"Error: unknown tool {call.name!r}", is_error=True)
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as exc:
                return call.call_id, ToolResult(
                    text=f"Error: invalid JSON arguments for tool {call.name!r}: {exc}", is_error=True
                )
            if not isinstance(args, dict):
                return call.call_id, ToolResult(
                    text=f"Error: arguments for tool {call.name!r} must be a JSON object", is_error=True
                )
            ctx = self._tool_context(call.call_id, turn, step)
            try:
                async with semaphore:
                    result = await tool.run(args, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - same contract as the tools pipeline
                return call.call_id, ToolResult(text=f"Error: {exc}", is_error=True)
            if not isinstance(result, ToolResult):
                return call.call_id, ToolResult(
                    text=f"Error: tool {call.name!r} did not return a ToolResult", is_error=True
                )
            return call.call_id, result

        pairs = await asyncio.gather(*(run_one(call) for call in group))
        return dict(pairs)

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

    def _ensure_system_message(self, turn: int, step: int) -> None:
        for record in self._records():
            if getattr(record, "type", None) == "system/message":
                return
        header = getattr(self._session, "header", None)
        started_at = getattr(header, "createdAt", None)
        if not isinstance(started_at, int):
            started_at = now_ms()
        prompt_context = PromptContext(
            cwd=self._cwd,
            platform=sys.platform,
            session_id=self._session.session_id,
            started_at=started_at,
            permission_preset=self._permission_preset(),
            approval_policy=self._approval_policy(),
            model=self._model,
            instructions=[*collect_instructions(self._cwd), *self._options.instructions],
            env={},
        )
        prompt = self._prompt_builder(self._registry.schemas(), prompt_context)
        message = Message(
            role="system",
            content=[TextPart(text=prompt)],
            id=new_message_id(),
            source={"kind": "system-prompt"},
        )
        self._append(
            "system/message",
            {"turn": turn, "step": step, "message": message.model_dump(exclude_none=True)},
            surfaceOp="append",
        )

    def _permission_preset(self) -> str:
        if self._options.permission_preset:
            return self._options.permission_preset
        return str(getattr(self._permission, "preset", "") or "workspace-write")

    def _approval_policy(self) -> str:
        if self._options.approval_policy:
            return self._options.approval_policy
        return str(getattr(self._permission, "approval", "") or "ask")

    def _append_user_message(self, turn: int, step: int, text: str) -> None:
        message = Message(
            role="user",
            content=[TextPart(text=text)],
            id=new_message_id(),
            source={"kind": "user"},
        )
        # user/message stores the wire message directly (fixtures shape); the
        # surface fold validates it as a Message, unlike the nested message
        # records used by system/assistant/tool.
        self._append("user/message", message.model_dump(exclude_none=True), surfaceOp="append")

    def _write_request_header(self, turn: int, step: int) -> None:
        has_header = any(getattr(record, "type", None) == "request/header" for record in self._records())
        reason = "initial" if not has_header else self._header_reason()
        schemas = [schema.model_dump() for schema in self._registry.schemas()]
        header = {
            "config": {
                "provider": self._provider.name,
                "model": self._model,
                "maxTokens": self._options.max_tokens,
                "reasoningEffort": None,
            },
            "adapterDefaults": {"reasoningEffort": True, "maxTokens": True},
            "tools": schemas,
        }
        self._append("request/header", {"turn": turn, "step": step, "header": header, "reason": reason})

    def _header_reason(self) -> str:
        last_reason: Any = None
        for record in self._records():
            if getattr(record, "type", None) == "turn/end":
                data = getattr(record, "data", None) or {}
                reason = data.get("reason") if isinstance(data, dict) else None
                if isinstance(reason, dict):
                    last_reason = reason.get("kind")
        return "resume" if last_reason not in (None, "completed") else "series"

    def _write_request_context(self, turn: int, step: int) -> None:
        last: dict[str, Any] | None = None
        for record in self._records():
            if getattr(record, "type", None) == "request/context":
                data = getattr(record, "data", None)
                last = data if isinstance(data, dict) else None
        changed = (
            last is None
            or last.get("provider") != self._provider.name
            or last.get("model") != self._model
            or last.get("contextWindow") != self._options.context_window
        )
        if not changed:
            return
        self._append(
            "request/context",
            {
                "turn": turn,
                "step": step,
                "provider": self._provider.name,
                "model": self._model,
                "contextWindow": self._options.context_window,
            },
        )

    def _append_assistant(self, turn: int, step: int, accumulator: StreamAccumulator) -> int:
        message = Message(
            role="assistant",
            content=accumulator.parts(),
            id=new_message_id(),
            source={"kind": "model", "provider": self._provider.name, "model": self._model},
        )
        data: dict[str, Any] = {
            "turn": turn,
            "step": step,
            "message": message.model_dump(exclude_none=True),
            "stream": [],
        }
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

    def _append_tool_result(self, turn: int, step: int, call: ToolCallDraft, result: ToolResult) -> int:
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
        if self._assistant_seq is not None:
            envelope["sourceEventSeqs"] = [self._assistant_seq]
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

    # -- closing -----------------------------------------------------------
    def _close_step(self) -> None:
        if not self._step_open:
            return
        self._append("step/end", {"turn": self._turn, "step": self._step})
        self._step_open = False

    def _close_turn(self, reason: dict[str, Any], *, final_text: str | None) -> None:
        if not self._turn_open:
            return
        self._append("turn/end", {"turn": self._turn, "reason": reason})
        self._turn_open = False
        if final_text is not None:
            self._emit({"type": "final", "text": final_text})
        self._emit_status("done")

    def _abort_turn(self, reason_kind: str) -> str:
        """Record a cancellation and return the partial assistant text."""
        text = ""
        accumulator = self._acc
        if accumulator is not None:
            text = accumulator.text_value()
            if accumulator.has_content() and not self._assistant_appended:
                self._append_assistant(self._turn, self._step, accumulator)
        for call in list(self._pending_calls):
            self._append_tool_result(
                self._turn,
                self._step,
                call,
                ToolResult(text="Error: interrupted", is_error=True),
            )
        self._pending_calls = []
        self._close_step()
        self._close_turn({"kind": "aborted", "reason": {"kind": reason_kind}}, final_text=None)
        return text

    def _failure_turn(self, exc: BaseException) -> None:
        """Record a failed turn after the assistant message (or attempt) is known."""
        if not self._turn_open:
            return
        accumulator = self._acc
        if accumulator is not None and accumulator.has_content() and not self._assistant_appended:
            self._append_assistant(self._turn, self._step, accumulator)
        elif not self._assistant_appended:
            self._append_assistant_attempt(self._turn, self._step, accumulator)
        self._emit({"type": "error", "message": str(exc)})
        self._close_step()
        error: dict[str, Any] = {"message": str(exc)}
        code = getattr(exc, "code", None)
        if code is not None:
            error["code"] = code
        self._close_turn({"kind": "error", "error": error}, final_text=None)

    def _append_assistant_attempt(self, turn: int, step: int, accumulator: StreamAccumulator | None) -> None:
        chunks: list[dict[str, Any]] = []
        if accumulator is not None and accumulator.usage is not None:
            chunks.append(
                {
                    "type": "chunk",
                    "time": now_ms(),
                    "chunk": {"type": "usage", "usage": _usage_dict(accumulator.usage)},
                }
            )
        failure = accumulator.finish.failure if accumulator is not None and accumulator.finish else None
        chunks.append(
            {
                "type": "chunk",
                "time": now_ms(),
                "chunk": {
                    "type": "finish",
                    "reason": {"kind": "error", "failure": failure or {}},
                },
            }
        )
        self._append("assistant/attempt", {"turn": turn, "step": step, "stream": chunks})

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
