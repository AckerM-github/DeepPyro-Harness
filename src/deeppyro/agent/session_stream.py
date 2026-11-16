"""Provider streaming and request building for the agent session."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from deeppyro.agent.images import plan_request_images
from deeppyro.agent.retry import decide_retry
from deeppyro.agent.stream import AssistantStreamAccumulator
from deeppyro.attachments import AttachmentStore
from deeppyro.core.errors import LlmError, SessionError
from deeppyro.core.protocols import (
    BlockEnd,
    BlockStart,
    Finish,
    LlmRequest,
    ReasoningDelta,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
)
from deeppyro.core.wire import Message
from deeppyro.hooks.types import (
    AGENT_ASSISTANT_STREAM,
    AGENT_ERROR,
    AGENT_PRE_STEP,
    AGENT_REQUEST,
    AGENT_REQUEST_ERROR,
    LLM_STREAM,
)
from deeppyro.session.derive import repair_pending_tool_calls
from deeppyro.session.image_projection import IMAGE_OFFLOAD_EVENT

from .session_support import StreamAccumulator, _coerce_message, _dump_messages, _finish_reason, _usage_dict
from .session_tools import ToolLayer


class StreamLayer(ToolLayer):
    """Provider streaming and request building for the agent session."""

    def _build_request(self, messages: list[Message]) -> LlmRequest:
        return LlmRequest(
            provider=self._provider.name,
            model=self._model,
            messages=messages,
            system=None,
            tools=self._surface.schemas(),
            max_tokens=self._options.max_tokens,
            temperature=self._options.temperature,
            context_window=self._options.context_window,
            session_id=self._session.session_id,
        )

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
            attachments=self._service(AttachmentStore),
            budget=self._image_budget(),
        )
        data = plan.record_data()
        if data:
            self._append(IMAGE_OFFLOAD_EVENT, data)
        return repair_pending_tool_calls(plan.messages)

    def _image_budget(self) -> Any:
        """Return the route image budget: injectable service first, then provider."""
        budget = self._services.get("image_budget")
        if budget is not None:
            return budget
        return getattr(self._provider, "image_budget", None)

    async def _stream_frame(self, frame: dict[str, Any]) -> None:
        hooks = self._hooks
        if hooks is None:
            return
        # agent/assistant-stream feeds the UI; llm/stream is the plugin view of
        # the same frames and carries the route that produced them.
        if hooks.has(AGENT_ASSISTANT_STREAM):
            await hooks.emit(AGENT_ASSISTANT_STREAM, {"frame": frame}, scope=self._hook_scope())
        if hooks.has(LLM_STREAM):
            await hooks.emit(
                LLM_STREAM,
                {
                    "agent": self,
                    "frame": frame,
                    "provider": self._provider.name,
                    "model": self._model,
                },
                scope=self._hook_scope(),
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
                status = exc.status
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
            if record.type != "llm/retry":
                continue
            data = record.data
            if not isinstance(data, dict):
                continue
            retry = data.get("retry")
            retry_id = data.get("retryId")
            if isinstance(retry, int) and isinstance(retry_id, str):
                return {"retry": retry, "retryId": retry_id}
        return None

    async def _request_hook(self, request: LlmRequest, turn: int, step: int) -> LlmRequest:
        if not self._hook_enabled(AGENT_REQUEST):
            return request
        hooks = self._hooks
        assert hooks is not None
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
        result = await hooks.waterfall(
            AGENT_REQUEST, payload, fallback=dict(controls), scope=self._hook_scope()
        )
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
        if not self._hook_enabled(AGENT_REQUEST_ERROR):
            return None
        hooks = self._hooks
        assert hooks is not None
        payload = {
            "agent": self,
            "turn": turn,
            "step": step,
            "provider": self._provider.name,
            "failure": failure,
            "retryPolicy": self._retry_policy,
            "signal": self._cancel_event,
        }
        return await hooks.waterfall(
            AGENT_REQUEST_ERROR, payload, fallback=lambda: None, scope=self._hook_scope()
        )

    async def _pre_step_hook(self, turn: int, step: int, claimed: list[Message]) -> dict[str, Any] | None:
        if not self._hook_enabled(AGENT_PRE_STEP):
            return None
        hooks = self._hooks
        assert hooks is not None
        payload = {
            "agent": self,
            "messages": list(claimed),
            "turn": turn,
            "step": step,
            "signal": self._cancel_event,
            "modelSelection": {"provider": self._provider.name, "model": self._model},
            "tools": [schema.model_dump() for schema in self._registry.schemas()],
        }
        fallback = {"kind": "enter", "messages": _dump_messages(claimed)}
        result = await hooks.waterfall(
            AGENT_PRE_STEP, payload, fallback=dict(fallback), scope=self._hook_scope()
        )
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

    async def _emit_agent_error(self, exc: BaseException, phase: str) -> None:
        """Publish one agent/error for a failed turn (never for cancellation)."""
        if not self._hook_enabled(AGENT_ERROR):
            return
        hooks = self._hooks
        assert hooks is not None
        await hooks.emit(AGENT_ERROR, {"agent": self, "error": exc, "phase": phase}, scope=self._hook_scope())
