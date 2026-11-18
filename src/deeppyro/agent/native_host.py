"""Reusable native agent host: one open session, many loop runs.

This is the bridge the TUI migration (KI-001) will sit on while the v4 runtime
is ported to the M2 native loop. It owns exactly one NativeSession and one
AgentLoop; every send() starts a fresh AgentLoop.run on the same session, so
each run rebuilds its context from the session fold and multi-turn state is
simply the append-only log.

Two objects live here:

* NativeAgentHost - the live agent: send/interrupt/aclose plus usage and
  transcript reads. The TUI's turn driver talks to this object.
* NativeSessionHandle - a thin, v4-shaped read surface over the same session,
  so existing front-end services (working directory, status bar, transcript
  rendering) can be pointed at a native session without learning the native
  schema.

The session vocabulary is owned by deeppyro.session.schema; this module only
maps the small set of v4 record types the bridge accepts onto existing
EventKinds and never invents new schema vocabulary. Nothing here imports
deeppyro.core.wire or the TUI, and no module-level mutable state is kept, so
several hosts can coexist in one process.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any

from deeppyro.agent.budget import BudgetLimits
from deeppyro.agent.checkpoint import CheckpointStore
from deeppyro.agent.errors import RunCancelledError
from deeppyro.agent.events import AgentHooks
from deeppyro.agent.loop import AgentLoop, RunResult
from deeppyro.core.ids import new_message_id, now_ms
from deeppyro.core.messages import (
    Message,
    MessageSource,
    PluginSource,
    TextPart,
    UserSource,
)
from deeppyro.core.protocols import LlmProvider, Tool
from deeppyro.observability.protocols import TraceExportSink, TurnUsage, UsageSink
from deeppyro.observability.trace import ModelCallRecord, RunRecord, ToolCallRecord, TraceStore
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeSession

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


class UnsupportedRecordTypeError(ValueError):
    """A record type outside the adapter's explicit mapping table was appended."""

    def __init__(self, record_type: str) -> None:
        self.record_type = record_type
        supported = ", ".join(sorted(RECORD_KIND_MAP))
        super().__init__(
            f"native host does not accept record type {record_type!r}; supported types: {supported}"
        )


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


def _notice_source(source: Mapping[str, Any] | None, /) -> MessageSource:
    """Pick the native MessageSource for a queued notice."""
    if source is not None:
        plugin = source.get("plugin") or source.get("kind")
        if isinstance(plugin, str) and plugin:
            return PluginSource(plugin=plugin)
    return UserSource()


def _normalise_title(data: Mapping[str, Any], /) -> dict[str, Any]:
    """Flatten v4's {"source": {"kind": ...}} title source to the native string."""
    source = data.get("source")
    kind = "user"
    if isinstance(source, Mapping):
        raw_kind = source.get("kind")
        if isinstance(raw_kind, str):
            kind = raw_kind
    elif isinstance(source, str):
        kind = source
    title = data.get("title", "")
    return {
        "title": title if isinstance(title, str) else str(title),
        "source": "llm" if kind == "llm" else "user",
    }


@dataclass(frozen=True, slots=True)
class RecordMapping:
    """One v4 record type and the native event it becomes."""

    kind: EventKind
    note: str
    normalise: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None


RECORD_KIND_MAP: Mapping[str, RecordMapping] = MappingProxyType(
    {
        "agent-preset/selected": RecordMapping(
            EventKind.PRESET_SELECTED,
            "v4 preset switch; the payload is already native-shaped ({preset, mode, ...}).",
        ),
        "model/selection": RecordMapping(
            EventKind.MODEL_SELECTED,
            "v4 model switch ({provider, model}).",
        ),
        "sandbox/mode": RecordMapping(
            EventKind.SANDBOX_MODE,
            "v4 sandbox mode; the values match the native enum.",
        ),
        "approval/policy": RecordMapping(
            EventKind.PERMISSION_POLICY,
            "v4 approval policy; ask/never match the native enum.",
        ),
        "working-directory/change": RecordMapping(
            EventKind.WORKSPACE_CWD_CHANGED,
            "v4 cd record; the native event requires an absolute cwd.",
        ),
        "session/title": RecordMapping(
            EventKind.SESSION_TITLE,
            "v4 title record; the {kind: user|llm} source object is flattened.",
            _normalise_title,
        ),
        "todo/write": RecordMapping(
            EventKind.TODO_WRITTEN,
            "v4 todo snapshot ({todos: [{content, status}]}).",
        ),
    }
)
"""Only these v4 record types cross the bridge; everything else is rejected."""


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
        mapping = RECORD_KIND_MAP.get(record_type)
        if mapping is None:
            raise UnsupportedRecordTypeError(record_type)
        payload = dict(data or {})
        if mapping.normalise is not None:
            payload = mapping.normalise(payload)
        return self._session.append(mapping.kind, payload).seq

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
                    source=_notice_source(notice.source),
                )
            )
        return messages

    # ----------------------------------------------------------------- reads

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
        view = fold(self._session.header, self._session.events)
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

    async def send(self, text: str) -> RunResult:
        """Run one fresh turn on the owned session and stream it through hooks.

        Any notices queued by inject() become user messages in the log before
        the run starts, so the new run folds them into its context. Raises
        asyncio.CancelledError when the run was interrupted.
        """
        if self._closed or self._session.closed:
            raise ValueError("session is closed")
        if not isinstance(text, str) or not text:
            raise ValueError("send requires a non-empty prompt")
        if self._task is not None and not self._task.done():
            raise RuntimeError("agent host already has a send in flight")
        for notice in self._handle._drain_notices():
            self._session.append(EventKind.USER_MESSAGE, {"message": notice.to_dict()})
        task = asyncio.create_task(self._loop.run(text))
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
