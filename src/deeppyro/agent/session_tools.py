"""Tool dispatch and conversation-record writing for the agent session."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from typing import Any, cast

from deeppyro.agent.request import (
    context_data,
    latest_header,
    latest_header_seq,
    plan_header,
    tool_change_data,
    tool_changes,
)
from deeppyro.core.ids import new_message_id, now_ms
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.core.wire import Message, TextPart

from .session_prompt import PromptLayer
from .session_support import (
    StreamAccumulator,
    ToolCallDraft,
    _BatchOutcome,
    _tool_names,
    _tool_request,
    _usage_dict,
    logger,
)


class ToolLayer(PromptLayer):
    """Tool dispatch and conversation-record writing for the agent session."""

    def _tool_context(self, call_id: str, turn: int, step: int) -> ToolContext:
        return ToolContext(
            cwd=self._current_cwd(),
            session_id=self._session.session_id,
            turn=turn,
            step=step,
            call_id=call_id,
            cancel=self._cancel_event,
            log=cast(Callable[[str, dict[str, Any]], None], self._tool_log),
            permission=self._permission,
            services=self._services,
            agent=self,
            conclude_turn=self._request_turn_conclusion,
        )

    async def _run_tools(self, turn: int, step: int, calls: list[ToolCallDraft]) -> None:
        self._emit_status("tool")
        # Calls that have not been handed to a batch yet; a cancellation records
        # them as aborted-before-dispatch (the pipeline only reports dispatched
        # calls, and it re-raises cancellation without an outcome).
        self._undispatched_calls = list(calls)
        for group in self._group_calls(calls):
            if self._interrupt_requested:
                raise asyncio.CancelledError()
            self._pending_calls = list(group)
            group_ids = {call.call_id for call in group}
            self._undispatched_calls = [
                pending for pending in self._undispatched_calls if pending.call_id not in group_ids
            ]
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
        self._undispatched_calls = []

    def _group_calls(self, calls: list[ToolCallDraft]) -> list[list[ToolCallDraft]]:
        groups: list[list[ToolCallDraft]] = []
        current: list[ToolCallDraft] = []
        for call in calls:
            tool = self._surface.get(call.name)
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

    async def _execute_batch(self, requests: list[Any], turn: int, step: int) -> list[Any]:
        try:
            from deeppyro.tools.pipeline import run_tool_batch
        except ImportError:  # tools workstream may be missing in isolation
            return await self._execute_batch_fallback(requests, turn, step)

        def factory(request: Any) -> ToolContext:
            return self._tool_context(request.call_id, turn, step)

        try:
            return await run_tool_batch(
                requests,
                registry=self._surface,
                ctx_factory=factory,
                hooks=self._hooks,
                turn=turn,
                step=step,
                log=self._tool_call_log,
                agent=self,
                tool_set=_tool_names(self._surface),
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
            tool = self._surface.get(request.name)
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
            # boundary: tool.run executes plugin tool code
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

    def _append_user_message(self, message: Message) -> None:
        # user/message stores the wire message directly (fixtures shape); the
        # surface fold validates it as a Message, unlike the nested message
        # records used by system/assistant/tool.
        self._append("user/message", message.model_dump(exclude_none=True), surfaceOp="append")

    def _write_request_records(self, turn: int, step: int, *, starts_series: bool) -> None:
        records = list(self._records())
        # The request header mirrors the request the model actually sees, so it
        # uses the surface registry rather than every registered tool.
        tools = [schema.model_dump() for schema in self._surface.schemas()]
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
            # Position is carried by the surrounding turn/step records; the
            # header itself only records what the request looked like.
            data: dict[str, Any] = {"header": plan.header}
            if plan.reason is not None:
                data["reason"] = plan.reason
            if plan.starts_series:
                data["startsSeries"] = True
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
        )
        if payload is not None:
            self._append("request/context", payload)

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
        if result.error:
            data["error"] = dict(result.error)
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
        self._append_tool_result_images(result)
        return seq

    def _append_tool_result_images(self, result: ToolResult) -> None:
        """Surface non-text tool result parts as a follow-up user message.

        Image parts cannot live in the tool message without changing its wire
        shape (and therefore the wire contract of tool/result records), so the
        PTC presentation layer hands them to the model in a separate user
        message.  It participates in the normal surface fold like any other
        user message.
        """
        images: list[Any] = [
            dict(part) for part in result.parts if isinstance(part, Mapping) and part.get("type") == "image"
        ]
        if not images:
            return
        message = Message(
            role="user",
            content=images,
            id=new_message_id(),
            source={"kind": "ptc-mode"},
        )
        self._append_user_message(message)

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

    def _close_step(self) -> None:
        if not self._step_open:
            return
        self._append("step/end", {"turn": self._turn, "step": self._step})
        self._step_open = False

    def _request_turn_conclusion(self, reason: dict[str, Any]) -> None:
        """Ask the loop to close the turn after the running tool batch.

        The first request wins: a second tool in the same batch cannot change
        why the turn ended.
        """
        if not isinstance(reason, dict) or not reason.get("kind"):
            raise ValueError("turn conclusion needs a kind")
        if self._turn_conclusion is not None:
            logger.warning("ignoring a second turn conclusion request")
            return
        self._turn_conclusion = dict(reason)

    def _take_turn_conclusion(self) -> dict[str, Any] | None:
        reason = self._turn_conclusion
        self._turn_conclusion = None
        return reason
