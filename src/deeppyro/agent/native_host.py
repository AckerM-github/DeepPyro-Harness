"""Reusable native agent host: one open session, many loop runs.

This is the bridge the TUI and headless front ends sit on. It owns exactly one
NativeSession and one AgentLoop; every send() starts a fresh AgentLoop.run on
the same session, so each run rebuilds its context from the session fold and
multi-turn state is simply the append-only log.

Two objects live here:

* NativeAgentHost - the live agent: send/interrupt/aclose plus usage and
  transcript reads. The TUI's turn driver talks to this object.
* NativeSessionHandle - a thin legacy-shaped read surface over the same session,
  so existing front-end services (working directory, status bar, transcript
  rendering) can be pointed at a native session without learning the native
  schema.

The session vocabulary is owned by deeppyro.session.schema; this module only
maps the small set of legacy record types the bridge accepts onto existing
EventKinds and never invents new schema vocabulary. Nothing here depends on the
TUI, and no module-level mutable state is kept, so several hosts can coexist in
one process.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from deeppyro.agent.budget import BudgetLimits
from deeppyro.agent.checkpoint import CheckpointStore
from deeppyro.agent.errors import RunCancelledError
from deeppyro.agent.events import AgentHooks
from deeppyro.agent.loop import AgentLoop, RunResult
from deeppyro.agent.native_records import (
    RECORD_KIND_MAP,
    RecordMapping,
    UnsupportedRecordTypeError,
    append_mapped,
    append_native,
    notice_source,
)
from deeppyro.agent.turn_messages import tool_specs, user_message
from deeppyro.core.ids import new_message_id, now_ms
from deeppyro.core.llm import LlmProvider
from deeppyro.core.messages import (
    Message,
    MessageSource,
    PluginSource,
    SystemSource,
    TextPart,
)
from deeppyro.core.protocols import Tool
from deeppyro.observability.protocols import TraceExportSink, TurnUsage, UsageSink
from deeppyro.observability.trace import ModelCallRecord, RunRecord, ToolCallRecord, TraceStore
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.schema import Event, EventKind, NativeSession

if TYPE_CHECKING:
    from deeppyro.agent.native_factory import PromptFacts, PromptSource

__all__ = [
    "DEFAULT_NOTICE_LIMIT",
    "RECORD_KIND_MAP",
    "NativeAgentHost",
    "NativeHeaderView",
    "NativeNotice",
    "NativeSessionHandle",
    "RecordMapping",
    "UnsupportedRecordTypeError",
]

DEFAULT_NOTICE_LIMIT = 64
"""How many injected notices may wait for the next run before drops are counted."""


@dataclass(frozen=True, slots=True)
class NativeHeaderView:
    """v4-shaped read view over a native SessionHeader.

    Front ends read header.id / header.cwd / header.createdAt (v4 naming) while
    the native header uses id/cwd/created_at. The view also carries the v4-only
    header fields as inert defaults so a v4 snapshot comparison does not treat
    a native session as missing fields.

    The caller-visible identity is part of the contract: the handle builds one
    view and returns that same object forever, because the TUI asserts the
    header object was not replaced mid-session.
    """

    id: str
    created_at: int
    cwd: str
    preset: str
    parent_id: str | None
    origin: str
    delegation_depth: int

    @property
    def createdAt(self) -> int:  # noqa: N802 - v4-shaped read surface
        return self.created_at

    @property
    def isSeeded(self) -> bool:  # noqa: N802 - v4-shaped read surface
        """Native sessions have no seed log yet, so they are never seeded."""
        return False

    @property
    def delegationDepth(self) -> int:  # noqa: N802 - v4-shaped read surface
        return self.delegation_depth

    @property
    def agentPreset(self) -> str:  # noqa: N802 - v4-shaped read surface
        return self.preset

    @property
    def parentSession(self) -> str | None:  # noqa: N802 - v4-shaped read surface
        return self.parent_id

    @property
    def model(self) -> None:
        """The v4 header carried the boot model; the native header does not."""
        return None

    @property
    def system(self) -> None:
        """The v4 header could carry a system prompt; the native header cannot."""
        return None


@dataclass(frozen=True, slots=True)
class NativeNotice:
    """One out-of-band notice queued for the next run."""

    text: str
    source: Mapping[str, Any] | None = None


def _source_payload(source: MessageSource | None) -> dict[str, Any] | None:
    """Flatten one message source to the mapping NativeSessionHandle.inject accepts."""
    if isinstance(source, PluginSource):
        return {"plugin": source.plugin}
    return None


class NativeSessionHandle:
    """Thin, v4-shaped adapter over one NativeSession.

    Read surface (what the TUI and its services may rely on):

    * session_id, header (cached NativeHeaderView), native_session/native_header
    * append(record_type, data) -> seq for the record types in RECORD_KIND_MAP
    * inject(text, source=None) -> bool plus pending/injected/dropped counters
    * usage() -> {input, output, cached}; context_tokens() -> int
    * derive_messages() / messages() -> list[native Message]
    * aclose()

    The handle is not a store and not a writer beyond append(): it deliberately
    exposes no arbitrary v4 attributes, no record stream and no projection
    callbacks. Reads keep working after aclose() because the fold uses the
    in-memory log; writes and injects require an open session.
    """

    def __init__(self, session: NativeSession, *, notice_limit: int = DEFAULT_NOTICE_LIMIT) -> None:
        if notice_limit < 1:
            raise ValueError("notice_limit must be at least 1")
        self._session = session
        self._header = NativeHeaderView(
            id=session.header.id,
            created_at=session.header.created_at,
            cwd=session.header.cwd,
            preset=session.header.preset,
            parent_id=session.header.parent_id,
            origin=session.header.origin,
            delegation_depth=session.header.delegation_depth,
        )
        self._notice_limit = notice_limit
        self._notices: deque[NativeNotice] = deque(maxlen=notice_limit)
        self._injected_count = 0
        self._dropped_count = 0
        self._usage_input = 0
        self._usage_output = 0
        self._usage_cached = 0
        self._context_tokens = 0
        self._fold_cache: tuple[int, SessionView] | None = None

    # ------------------------------------------------------------- identity

    @property
    def session_id(self) -> str:
        """Durable native session id, stable for the lifetime of the handle."""
        return self._session.header.id

    @property
    def header(self) -> NativeHeaderView:
        """The cached v4-shaped header view (same object on every access)."""
        return self._header

    @property
    def native_session(self) -> NativeSession:
        """The underlying native session, for hosts that need the raw log."""
        return self._session

    @property
    def native_header(self) -> Any:
        """The raw native SessionHeader (snake_case fields)."""
        return self._session.header

    # ---------------------------------------------------------------- writes

    def append(self, record_type: str, data: Mapping[str, Any] | None = None) -> int:
        """Map one v4 record type onto its native event and append it.

        Returns the appended event sequence number. Unknown record types raise
        UnsupportedRecordTypeError before anything is written; known types are
        still validated by the native schema, so a mismatched payload raises the
        schema's own validation error.
        """
        self._ensure_open()
        return append_mapped(self._session, record_type, data).seq

    def append_native(
        self,
        kind: EventKind | str,
        data: Mapping[str, Any] | None = None,
        **envelope: Any,
    ) -> Event:
        """Append one schema event directly, forwarding the shared envelope fields.

        This is the native counterpart of append(): callers that already have a
        schema EventKind (goal.changed, task.*, ...) append it without going
        through the v4 record mapping.
        """
        self._ensure_open()
        return append_native(self._session, kind, data, **envelope)

    def inject(self, text: str, source: Mapping[str, Any] | None = None) -> bool:
        """Queue a notice for the next run; False when the bounded queue is full.

        Queued notices are appended as user messages by the host before the
        next send() folds its context. A full queue drops the notice, increments
        dropped_count and returns False, so the loss is observable instead of
        silent.
        """
        self._ensure_open()
        if not isinstance(text, str) or not text.strip():
            raise ValueError("notice text must be a non-empty string")
        if len(self._notices) >= self._notice_limit:
            self._dropped_count += 1
            return False
        self._notices.append(NativeNotice(text=text, source=dict(source) if source is not None else None))
        self._injected_count += 1
        return True

    def _drain_notices(self) -> list[Message]:
        """Turn queued notices into native user messages, oldest first."""
        if not self._notices:
            return []
        timestamp = max(now_ms(), self._session.last_ts)
        messages: list[Message] = []
        while self._notices:
            notice = self._notices.popleft()
            messages.append(
                Message(
                    id=new_message_id(),
                    role="user",
                    content=(TextPart(text=notice.text),),
                    created_at=timestamp,
                    source=notice_source(notice.source),
                )
            )
        return messages

    # ----------------------------------------------------------------- reads

    @property
    def events(self) -> tuple[Event, ...]:
        """The session events as of the last append (read-only, no lock)."""
        return self._session.events

    def fold(self) -> SessionView:
        """Fold the session into a SessionView, cached by last sequence number.

        The cache is keyed by the session's last seq, so a caller may fold
        repeatedly (refresh_prompt, transcript reads) and only pay for new
        events. Reads use the in-memory log and never take the write lock.
        """
        last_seq = self._session.last_seq
        cached = self._fold_cache
        if cached is not None and cached[0] == last_seq:
            return cached[1]
        view = fold(self._session.header, self._session.events)
        self._fold_cache = (last_seq, view)
        return view

    def usage(self) -> dict[str, int]:
        """Cumulative input/output/cached tokens over every finished turn."""
        return {
            "input": self._usage_input,
            "output": self._usage_output,
            "cached": self._usage_cached,
        }

    def context_tokens(self) -> int:
        """Prompt tokens of the newest model call, i.e. the live context size."""
        return self._context_tokens

    def derive_messages(self, *, strict: bool | None = None) -> list[Message]:
        """Rebuild the transcript from the session fold, like the v4 Session API."""
        view = self.fold()
        if strict and view.issues:
            raise ValueError(f"session fold reported {len(view.issues)} issue(s)")
        return list(view.messages)

    def messages(self) -> list[Message]:
        """Alias for derive_messages() used by the host read surface."""
        return self.derive_messages()

    @property
    def notice_limit(self) -> int:
        """Maximum number of notices that may wait for the next run."""
        return self._notice_limit

    @property
    def pending_notices(self) -> tuple[NativeNotice, ...]:
        """Notices queued for the next run, oldest first."""
        return tuple(self._notices)

    @property
    def injected_count(self) -> int:
        """Total notices accepted into the queue since the handle was created."""
        return self._injected_count

    @property
    def dropped_count(self) -> int:
        """Total notices rejected because the queue was full."""
        return self._dropped_count

    @property
    def closed(self) -> bool:
        """Whether the underlying native session is closed."""
        return self._session.closed

    def aclose(self) -> None:
        """Close the native session and release its lock (idempotent)."""
        self._session.close()

    # --------------------------------------------------------------- private

    def _ensure_open(self) -> None:
        if self._session.closed:
            raise ValueError("session is closed")

    def _account_turn(self, usage: TurnUsage, /) -> None:
        self._usage_input += int(usage.input_tokens)
        self._usage_output += int(usage.output_tokens)
        self._usage_cached += int(usage.cached_tokens)

    def _set_context_tokens(self, tokens: int) -> None:
        self._context_tokens = int(tokens)


class _UsageTap:
    """UsageSink service: account into the handle, then chain to the caller's sink."""

    def __init__(self, handle: NativeSessionHandle, inner: UsageSink | None) -> None:
        self._handle = handle
        self._inner = inner

    def record_turn(self, usage: TurnUsage, /) -> object:
        self._handle._account_turn(usage)
        if self._inner is not None:
            return self._inner.record_turn(usage)
        return usage


class _ContextTap:
    """TraceExportSink service: track the live prompt size, then chain the export.

    Installing this tap also makes the loop build model-call trace records even
    when no TraceStore was configured, which is what makes context_tokens() live
    instead of stale.
    """

    def __init__(self, handle: NativeSessionHandle, inner: TraceExportSink | None) -> None:
        self._handle = handle
        self._inner = inner

    def export_trace_record(self, record: RunRecord | ModelCallRecord | ToolCallRecord, /) -> None:
        if isinstance(record, ModelCallRecord):
            self._handle._set_context_tokens(record.input_tokens)
        if self._inner is not None:
            self._inner.export_trace_record(record)


class NativeAgentHost:
    """One native session driven by repeated AgentLoop runs.

    The host owns the session, the loop, the provider and the tools. Each
    send(text) awaits one fresh AgentLoop.run on the same session; the loop
    folds the log on every step, so later runs see earlier turns without the
    host copying context. Streaming reaches the caller through the supplied
    AgentHooks instance unchanged.

    The host also installs two observability taps (usage and trace export) into
    the loop's service bag so usage()/context_tokens() stay live while chaining
    to any caller-provided UsageSink/TraceExportSink services.
    """

    def __init__(
        self,
        *,
        session: NativeSession,
        provider: LlmProvider,
        model: str,
        tools: Iterable[Tool] = (),
        cwd: str | Path = ".",
        hooks: AgentHooks | None = None,
        budget: BudgetLimits | None = None,
        checkpoint_store: CheckpointStore | None = None,
        trace_store: TraceStore | None = None,
        temperature: float | None = None,
        max_tokens_per_call: int | None = None,
        provider_name: str | None = None,
        cost_per_million_tokens: float | None = None,
        permission: Any | None = None,
        services: Mapping[str, Any] | None = None,
        notice_limit: int = DEFAULT_NOTICE_LIMIT,
    ) -> None:
        if session.closed:
            raise ValueError("session is closed")
        self._session = session
        self._handle = NativeSessionHandle(session, notice_limit=notice_limit)
        self._task: asyncio.Task[RunResult] | None = None
        self._closed = False
        self._tools = tuple(tools)
        self._prompt_source: PromptSource | None = None
        self._prompt_facts: PromptFacts | None = None
        self._pending_callback: Callable[[], None] | None = None
        service_map: dict[str, Any] = dict(services or {})
        usage_inner = service_map.get("usage")
        trace_inner = service_map.get("trace_export")
        service_map["usage"] = _UsageTap(self._handle, usage_inner)
        service_map["trace_export"] = _ContextTap(self._handle, trace_inner)
        self._loop = AgentLoop(
            session=session,
            provider=provider,
            model=model,
            tools=tools,
            cwd=cwd,
            hooks=hooks,
            budget=budget,
            checkpoint_store=checkpoint_store,
            trace_store=trace_store,
            temperature=temperature,
            max_tokens_per_call=max_tokens_per_call,
            provider_name=provider_name,
            cost_per_million_tokens=cost_per_million_tokens,
            permission=permission,
            services=service_map,
        )

    # ------------------------------------------------------------- lifecycle

    async def send(self, text: str, *, source: MessageSource | None = None) -> RunResult:
        """Run one fresh turn on the owned session and stream it through hooks.

        The system prompt is refreshed first (a no-op unless it changed), then
        any notices queued by inject() become user messages in the log, so the
        new run folds both into its context. source defaults to UserSource().
        Raises asyncio.CancelledError when the run was interrupted.
        """
        self._ensure_ready()
        if not isinstance(text, str) or not text:
            raise ValueError("send requires a non-empty prompt")
        self._ensure_idle()
        self.refresh_prompt()
        self._drain_notices_into_log()
        return await self._await_run(asyncio.create_task(self._loop.run(text, source=source)))

    async def resume(self) -> RunResult:
        """Continue a session whose previous run did not finish.

        Repairs any dangling step/turn in the log (no fabricated messages), then
        starts a new run without appending a user message. Refuses while busy.
        """
        self._ensure_ready()
        self._ensure_idle()
        self.refresh_prompt()
        self._drain_notices_into_log()
        return await self._await_run(asyncio.create_task(self._loop.run(None, resume=True)))

    def deliver(self, text: str, *, source: MessageSource | None = None) -> bool:
        """Append one user message now, or queue it when a run is in flight.

        Returns True when the text was queued as a notice for the next run and
        False when it was appended to the log immediately. The pending callback
        (see on_pending) fires after a successful queue.
        """
        self._ensure_ready()
        if not isinstance(text, str) or not text:
            raise ValueError("deliver requires a non-empty prompt")
        if self.busy:
            accepted = self._handle.inject(text, _source_payload(source))
            if accepted:
                self._notify_pending()
            return accepted
        message = user_message(text, source=source, created_at=self._next_ts())
        self._session.append(EventKind.USER_MESSAGE, {"message": message.to_dict()})
        return False

    def on_pending(self, callback: Callable[[], None] | None) -> None:
        """Register the single callback fired when deliver() had to queue text."""
        if callback is not None and not callable(callback):
            raise ValueError("on_pending callback must be callable or None")
        self._pending_callback = callback

    async def wait_for_idle(self) -> None:
        """Wait for the in-flight run to finish; returns immediately when idle."""
        task = self._task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(task)

    def restore(self) -> bool:
        """Close an interrupted open turn without fabricating content.

        Returns True when a dangling turn (and step, if any) was closed in the
        log as aborted-by-disposed.
        """
        self._ensure_ready()
        return self._loop.repair_open_turn({"kind": "aborted", "by": "disposed"})

    def attach_prompt(self, source: PromptSource, facts: PromptFacts | None = None) -> bool:
        """Attach the system-prompt renderer and render it into the log now.

        Rendering is the host's only system.message writer; returns True when a
        system.message fact was written. facts is stored for callers that
        inspect the build but is not required to render.
        """
        self._ensure_ready()
        self._prompt_source = source
        self._prompt_facts = facts
        return self.refresh_prompt()

    def refresh_prompt(self) -> bool:
        """Render the system prompt and persist it when it changed.

        The fold keeps system.message last-wins and out of the message sequence,
        so re-rendering an unchanged prompt writes nothing. Never called
        mid-turn: send()/resume() refresh before starting a run.
        """
        self._ensure_ready()
        source = self._prompt_source
        if source is None:
            return False
        view = self._handle.fold()
        text = source.render(view.header, tool_specs(self._tools))
        if not text:
            return False
        current = view.system.text() if view.system is not None else ""
        if text == current:
            return False
        message = Message(
            id=new_message_id(),
            role="system",
            content=(TextPart(text=text),),
            created_at=self._next_ts(),
            source=SystemSource(),
        )
        self._session.append(EventKind.SYSTEM_MESSAGE, {"message": message.to_dict()})
        return True

    def last_assistant_text(self) -> str:
        """The last assistant text in the folded transcript (empty when none)."""
        for message in reversed(self._handle.fold().messages):
            if message.role == "assistant":
                return message.text()
        return ""

    def turn_count(self) -> int:
        """How many turns were started in this session so far."""
        return sum(1 for event in self._session.events if event.type == EventKind.TURN_STARTED.value)

    async def _await_run(self, task: asyncio.Task[RunResult]) -> RunResult:
        """Await one loop run, translating cooperative stops into cancellations."""
        self._task = task
        try:
            return await task
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            # A cancelled run has already persisted its aborted turn/run records.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise
        except RunCancelledError as exc:
            # A cooperative stop (provider/tool consumed the cancellation) is
            # still a cancellation from the front end's point of view.
            raise asyncio.CancelledError from exc
        finally:
            if self._task is task:
                self._task = None

    def _ensure_ready(self) -> None:
        if self._closed or self._session.closed:
            raise ValueError("session is closed")

    def _ensure_idle(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("agent host already has a send in flight")

    def _drain_notices_into_log(self) -> None:
        for notice in self._handle._drain_notices():
            self._session.append(EventKind.USER_MESSAGE, {"message": notice.to_dict()})

    def _next_ts(self) -> int:
        return max(now_ms(), self._session.last_ts)

    def _notify_pending(self) -> None:
        callback = self._pending_callback
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback()

    async def interrupt(self) -> bool:
        """Cancel the in-flight run; True when a run was actually interrupted.

        The loop persists the aborted turn.ended/run.finished records before the
        task finishes, so the log stays replayable. No-op when idle.
        """
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True

    async def aclose(self) -> None:
        """Cancel an in-flight run, then close the session and release the lock."""
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
        self._handle.aclose()

    async def __aenter__(self) -> NativeAgentHost:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- reads

    @property
    def handle(self) -> NativeSessionHandle:
        """The v4-shaped adapter the TUI uses as its session object."""
        return self._handle

    @property
    def native_session(self) -> NativeSession:
        """The owned native session log."""
        return self._session

    @property
    def session_id(self) -> str:
        """Durable native session id."""
        return self._session.header.id

    @property
    def busy(self) -> bool:
        """Whether a run is in flight."""
        return self._task is not None and not self._task.done()

    @property
    def closed(self) -> bool:
        """Whether aclose() has run or the session was closed underneath."""
        return self._closed or self._session.closed

    def usage(self) -> dict[str, int]:
        """Cumulative input/output/cached tokens, delegated to the handle."""
        return self._handle.usage()

    def context_tokens(self) -> int:
        """Live prompt size of the newest model call, delegated to the handle."""
        return self._handle.context_tokens()

    def messages(self) -> list[Message]:
        """Session transcript from the fold, delegated to the handle."""
        return self._handle.messages()

    def derive_messages(self, *, strict: bool | None = None) -> list[Message]:
        """Session transcript from the fold, delegated to the handle."""
        return self._handle.derive_messages(strict=strict)
