"""Context compaction as a durable record transaction.

One compaction appends four records, in this order:

1. compaction/start     - durable lock, carries the compaction id and turn.
2. compaction/summary   - the summarizer output plus shadowed-range metadata.
3. user/message         - the checkpoint message with a replace surfaceOp.
4. compaction/end       - closes the transaction (and carries the error on failure).

The checkpoint replacement is what actually removes the old span from the
derived context; derive_messages in the session workstream applies the surface
fold.  This module never edits earlier records.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from typing import Any

from deeppyro.compact.prompts import COMPACTION_INSTRUCTION, frame_summary
from deeppyro.compact.surface import (
    CompactionRegion,
    entry_state,
    fold_surface,
    latest_tool_schemas,
    select_region,
    surface_messages,
)
from deeppyro.compact.tokens import estimate_message_tokens, estimate_messages_tokens
from deeppyro.core.errors import AbortedError, LlmError, SessionError
from deeppyro.core.ids import new_message_id
from deeppyro.core.protocols import Finish, LlmProvider, LlmRequest, Message, TextDelta, Usage
from deeppyro.core.wire import TextPart

logger = logging.getLogger(__name__)


def _append(session: Any, record_type: str, data: dict[str, Any], **envelope: Any) -> int:
    """Append through a session whose signature may or may not accept envelopes."""
    append = getattr(session, "append", None)
    if append is None:
        raise SessionError("session object has no append()")
    try:
        signature = inspect.signature(append)
    except (TypeError, ValueError):
        supports_envelope = False
    else:
        supports_envelope = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )
    if supports_envelope and envelope:
        return int(append(record_type, data, **envelope))
    return int(append(record_type, data))


def _error_chain(exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {"message": str(exc), "name": type(exc).__name__}
    code = getattr(exc, "code", None)
    if code is not None:
        payload["code"] = code
    return payload


class Compactor:
    """Summarize old context into a checkpoint and record it durably."""

    def __init__(
        self,
        *,
        provider: LlmProvider,
        model: str,
        keep_recent_tokens: int = 8000,
        context_window: int = 32768,
        threshold: float = 0.85,
        max_tokens: int | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.keep_recent_tokens = keep_recent_tokens
        self.context_window = context_window
        self.threshold = threshold
        self.max_tokens = max_tokens

    # -- pressure ----------------------------------------------------------
    def should_compact(self, messages: list[Message]) -> bool:
        """True when the estimated prompt exceeds the configured fraction."""
        if self.context_window <= 0:
            return False
        return estimate_messages_tokens(messages) > self.threshold * self.context_window

    # -- main entry point --------------------------------------------------
    async def compact(self, session: Any, messages: list[Message] | None = None) -> bool:
        """Compact the oldest safe region; return True when a checkpoint was written.

        messages is optional and only used for interface compatibility; the
        actual region is always selected from the session surface so seqs stay
        authoritative.  Returns False when there is no safe range (the
        conversation is still small, or every span would split a
        tool-call/result pair).
        """
        del messages
        records = getattr(session, "records", None)
        if not isinstance(records, list):
            raise SessionError("session does not expose records; cannot select a compaction region")
        state = entry_state(records)
        if state.unmatched_start_seq is not None and (
            state.latest_end_seed_seq is None or state.latest_end_seed_seq < state.unmatched_start_seq
        ):
            raise SessionError("a compaction is already in progress for this session")
        region = select_region(records, self.keep_recent_tokens)
        if region is None:
            return False
        open_turn = state.open_turn
        compaction_id = str(uuid.uuid4())
        lifecycle: dict[str, Any] = {"compactionId": compaction_id}
        if open_turn is not None:
            lifecycle["turn"] = open_turn
        start_seq = _append(session, "compaction/start", lifecycle)
        ended = False
        try:
            summary, usage = await self._summarize(session, records, region)
            # The surface must still match what we selected around the call.
            current = fold_surface(getattr(session, "records", records))
            current_seqs = [node.seq for node in current]
            try:
                start_index = current_seqs.index(region.start)
                end_index = current_seqs.index(region.end)
            except ValueError as exc:
                raise SessionError("surface changed while summarizing; compaction aborted") from exc
            if start_index > end_index:
                raise SessionError("surface changed while summarizing; compaction aborted")
            checkpoint = self._checkpoint_message(compaction_id, summary)
            framed_tokens = estimate_message_tokens(checkpoint)
            if framed_tokens >= region.shadowed_tokens:
                raise LlmError(
                    f"summary is not smaller than the shadowed content ({framed_tokens:.0f} estimated "
                    f"framed tokens >= {region.shadowed_tokens:.0f})",
                    provider=self.provider.name,
                )
            summary_data: dict[str, Any] = {
                "compactionId": compaction_id,
                "summary": [{"type": "text", "text": summary}],
                "rawOutput": [{"type": "text", "text": summary}],
                "llmStreamCall": True,
                "shadowedRange": {"start": region.start, "end": region.end},
                "shadowedSeqs": list(region.shadowed_seqs),
                "shadowedTokenCount": round(region.shadowed_tokens),
                "provider": self.provider.name,
                "model": self.model,
            }
            if self.max_tokens is not None:
                summary_data["maxTokens"] = self.max_tokens
            if usage is not None:
                summary_data["usage"] = {
                    "inputTokens": usage.input_tokens,
                    "outputTokens": usage.output_tokens,
                    "cacheReadTokens": usage.cached_input_tokens or 0,
                    "cacheWriteTokens": 0,
                    "totalTokens": usage.total_tokens,
                }
            summary_seq = _append(session, "compaction/summary", summary_data)
            source_seqs = [start_seq, summary_seq, *region.shadowed_seqs]
            _append(
                session,
                "user/message",
                checkpoint.model_dump(exclude_none=True),
                surfaceOp={"op": "replace", "startSeq": region.start, "endSeq": region.end},
                sourceEventSeqs=source_seqs,
            )
            _append(session, "compaction/end", dict(lifecycle))
            ended = True
            self._verify_checkpoint(session, checkpoint.id)
            return True
        except BaseException as exc:
            if not ended:
                failure_data = dict(lifecycle)
                failure_data["error"] = _error_chain(exc)
                try:
                    _append(session, "compaction/end", failure_data)
                except Exception:
                    logger.exception("failed to close compaction transaction")
            raise

    # -- summarization -----------------------------------------------------
    async def _summarize(
        self, session: Any, records: list[Any], region: CompactionRegion
    ) -> tuple[str, Usage | None]:
        nodes = fold_surface(records)
        prefix = surface_messages(nodes, stop_index=region.start_index)
        instruction = Message(
            role="user",
            content=[TextPart(text=COMPACTION_INSTRUCTION)],
            id=new_message_id(),
            source={"kind": "condensation-instruction"},
        )
        request = LlmRequest(
            provider=self.provider.name,
            model=self.model,
            messages=[*prefix, instruction],
            system=None,
            tools=latest_tool_schemas(records),
            max_tokens=self.max_tokens,
            temperature=None,
            context_window=None,
            session_id=getattr(session, "session_id", None),
        )
        chunks: list[str] = []
        usage: Usage | None = None
        finish: Finish | None = None
        async for event in self.provider.stream(request):
            if isinstance(event, TextDelta):
                chunks.append(event.text)
            elif isinstance(event, Usage):
                usage = event
            elif isinstance(event, Finish):
                finish = event
                break
        if finish is None:
            raise LlmError("summarization stream ended without a finish event", provider=self.provider.name)
        failure = finish.failure or {}
        if finish.kind == "error":
            status = failure.get("status")
            raise LlmError(
                str(failure.get("message", "summarization failed")),
                provider=self.provider.name,
                status=status if isinstance(status, int) else None,
            )
        if finish.kind == "aborted":
            raise AbortedError("summarization was aborted")
        if finish.kind == "length":
            raise LlmError(
                "summarization truncated at the token cap (incomplete checkpoint)",
                provider=self.provider.name,
            )
        if finish.kind == "content-filter":
            raise LlmError("summarization was blocked by a content filter", provider=self.provider.name)
        summary = "".join(chunks).strip()
        if not summary:
            raise LlmError("summarization produced no text summary content", provider=self.provider.name)
        return summary, usage

    # -- checkpoint --------------------------------------------------------
    def _checkpoint_message(self, compaction_id: str, summary: str) -> Message:
        return Message(
            role="user",
            content=[TextPart.model_validate(part) for part in frame_summary(summary)],
            id=new_message_id(),
            source={"kind": "compact-checkpoint", "compactionId": compaction_id},
        )

    def _verify_checkpoint(self, session: Any, checkpoint_id: str) -> None:
        derive = getattr(session, "derive_messages", None)
        if not callable(derive):
            return
        try:
            derived = derive()
        except Exception as exc:
            raise SessionError(f"session derive_messages failed after compaction: {exc}") from exc
        if not any(message.id == checkpoint_id for message in derived):
            raise SessionError("compaction checkpoint is missing from the derived message list")
