"""Native (schema 1) automatic compaction over the folded session view.

The v4 compactor writes a start/summary/end bracket of records and reconstructs
the visible surface from them.  A native session already folds into a
SessionView, so compaction becomes one fact: a single compaction.done event that
covers a contiguous range of message ids and inserts one summary message.  There
is no start record, no partial state and no surface bookkeeping, so replay needs
nothing but the session log.

Token estimates come from agent/context_plan (the single estimator in the
native runtime), never from the v4 compact/tokens module.  Provider usage,
latency and retry counts stay out of the session log and go to the trace store
only; the session records facts.

Region selection is deliberately conservative:

1. Pinned prefix: a range may not start before or inside a CompactionSource
   summary, so compaction ranges stay contiguous after any earlier fold.
2. Keep-recent tail: walk newest to oldest until at least keep_recent_tokens
   are retained, so the most recent exchange always survives verbatim.
3. Tool-pair safety: a cut may not split an assistant tool call from its tool
   result.  The cut before the tail is extended left until safe, and the start
   is moved right past an unmatched tool result, if any.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from deeppyro.agent.context_plan import estimate_message_tokens, message_tokens
from deeppyro.agent.errors import ProviderStreamError
from deeppyro.agent.model_call import complete_model_call
from deeppyro.compact.blocks import BlockStore
from deeppyro.compact.native_refs import RefStore
from deeppyro.compact.prompts import COMPACTION_INSTRUCTION, frame_summary
from deeppyro.core.errors import AbortedError, LlmError, SessionError
from deeppyro.core.ids import new_message_id, new_span_id, now_ms
from deeppyro.core.llm import LlmProvider, ModelRequest, ModelResponse, StopReason, ToolSpec
from deeppyro.core.messages import (
    CompactionSource,
    Message,
    PluginSource,
    TextPart,
    message_tool_calls,
    part_from_dict,
)
from deeppyro.observability.trace import ModelCallRecord, prompt_hash
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.schema import Event, EventKind, NativeSession

logger = logging.getLogger(__name__)

#: Envelope fields a compaction.done event may carry when appended inside a turn.
ENVELOPE_KEYS = ("run_id", "turn_id", "step_id", "trace_id")

#: Instruction plugin identity used for the summarization request message.
INSTRUCTION_PLUGIN = "compact"


@dataclass(frozen=True, slots=True)
class CompactionRegion:
    """One contiguous, tool-safe range of visible messages to compact."""

    start_index: int
    end_index: int
    message_ids: tuple[str, ...]
    message_tokens: int

    def __post_init__(self) -> None:
        if self.start_index < 0 or self.end_index < self.start_index:
            raise ValueError("compaction region indices must be non-negative and ordered")
        if len(self.message_ids) != self.end_index - self.start_index + 1:
            raise ValueError("compaction region message ids must match its index range")
        if self.message_tokens < 0:
            raise ValueError("compaction region token count must be non-negative")

    @property
    def message_count(self) -> int:
        """Number of messages the region covers."""
        return len(self.message_ids)


@dataclass(frozen=True, slots=True)
class CompactionDone:
    """The result of one successful native compaction."""

    compaction_id: str
    region: CompactionRegion
    summary_message: Message
    event: Event
    response: ModelResponse | None = None
    block_id: str | None = None


def _native_session(session: NativeSession | object) -> NativeSession:
    """Accept a NativeSession or a NativeSessionHandle-shaped adapter."""
    native = getattr(session, "native_session", session)
    if not isinstance(native, NativeSession):
        raise TypeError("native compaction requires a NativeSession or a native session handle")
    return native


def _view_of(session: NativeSession | object) -> SessionView:
    return fold(_native_session(session).header, _native_session(session).events)


def _pinned_prefix_end(messages: Sequence[Message]) -> int:
    """Index just after the last pinned compaction summary in *messages*."""
    end = 0
    for index, message in enumerate(messages):
        if isinstance(message.source, CompactionSource):
            end = index + 1
    return end


def tool_safe_cuts(messages: Sequence[Message]) -> tuple[bool, ...]:
    """Return tool-pair safety for every cut in *messages*.

    Element i describes the cut immediately before messages[i] (cut 0 is the
    start of the view, cut len(messages) is the end).  A cut is safe when every
    tool call opened before it also closed before it, and when the message right
    after the cut is not a tool result (a range may not begin with a result).
    """
    cuts: list[bool] = []
    open_calls: set[str] = set()
    for index, message in enumerate(messages):
        boundary_ok = index == 0 or message.role != "tool"
        cuts.append(boundary_ok and not open_calls)
        for call in message_tool_calls(message):
            open_calls.add(call.id)
        if message.role == "tool" and message.tool_call_id:
            open_calls.discard(message.tool_call_id)
    cuts.append(not open_calls)
    return tuple(cuts)


def select_region(
    messages: Sequence[Message],
    keep_recent_tokens: int,
) -> CompactionRegion | None:
    """Select the oldest compactable range, or None when nothing may be compacted."""
    total = len(messages)
    if total == 0:
        return None
    start_limit = _pinned_prefix_end(messages)
    if start_limit >= total:
        return None
    tokens = [estimate_message_tokens(message) for message in messages]
    cuts = tool_safe_cuts(messages)
    keep_from = total
    kept = 0
    for index in range(total - 1, start_limit - 1, -1):
        kept += tokens[index]
        if kept >= keep_recent_tokens:
            keep_from = index
            break
    if keep_from == total:
        # The retained tail already exceeds the budget for everything visible.
        return None
    while keep_from > start_limit and not cuts[keep_from]:
        keep_from -= 1
    start = start_limit
    while start < keep_from and not cuts[start]:
        start += 1
    if start >= keep_from:
        return None
    covered = tuple(messages[start:keep_from])
    if not covered:
        return None
    return CompactionRegion(
        start_index=start,
        end_index=keep_from - 1,
        message_ids=tuple(message.id for message in covered),
        message_tokens=sum(tokens[start:keep_from]),
    )


def should_compact_before_send(
    view_or_messages: SessionView | Sequence[Message],
    *,
    context_window: int,
    threshold: float = 0.85,
) -> bool:
    """Pure native pre-send policy: compact before sending when over budget.

    This is the callable the runtime may wire into a pre-send hook.  It does not
    append anything; it only answers the policy question from the folded view.
    """
    if context_window <= 0:
        return False
    if isinstance(view_or_messages, SessionView):
        messages: Sequence[Message] = view_or_messages.messages
    else:
        messages = tuple(view_or_messages)
    return message_tokens(messages) > threshold * context_window


def _instruction_message() -> Message:
    return Message(
        role="user",
        content=(TextPart(COMPACTION_INSTRUCTION),),
        source=PluginSource(INSTRUCTION_PLUGIN),
        id=new_message_id(),
        created_at=now_ms(),
    )


def _summary_message(summary_text: str, compaction_id: str) -> Message:
    framed = tuple(part_from_dict(part) for part in frame_summary(summary_text))
    return Message(
        role="user",
        content=framed,
        source=CompactionSource(compaction_id),
        id=new_message_id(),
        created_at=now_ms(),
    )


def _prompt_payload(request: ModelRequest) -> dict[str, Any]:
    """Provider prompt payload in the same shape the agent loop hashes."""
    return {
        "provider": request.provider,
        "model": request.model,
        "system": request.system.to_dict() if request.system is not None else None,
        "messages": [message.to_dict() for message in request.messages],
        "tools": [spec.to_dict() for spec in request.tools],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }


def _envelope(envelope: Mapping[str, Any] | None) -> dict[str, str]:
    if not envelope:
        return {}
    return {
        key: str(envelope[key])
        for key in ENVELOPE_KEYS
        if isinstance(envelope.get(key), str) and envelope.get(key)
    }


class NativeCompactor:
    """Manual and pre-send automatic compaction for native sessions."""

    def __init__(
        self,
        provider: LlmProvider,
        model: str,
        *,
        keep_recent_tokens: int = 8000,
        context_window: int = 32768,
        threshold: float = 0.85,
        max_tokens: int | None = None,
        blocks: BlockStore | None = None,
        refs: RefStore | None = None,
        trace_store: Any | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.keep_recent_tokens = keep_recent_tokens
        self.context_window = context_window
        self.threshold = threshold
        self.max_tokens = max_tokens
        self.blocks = blocks
        self.refs = refs
        self.trace_store = trace_store

    @property
    def provider_name(self) -> str:
        return getattr(self.provider, "name", "") or "provider"

    def should_compact(self, view_or_messages: SessionView | Sequence[Message]) -> bool:
        """Return True when the visible context exceeds the compaction threshold."""
        return should_compact_before_send(
            view_or_messages,
            context_window=self.context_window,
            threshold=self.threshold,
        )

    def select(self, view: SessionView) -> CompactionRegion | None:
        """Return the region this compactor would summarize, without calling a model."""
        return select_region(view.messages, self.keep_recent_tokens)

    async def compact(
        self,
        session: NativeSession | object,
        *,
        view: SessionView | None = None,
        tools: Sequence[ToolSpec] = (),
        envelope: Mapping[str, Any] | None = None,
    ) -> CompactionDone | None:
        """Compact the oldest safe range, appending exactly one compaction.done.

        Returns None when nothing is compactable.  Any failure (provider error,
        changed session, summary not smaller) raises before or without appending,
        so the log never carries a partial compaction.
        """
        native = _native_session(session)
        current = view if view is not None else fold(native.header, native.events)
        region = select_region(current.messages, self.keep_recent_tokens)
        if region is None:
            return None
        compaction_id = str(uuid.uuid4())
        request = ModelRequest(
            provider=self.provider_name,
            model=self.model,
            messages=(*current.messages[: region.start_index], _instruction_message()),
            system=current.system,
            tools=tuple(tools),
            max_tokens=self.max_tokens,
            temperature=None,
            context_window=None,
            session_id=current.header.id,
        )
        try:
            response = await complete_model_call(self.provider, request)
        except ProviderStreamError as exc:
            raise LlmError(f"summarization failed: {exc}", provider=self.provider_name) from exc
        summary = self._summary_text(response)
        self._verify_region(native, current, region)
        summary_message = _summary_message(summary, compaction_id)
        framed = estimate_message_tokens(summary_message)
        if framed >= region.message_tokens:
            raise LlmError(
                f"summary is not smaller than the shadowed content "
                f"({framed:.0f} estimated framed tokens >= {region.message_tokens:.0f})",
                provider=self.provider_name,
            )
        assoc = _envelope(envelope)
        event = native.append(
            EventKind.COMPACTION_DONE,
            {
                "compaction_id": compaction_id,
                "covers": list(region.message_ids),
                "message": summary_message.to_dict(),
            },
            run_id=assoc.get("run_id"),
            turn_id=assoc.get("turn_id"),
            step_id=assoc.get("step_id"),
            trace_id=assoc.get("trace_id"),
        )
        self._verify_append(native, summary_message)
        block_id = self._record_block(current, region, compaction_id, summary)
        self._record_trace(request, response, region, assoc)
        return CompactionDone(
            compaction_id=compaction_id,
            region=region,
            summary_message=summary_message,
            event=event,
            response=response,
            block_id=block_id,
        )

    # -- steps -------------------------------------------------------------
    def _summary_text(self, response: ModelResponse) -> str:
        reason = response.finish_reason
        if reason is StopReason.ABORTED:
            raise AbortedError("summarization was aborted")
        if reason is StopReason.LENGTH:
            raise LlmError(
                "summarization truncated at the token cap (incomplete checkpoint)",
                provider=self.provider_name,
            )
        if reason is StopReason.CONTENT_FILTER:
            raise LlmError(
                "summarization was blocked by a content filter",
                provider=self.provider_name,
            )
        if reason is StopReason.TOOL_CALLS:
            raise LlmError(
                "summarization attempted to call tools; expected a plain text summary",
                provider=self.provider_name,
            )
        text = response.text.strip()
        if not text:
            raise LlmError(
                "summarization produced no text summary content",
                provider=self.provider_name,
            )
        return text

    def _verify_region(
        self,
        native: NativeSession,
        view: SessionView,
        region: CompactionRegion,
    ) -> None:
        latest = fold(native.header, native.events)
        visible = tuple(message.id for message in latest.messages[region.start_index : region.end_index + 1])
        if visible != region.message_ids:
            raise SessionError("the session changed while summarizing; compaction aborted")
        if latest.header.id != view.header.id:  # pragma: no cover - defensive
            raise SessionError("the session changed while summarizing; compaction aborted")

    def _verify_append(self, native: NativeSession, summary_message: Message) -> None:
        latest = fold(native.header, native.events)
        if not any(message.id == summary_message.id for message in latest.messages):
            raise SessionError("the compaction summary is missing from the folded session after append")

    def _record_trace(
        self,
        request: ModelRequest,
        response: ModelResponse,
        region: CompactionRegion,
        assoc: Mapping[str, str],
    ) -> None:
        if self.trace_store is None:
            return
        if not all(assoc.get(key) for key in ENVELOPE_KEYS):
            return
        usage = response.usage
        try:
            record = ModelCallRecord(
                trace_id=assoc["trace_id"],
                span_id=new_span_id(),
                run_id=assoc["run_id"],
                turn_id=assoc["turn_id"],
                step_id=assoc["step_id"],
                provider=request.provider,
                model=request.model,
                prompt_hash=prompt_hash(_prompt_payload(request)),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=response.latency_ms,
                finish_reason=response.finish_reason,
                retry_count=response.retries,
                ts=now_ms(),
                cached_input_tokens=usage.cached_input_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                context_message_ids=region.message_ids,
            )
            self.trace_store.append(record)
        except Exception:
            logger.exception("could not append the compaction trace record")

    def _record_block(
        self,
        view: SessionView,
        region: CompactionRegion,
        compaction_id: str,
        summary: str,
    ) -> str | None:
        if self.blocks is None:
            return None
        start_ref = ""
        end_ref = ""
        if self.refs is not None:
            try:
                plan = self.refs.plan(view.header.id, view)
                start_ref = plan.ref_for(region.message_ids[0]) or ""
                end_ref = plan.ref_for(region.message_ids[-1]) or ""
            except Exception:
                logger.exception("could not resolve refs for the compaction block")
        try:
            record = self.blocks.add(
                view.header.id,
                start_ref=start_ref,
                end_ref=end_ref,
                start_seq=region.start_index,
                end_seq=region.end_index,
                title=f"Automatic compaction {region.start_index}–{region.end_index}",
                summary=summary,
                source="auto",
                compaction_id=compaction_id,
                start_message_id=region.message_ids[0],
                end_message_id=region.message_ids[-1],
            )
            return record.id
        except Exception:
            logger.exception("could not record the automatic compaction block")
            return None


__all__ = [
    "ENVELOPE_KEYS",
    "INSTRUCTION_PLUGIN",
    "CompactionDone",
    "CompactionRegion",
    "NativeCompactor",
    "select_region",
    "should_compact_before_send",
    "tool_safe_cuts",
]
