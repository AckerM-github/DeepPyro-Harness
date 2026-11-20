"""Native model-call assembly (KI-010).

Consume one :class:`~deeppyro.core.llm.LlmProvider` stream and assemble a
:class:`~deeppyro.core.llm.ModelResponse`. This module replaces the v4
`NativeProviderAdapter` that lived in `agent/native_provider.py`; the
assembly rules are preserved verbatim so the native loop keeps its behavior:

* a `Finish` event is mandatory; an unknown or `ERROR` finish is a
  `ProviderStreamError`;
* every tool call must have a name, its argument fragments are joined and
  default to `"{}"`;
* `finish_reason` is forced to `TOOL_CALLS` whenever tool calls exist, and a
  `TOOL_CALLS` finish without calls is an error;
* an empty assistant message (no text, reasoning or tool calls) is an error;
* cancellation surfaces as `RunCancelledError` before and during the call;
* usage is last-wins, latency is measured around the whole call and scheduled
  transport retries are counted into `ModelResponse.retries` for the trace.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from deeppyro.agent.errors import ProviderStreamError, RunCancelledError
from deeppyro.core.errors import LlmError
from deeppyro.core.ids import new_call_id
from deeppyro.core.llm import (
    Finish,
    LlmProvider,
    ModelRequest,
    ModelResponse,
    NativeToolCall,
    ReasoningDelta,
    RetryScheduled,
    StopReason,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
    UsageEvent,
)

__all__ = ["complete_model_call"]


@dataclass(slots=True)
class _CallBuilder:
    call_id: str
    name: str = ""
    fragments: list[str] = field(default_factory=list)


async def complete_model_call(
    provider: LlmProvider,
    request: ModelRequest,
    *,
    cancel: asyncio.Event | None = None,
    on_text: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
) -> ModelResponse:
    """Consume one streamed provider call and normalize it into native data.

    Raises:
        RunCancelledError: the cancel event was set before or during the call.
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
    retries = 0

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
        async for event in provider.stream(request):
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
            elif isinstance(event, UsageEvent):
                usage = event.usage
            elif isinstance(event, RetryScheduled):
                retries += 1
            elif isinstance(event, Finish):
                finish = event
    except asyncio.CancelledError:
        raise
    except LlmError as exc:
        # A transport failure (HTTP status, timeout, closed stream) is the same
        # user-visible outcome as a malformed in-stream Finish: the model call
        # produced no assistant message. Normalize it here so the loop always
        # closes the turn and never leaves a second run with an open turn.
        raise ProviderStreamError(f"provider request failed: {exc}") from exc
    latency_ms = int((time.monotonic() - started) * 1000)
    if cancel is not None and cancel.is_set():
        raise RunCancelledError("run cancelled before the model call finished")
    if finish is None:
        raise ProviderStreamError("provider stream ended without a finish event")
    try:
        stop_reason = StopReason(finish.stop_reason)
    except ValueError as exc:
        raise ProviderStreamError(f"provider reported unknown finish reason {finish.stop_reason!r}") from exc
    if stop_reason is StopReason.ERROR:
        raise ProviderStreamError(
            f"provider reported an error: {finish.message!r}"
            if finish.message
            else "provider reported an error"
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
    if tool_calls and stop_reason is not StopReason.TOOL_CALLS:
        stop_reason = StopReason.TOOL_CALLS
    if stop_reason is StopReason.TOOL_CALLS and not tool_calls:
        raise ProviderStreamError("provider finished with tool_calls but sent no tool call")
    text = "".join(text_fragments)
    reasoning = "".join(reasoning_fragments)
    if not text and not reasoning and not tool_calls:
        raise ProviderStreamError("provider returned an empty assistant message")
    return ModelResponse(
        text=text,
        reasoning=reasoning,
        tool_calls=tuple(tool_calls),
        finish_reason=stop_reason,
        usage=usage,
        latency_ms=latency_ms,
        retries=retries,
        replay_state=finish.replay_state,
    )
