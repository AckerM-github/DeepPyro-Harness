"""Adapter from native messages to the existing v4 provider protocol (M2 seam).

This is the single M2 module allowed to import `deeppyro.core.wire` (ADR 4 rule 1,
docs/native/schema.md 6.5 provider boundary): the loop speaks native messages and
this adapter converts them to the v4 wire shapes the existing `LlmProvider`
implementations consume, then converts streamed responses back to native parts.
Removing this seam is the M3 provider port.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from deeppyro.agent.context_plan import ContextPlan
from deeppyro.agent.errors import ProviderStreamError, RunCancelledError
from deeppyro.core.ids import new_call_id
from deeppyro.core.messages import Message, ReasoningPart, TextPart, ToolCallPart
from deeppyro.core.protocols import (
    Finish,
    LlmProvider,
    LlmRequest,
    ReasoningDelta,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
)
from deeppyro.core.wire import ContentPart as WireContentPart
from deeppyro.core.wire import ImagePart as WireImagePart
from deeppyro.core.wire import Message as WireMessage
from deeppyro.core.wire import ReasoningPart as WireReasoningPart
from deeppyro.core.wire import TextPart as WireTextPart
from deeppyro.core.wire import ToolCallPart as WireToolCallPart

FINISH_KIND_TO_NATIVE: dict[str, str] = {
    "stop": "stop",
    "tool-calls": "tool_calls",
    "length": "length",
    "error": "error",
    "aborted": "aborted",
    "content-filter": "content_filter",
}

NATIVE_FINISH_REASONS = frozenset({"stop", "tool_calls", "length", "error", "aborted", "content_filter"})


@dataclass(frozen=True, slots=True)
class NativeToolCall:
    """One fully assembled assistant tool call in native id space."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A complete model response, ready to become one native assistant message."""

    text: str
    reasoning: str
    tool_calls: tuple[NativeToolCall, ...]
    finish_reason: str
    usage: Usage
    latency_ms: int


def to_wire_message(message: Message) -> WireMessage:
    """Convert one native message to the v4 wire message shape."""
    content: list[WireContentPart] = []
    for part in message.content:
        if isinstance(part, TextPart):
            content.append(WireTextPart(text=part.text))
        elif isinstance(part, ReasoningPart):
            content.append(WireReasoningPart(text=part.text))
        elif isinstance(part, ToolCallPart):
            content.append(WireToolCallPart(id=part.id, name=part.name, arguments=part.arguments))
        else:
            content.append(WireImagePart(attachment=part.attachment))
    return WireMessage(
        role=message.role,
        content=content,
        id=message.id,
        source=None,
        toolCallId=message.tool_call_id,
        isError=True if message.is_error else None,
    )


def to_wire_messages(messages: Iterable[Message]) -> list[WireMessage]:
    """Convert native messages in order."""
    return [to_wire_message(message) for message in messages]


def build_request(
    plan: ContextPlan,
    *,
    provider: str,
    model: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
    session_id: str | None = None,
) -> LlmRequest:
    """Build the provider request for one :class:`ContextPlan`."""
    system = None if plan.system is None else plan.system.text()
    return LlmRequest(
        provider=provider,
        model=model,
        messages=to_wire_messages(plan.messages),
        system=system or None,
        tools=list(plan.tools),
        max_tokens=max_tokens,
        temperature=temperature,
        context_window=None,
        session_id=session_id,
    )


@dataclass(slots=True)
class _CallBuilder:
    call_id: str
    name: str = ""
    fragments: list[str] = field(default_factory=list)


class NativeProviderAdapter:
    """Stream one provider response and normalize it into native data."""

    def __init__(self, provider: LlmProvider) -> None:
        self._provider = provider
        self.name = provider.name

    async def complete(
        self,
        request: LlmRequest,
        *,
        cancel: asyncio.Event | None = None,
        on_text: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """Consume one streamed provider call.

        Raises:
            RunCancelledError: the cancel event was set.
            ProviderStreamError: the stream was malformed, failed or returned an
                empty assistant message.
        """
        if cancel is not None and cancel.is_set():
            raise RunCancelledError("run cancelled before the model call")
        started = time.monotonic()
        text_fragments: list[str] = []
        reasoning_fragments: list[str] = []
        builders: dict[int, _CallBuilder] = {}
        ordered: list[_CallBuilder] = []
        by_provider_id: dict[str, _CallBuilder] = {}
        usage = Usage()
        finish: Finish | None = None

        def resolve(index: int, provider_call_id: str | None, name: str | None) -> _CallBuilder:
            builder: _CallBuilder | None = None
            if provider_call_id:
                builder = by_provider_id.get(provider_call_id)
            if builder is None:
                builder = builders.get(index)
            if builder is None:
                builder = _CallBuilder(call_id=new_call_id(), name=name or "")
                builders[index] = builder
                ordered.append(builder)
            if name and not builder.name:
                builder.name = name
            if provider_call_id:
                by_provider_id[provider_call_id] = builder
            return builder

        try:
            async for event in self._provider.stream(request):
                if cancel is not None and cancel.is_set():
                    raise RunCancelledError("run cancelled during the model call")
                if isinstance(event, TextDelta):
                    if event.text:
                        text_fragments.append(event.text)
                        if on_text is not None:
                            on_text(event.text)
                elif isinstance(event, ReasoningDelta):
                    if event.text:
                        reasoning_fragments.append(event.text)
                        if on_reasoning is not None:
                            on_reasoning(event.text)
                elif isinstance(event, ToolCallStart):
                    resolve(event.index, event.call_id or None, event.name or None)
                elif isinstance(event, ToolCallArgsDelta):
                    builder = resolve(event.index, event.call_id or None, event.name)
                    if event.delta:
                        builder.fragments.append(event.delta)
                elif isinstance(event, Usage):
                    usage = event
                elif isinstance(event, Finish):
                    finish = event
        except asyncio.CancelledError:
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        if cancel is not None and cancel.is_set():
            raise RunCancelledError("run cancelled before the model call finished")
        if finish is None:
            raise ProviderStreamError("provider stream ended without a finish event")
        if finish.kind not in FINISH_KIND_TO_NATIVE:
            raise ProviderStreamError(f"provider reported unknown finish kind {finish.kind!r}")
        if finish.kind == "error":
            failure = finish.failure or {}
            detail = failure.get("message") if isinstance(failure, dict) else None
            raise ProviderStreamError(
                f"provider reported an error: {detail!r}" if detail else "provider reported an error"
            )
        tool_calls: list[NativeToolCall] = []
        for builder in ordered:
            if not builder.name:
                raise ProviderStreamError(f"provider sent tool call {builder.call_id} without a name")
            tool_calls.append(
                NativeToolCall(
                    call_id=builder.call_id,
                    name=builder.name,
                    arguments="".join(builder.fragments) or "{}",
                )
            )
        finish_reason = FINISH_KIND_TO_NATIVE[finish.kind]
        if tool_calls and finish_reason != "tool_calls":
            finish_reason = "tool_calls"
        if finish_reason == "tool_calls" and not tool_calls:
            raise ProviderStreamError("provider finished with tool_calls but sent no tool call")
        text = "".join(text_fragments)
        reasoning = "".join(reasoning_fragments)
        if not text and not reasoning and not tool_calls:
            raise ProviderStreamError("provider returned an empty assistant message")
        return ModelResponse(
            text=text,
            reasoning=reasoning,
            tool_calls=tuple(tool_calls),
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        """Close the underlying provider."""
        await self._provider.aclose()


__all__ = [
    "FINISH_KIND_TO_NATIVE",
    "NATIVE_FINISH_REASONS",
    "ModelResponse",
    "NativeProviderAdapter",
    "NativeToolCall",
    "build_request",
    "to_wire_message",
    "to_wire_messages",
]
