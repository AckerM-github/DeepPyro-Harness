"""Native agent loop driven by the M1 session and message contracts (M2 task 1).

One :class:`AgentLoop` executes runs over a schema 1
:class:`deeppyro.session.schema.NativeSession`. The loop speaks native
:class:`deeppyro.core.messages.Message` objects end to end, persists native
session events, calls tools through the existing tool protocol and records
budgets, checkpoints and traces. The single v4 seam is
:mod:`deeppyro.agent.native_provider`, which adapts native messages to the
existing provider protocol; nothing in this module imports `core.wire`.

Design notes:

* The session log is the source of truth. The loop appends native events in the
  order `run.started`, `turn.started`, `user.message`, then per step
  `step.started`, `assistant.message`, `tool.call`, `tool.result`,
  `step.ended`, ending with `turn.ended` and `run.finished`.
* A checkpoint file is an optional sidecar; the loop writes the matching
  `run.checkpoint` event unconditionally so a resume can reconstruct the
  checkpoint from the log even when the sidecar is missing.
* Consumption is single-run: calling :meth:`AgentLoop.run` again after a
  terminal run starts a fresh run id on the same session.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

from deeppyro.agent.budget import (
    BudgetExceeded,
    BudgetLimits,
    BudgetTracker,
    BudgetUsage,
    idempotency_key,
)
from deeppyro.agent.checkpoint import CheckpointRecord, CheckpointStore
from deeppyro.agent.context_plan import ContextPlan, build_context_plan
from deeppyro.agent.errors import AgentLoopError, ProviderStreamError, RunCancelledError
from deeppyro.agent.events import (
    AgentHooks,
    AssistantMessageEvent,
    BudgetExceededEvent,
    CheckpointWrittenEvent,
    NullAgentHooks,
    ReasoningDeltaEvent,
    RunFinishedEvent,
    RunStarted,
    StepStarted,
    TextDeltaEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    TurnStarted,
)
from deeppyro.agent.native_provider import (
    ModelResponse,
    NativeProviderAdapter,
    NativeToolCall,
    build_request,
)
from deeppyro.agent.run_state import RunState, RunStatus, ToolCallCheckpoint
from deeppyro.api.services import ServiceBag
from deeppyro.core.ids import (
    new_message_id,
    new_run_id,
    new_span_id,
    new_step_id,
    new_trace_id,
    new_turn_id,
    now_ms,
)
from deeppyro.core.messages import (
    ContentPart,
    Message,
    ModelSource,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.core.protocols import (
    LlmProvider,
    LlmRequest,
    Tool,
    ToolContext,
    ToolResult,
    Usage,
)
from deeppyro.observability.protocols import TraceExportSink, TurnUsage, UsageSink
from deeppyro.observability.trace import (
    ModelCallRecord,
    RunRecord,
    ToolCallRecord,
    TraceFormatError,
    TraceStore,
    args_hash,
    hash_payload,
    prompt_hash,
)
from deeppyro.session.fold import fold
from deeppyro.session.schema import Event, EventKind, NativeSession
from deeppyro.tools.pipeline import ToolCallRequest, ToolOutcome, run_tool_call
from deeppyro.tools.registry import ToolRegistry

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RunResult:
    """Terminal result of one successful native run."""

    run_id: str
    trace_id: str
    session_id: str
    status: RunStatus
    final_text: str
    finish_reason: str | None
    steps: int
    tool_calls: int
    usage: BudgetUsage
    last_message_id: str | None
    checkpoint_path: Path | None


class AgentLoop:
    """Drive one native conversation over a schema 1 session.

    Args:
        session: The append-only native session this loop owns (single writer).
        provider: Existing v4 provider; bridged through the native adapter.
        model: Provider model id recorded on messages and trace records.
        tools: Tools offered to the model; wrapped in a ToolRegistry.
        cwd: Working directory passed to tool contexts.
        hooks: Optional typed lifecycle observer; defaults to a no-op observer.
        budget: Run limits; an empty BudgetLimits means unlimited.
        checkpoint_store: Optional atomic checkpoint sidecar store.
        trace_store: Optional per-run trace store (M1 trace contract).
        temperature: Sampling temperature forwarded to each model call.
        max_tokens_per_call: Per-call output cap forwarded to the provider.
        provider_name: Provider id for messages/trace; defaults to provider.name.
        cost_per_million_tokens: Optional flat price used to estimate run cost.
        permission: Opaque permission object forwarded into tool contexts.
        services: Plugin service mapping forwarded into tool contexts.
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
    ) -> None:
        self._session = session
        self._provider = NativeProviderAdapter(provider)
        self._model = model
        self._registry = ToolRegistry(tuple(tools))
        self._cwd = Path(cwd)
        self._hooks: AgentHooks = hooks if hooks is not None else NullAgentHooks()
        self._limits = budget if budget is not None else BudgetLimits()
        self._checkpoint_store = checkpoint_store
        self._trace_store = trace_store
        self._temperature = temperature
        self._max_tokens_per_call = max_tokens_per_call
        self._provider_name = provider_name if provider_name is not None else provider.name
        self._cost_per_million_tokens = cost_per_million_tokens
        self._permission = permission
        self._services: dict[str, Any] = dict(services or {})
        self._usage_sink = ServiceBag(self._services).get(UsageSink)
        self._trace_export = ServiceBag(self._services).get(TraceExportSink)
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_cached_tokens = 0
        self._turn_latency_ms = 0
        self._turn_cost = 0.0
        self._cancel = asyncio.Event()
        self._running = False
        self._run_id: str | None = None
        self._trace_id: str | None = None
        self._turn_id: str | None = None
        self._step_id: str | None = None
        self._turn_index = 0
        self._step_index = 0
        self._started = 0
        self._usage = BudgetUsage()
        self._tracker: BudgetTracker | None = None
        self._turn_open = False
        self._step_open = False
        self._last_message_id: str | None = None
        self._last_assistant_text = ""
        self._last_finish_reason: str | None = None
        self._last_checkpoint_path: Path | None = None

    @property
    def session(self) -> NativeSession:
        """The native session this loop appends to."""
        return self._session

    @property
    def run_id(self) -> str | None:
        """Run id of the current or most recent run."""
        return self._run_id

    @property
    def trace_id(self) -> str | None:
        """Trace id of the current or most recent run."""
        return self._trace_id

    def cancel(self) -> None:
        """Request cooperative cancellation of the active run."""
        self._cancel.set()

    # ------------------------------------------------------------ internals

    def _require_tracker(self) -> BudgetTracker:
        if self._tracker is None:
            raise AgentLoopError("run context is not initialized")
        return self._tracker

    def _require_ids(self) -> tuple[str, str, str, str]:
        run_id = self._run_id
        trace_id = self._trace_id
        turn_id = self._turn_id
        step_id = self._step_id
        if run_id is None or trace_id is None or turn_id is None or step_id is None:
            raise AgentLoopError("run context is not initialized")
        return run_id, trace_id, turn_id, step_id

    def _next_ts(self) -> int:
        return max(now_ms(), self._session.last_ts)

    def _turn_number(self) -> int:
        started = sum(1 for event in self._session.events if event.type == EventKind.TURN_STARTED.value)
        return 1 + started

    # ------------------------------------------------------------------ run

    async def run(
        self,
        prompt: str | None = None,
        *,
        resume: bool = False,
        cancel: asyncio.Event | None = None,
    ) -> RunResult:
        """Execute one run and return its terminal result.

        Args:
            prompt: The user prompt for a fresh run; None on a resume.
            resume: Continue a session whose previous run did not finish. The
                loop repairs any dangling step/turn (schema 10.2) and starts a
                new run without appending a new user message.
            cancel: Optional external cancellation event; defaults to a fresh
                event that :meth:`cancel` sets.

        Raises:
            ValueError: The prompt/resume combination is invalid.
            RuntimeError: Another run is already in flight on this loop.
            BudgetExceeded: A run budget was crossed; the abort is persisted.
            ProviderStreamError: The provider stream failed; a failed run is
                persisted before the error is re-raised.
            RunCancelledError: Cancellation was requested; the abort is
                persisted before the error is re-raised.
            asyncio.CancelledError: The surrounding task was cancelled; the
                abort is persisted before cancellation propagates.
        """
        if self._running:
            raise RuntimeError("agent loop already has a run in flight")
        if self._session.closed:
            raise ValueError("session is closed")
        if not resume and (prompt is None or not prompt.strip()):
            raise ValueError("a fresh run requires a non-empty prompt")
        if resume and prompt is not None:
            raise ValueError("a resumed run does not accept a new prompt")
        self._running = True
        self._cancel = cancel if cancel is not None else asyncio.Event()
        try:
            self._begin(resume)
            try:
                final_text, finish_reason = await self._drive(prompt, resume)
            except BudgetExceeded as exc:
                reason = exc.turn_reason()
                self._emit_budget_exceeded(exc)
                self._finalize_abort(RunStatus.FAILED, reason)
                raise
            except ProviderStreamError as exc:
                reason = {"kind": "error", "message": str(exc), "code": "provider_error"}
                self._finalize_abort(RunStatus.FAILED, reason)
                raise
            except RunCancelledError:
                reason = {"kind": "aborted", "by": "user"}
                self._finalize_abort(RunStatus.CANCELLED, reason)
                raise
            except asyncio.CancelledError:
                reason = {"kind": "aborted", "by": "user"}
                self._finalize_abort(RunStatus.CANCELLED, reason)
                raise
            self._finalize_success()
            run_id, trace_id = self._require_ids()[:2]
            return RunResult(
                run_id=run_id,
                trace_id=trace_id,
                session_id=self._session.header.id,
                status=RunStatus.COMPLETED,
                final_text=final_text,
                finish_reason=finish_reason,
                steps=self._require_tracker().steps,
                tool_calls=self._usage.tool_calls,
                usage=BudgetUsage.from_dict(self._usage.to_dict()),
                last_message_id=self._last_message_id,
                checkpoint_path=self._last_checkpoint_path,
            )
        finally:
            self._running = False

    def _begin(self, resume: bool) -> None:
        session = self._session
        self._usage = BudgetUsage()
        if resume:
            self._restore_usage_from_checkpoint()
            self._repair_unfinished_turn()
        self._run_id = new_run_id()
        self._trace_id = new_trace_id()
        self._started = now_ms()
        self._tracker = BudgetTracker(self._limits, usage=self._usage)
        self._turn_id = None
        self._step_id = None
        self._turn_open = False
        self._step_open = False
        self._last_checkpoint_path = None
        self._last_assistant_text = ""
        self._last_finish_reason = None
        self._turn_input_tokens = 0
        self._turn_output_tokens = 0
        self._turn_cached_tokens = 0
        self._turn_latency_ms = 0
        self._turn_cost = 0.0
        view = fold(session.header, session.events)
        self._last_message_id = view.last_message_id
        run_record = RunRecord(
            run_id=self._run_id,
            trace_id=self._trace_id,
            session_id=session.header.id,
            started_at=self._started,
            provider=self._provider_name,
            model=self._model,
        )
        if self._trace_store is not None:
            self._trace_store.create_run(run_record)
        if self._trace_export is not None:
            with contextlib.suppress(Exception):
                self._trace_export.export_trace_record(run_record)
        self._append_run_event(EventKind.RUN_STARTED, {})
        self._emit_run_started(resume=resume)

    def _restore_usage_from_checkpoint(self) -> None:
        store = self._checkpoint_store
        if store is None:
            return
        record = store.read(self._session.header.id)
        if record is None:
            return
        if record.state.status is RunStatus.RUNNING:
            self._usage = BudgetUsage.from_dict(record.state.usage.to_dict())

    def _repair_unfinished_turn(self) -> None:
        session = self._session
        open_turn: str | None = None
        open_step: str | None = None
        run_id: str | None = None
        trace_id: str | None = None
        for event in session.events:
            if event.type == EventKind.TURN_STARTED.value:
                open_turn = event.turn_id
                open_step = None
                run_id = event.run_id
                trace_id = event.trace_id
            elif event.type == EventKind.TURN_ENDED.value:
                open_turn = None
                open_step = None
            elif event.type == EventKind.STEP_STARTED.value:
                open_step = event.step_id
                run_id = event.run_id or run_id
                trace_id = event.trace_id or trace_id
            elif event.type == EventKind.STEP_ENDED.value:
                open_step = None
        if open_turn is None:
            return
        if open_step is not None:
            session.append(
                EventKind.STEP_ENDED,
                {},
                run_id=run_id,
                turn_id=open_turn,
                step_id=open_step,
                trace_id=trace_id,
            )
        session.append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "aborted", "by": "disposed"}},
            run_id=run_id,
            turn_id=open_turn,
            trace_id=trace_id,
        )

    # ------------------------------------------------------------- main loop

    async def _drive(self, prompt: str | None, resume: bool) -> tuple[str, str]:
        if self._cancel.is_set():
            raise RunCancelledError("run cancelled before the turn started")
        self._turn_index = self._turn_number()
        self._turn_id = new_turn_id()
        self._append_turn_event(EventKind.TURN_STARTED, {})
        self._turn_open = True
        if resume:
            self._emit_turn_started(prompt_message_id=None)
        else:
            if prompt is None:
                raise ValueError("a fresh run requires a prompt")
            user_message, user_ts = self._user_message(prompt)
            self._append_turn_event(EventKind.USER_MESSAGE, {"message": user_message.to_dict()}, ts=user_ts)
            self._last_message_id = user_message.id
            self._emit_turn_started(prompt_message_id=user_message.id)
        while True:
            if self._cancel.is_set():
                raise RunCancelledError("run cancelled between steps")
            tracker = self._require_tracker()
            tracker.start_step()
            self._step_index = tracker.steps
            self._step_id = new_step_id()
            self._append_step_event(EventKind.STEP_STARTED, {})
            self._step_open = True
            self._emit_step_started()
            self._write_checkpoint(RunStatus.RUNNING)
            plan = self._build_plan()
            request = build_request(
                plan,
                provider=self._provider_name,
                model=self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens_per_call,
                session_id=self._session.header.id,
            )
            payload = self._prompt_payload(request)
            response = await self._model_call(request)
            if response.finish_reason == "aborted":
                raise RunCancelledError("provider aborted the model call")
            cost = self._cost_for(response.usage)
            self._record_model_trace(plan, payload, response, cost)
            tracker.record_model(response.usage, latency_ms=response.latency_ms, cost_usd=cost)
            usage = response.usage
            self._turn_input_tokens += usage.input_tokens
            self._turn_output_tokens += usage.output_tokens
            self._turn_cached_tokens += usage.cached_input_tokens or 0
            self._turn_latency_ms += response.latency_ms
            self._turn_cost += cost
            assistant_message, assistant_ts = self._assistant_message(response)
            self._append_step_event(
                EventKind.ASSISTANT_MESSAGE,
                {"message": assistant_message.to_dict(), "finish_reason": response.finish_reason},
                ts=assistant_ts,
            )
            self._last_message_id = assistant_message.id
            self._last_assistant_text = response.text
            self._last_finish_reason = response.finish_reason
            self._emit_assistant_message(assistant_message, response.finish_reason)
            if response.tool_calls:
                await self._dispatch_tool_calls(response.tool_calls)
                self._append_step_event(EventKind.STEP_ENDED, {})
                self._step_open = False
                continue
            self._append_step_event(EventKind.STEP_ENDED, {})
            self._step_open = False
            return self._last_assistant_text, response.finish_reason

    def _build_plan(self) -> ContextPlan:
        view = fold(self._session.header, self._session.events)
        return build_context_plan(view.messages, system=view.system, tools=self._registry)

    # --------------------------------------------------------- model calling

    async def _model_call(self, request: LlmRequest) -> ModelResponse:
        run_id, trace_id, turn_id, step_id = self._require_ids()
        hooks = self._hooks

        def on_text(fragment: str) -> None:
            with contextlib.suppress(Exception):
                hooks.text_delta(
                    TextDeltaEvent(
                        run_id=run_id,
                        trace_id=trace_id,
                        turn_id=turn_id,
                        step_id=step_id,
                        text=fragment,
                    )
                )

        def on_reasoning(fragment: str) -> None:
            with contextlib.suppress(Exception):
                hooks.reasoning_delta(
                    ReasoningDeltaEvent(
                        run_id=run_id,
                        trace_id=trace_id,
                        turn_id=turn_id,
                        step_id=step_id,
                        text=fragment,
                    )
                )

        return await self._await_cancellable(
            self._provider.complete(
                request,
                cancel=self._cancel,
                on_text=on_text,
                on_reasoning=on_reasoning,
            )
        )

    async def _await_cancellable(self, awaitable: Awaitable[T]) -> T:
        if self._cancel.is_set():
            raise RunCancelledError("run cancelled")
        task: asyncio.Future[T] = asyncio.ensure_future(awaitable)
        monitor = asyncio.ensure_future(self._cancel.wait())
        try:
            try:
                finished, _ = await asyncio.wait({task, monitor}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                raise
            if task in finished:
                return cast("T", task.result())
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise RunCancelledError("run cancelled")
        finally:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await monitor

    def _prompt_payload(self, request: LlmRequest) -> dict[str, object]:
        return {
            "provider": request.provider,
            "model": request.model,
            "system": request.system,
            "messages": [message.model_dump(mode="json") for message in request.messages],
            "tools": [schema.model_dump(mode="json") for schema in request.tools],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }

    def _cost_for(self, usage: Usage) -> float:
        if self._cost_per_million_tokens is None:
            return 0.0
        tokens = usage.total_tokens or (usage.input_tokens + usage.output_tokens)
        return float(tokens) / 1_000_000.0 * self._cost_per_million_tokens

    def _report_usage(self, stop_reason: str) -> None:
        """Report the finished turn to the injected usage sink, if any."""
        sink = self._usage_sink
        if sink is None or self._turn_id is None:
            return
        usage = TurnUsage(
            provider=self._provider_name,
            model=self._model,
            session_id=self._session.header.id,
            turn=self._turn_index,
            input_tokens=self._turn_input_tokens,
            output_tokens=self._turn_output_tokens,
            cached_tokens=self._turn_cached_tokens,
            latency_ms=self._turn_latency_ms,
            stop_reason=stop_reason,
            step=self._step_index or None,
            cost=self._turn_cost,
            ts=now_ms(),
            run_id=self._run_id,
            trace_id=self._trace_id,
            source="native",
        )
        with contextlib.suppress(Exception):
            sink.record_turn(usage)

    def _record_model_trace(
        self,
        plan: ContextPlan,
        payload: dict[str, object],
        response: ModelResponse,
        cost: float,
    ) -> None:
        store = self._trace_store
        export = self._trace_export
        if store is None and export is None:
            return
        run_id, trace_id, turn_id, step_id = self._require_ids()
        record = ModelCallRecord(
            trace_id=trace_id,
            span_id=new_span_id(),
            run_id=run_id,
            turn_id=turn_id,
            step_id=step_id,
            provider=self._provider_name,
            model=self._model,
            prompt_hash=prompt_hash(payload),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=response.latency_ms,
            finish_reason=response.finish_reason,
            retry_count=0,
            ts=now_ms(),
            cached_input_tokens=response.usage.cached_input_tokens,
            reasoning_tokens=response.usage.reasoning_tokens,
            cost_usd=cost if cost > 0 else None,
            context_message_ids=plan.source_message_ids,
        )
        if store is not None:
            store.append(record)
        if export is not None:
            with contextlib.suppress(Exception):
                export.export_trace_record(record)

    # ---------------------------------------------------------- tool calling

    async def _dispatch_tool_calls(self, calls: Sequence[NativeToolCall]) -> tuple[ToolOutcome, ...]:
        run_id, trace_id, turn_id, step_id = self._require_ids()
        checkpoints = [
            ToolCallCheckpoint(
                call_id=call.call_id,
                name=call.name,
                state="pending",
                idempotency_key=self._idempotency_key(call),
            )
            for call in calls
        ]
        for call, checkpoint in zip(calls, checkpoints, strict=True):
            self._append_step_event(
                EventKind.TOOL_CALL,
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments},
            )
            with contextlib.suppress(Exception):
                self._hooks.tool_call_started(
                    ToolCallStartedEvent(
                        run_id=run_id,
                        trace_id=trace_id,
                        turn_id=turn_id,
                        step_id=step_id,
                        call_id=call.call_id,
                        name=call.name,
                        arguments=call.arguments,
                        idempotency_key=checkpoint.idempotency_key or "",
                    )
                )
        self._write_checkpoint(RunStatus.RUNNING, tool_calls=checkpoints)
        outcomes: list[ToolOutcome] = []
        for batch in self._batches(calls):
            results = await self._run_batch(batch)
            for call, outcome in zip(batch, results, strict=True):
                if outcome is None:
                    raise RunCancelledError("run cancelled during tool execution")
                self._record_tool_result(call, outcome)
                outcomes.append(outcome)
            self._write_checkpoint(RunStatus.RUNNING, tool_calls=self._checkpoint_calls(calls, outcomes))
            self._require_tracker().check_wall()
            if self._cancel.is_set():
                raise RunCancelledError("run cancelled during tool execution")
        return tuple(outcomes)

    def _batches(self, calls: Sequence[NativeToolCall]) -> list[tuple[NativeToolCall, ...]]:
        batches: list[tuple[NativeToolCall, ...]] = []
        current: list[NativeToolCall] = []
        for call in calls:
            tool = self._registry.get(call.name)
            parallel = tool is not None and bool(getattr(tool, "parallel_safe", True))
            if parallel:
                current.append(call)
            else:
                if current:
                    batches.append(tuple(current))
                    current = []
                batches.append((call,))
        if current:
            batches.append(tuple(current))
        return batches

    async def _run_batch(self, batch: Sequence[NativeToolCall]) -> list[ToolOutcome | None]:
        if len(batch) == 1:
            return [await self._run_single(batch[0])]
        tasks: list[asyncio.Task[ToolOutcome | None]] = []
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._run_single(call)) for call in batch]
        return [task.result() for task in tasks]

    async def _run_single(self, call: NativeToolCall) -> ToolOutcome | None:
        request = ToolCallRequest(call_id=call.call_id, name=call.name, arguments=call.arguments)
        try:
            return await self._await_cancellable(
                run_tool_call(
                    request,
                    registry=self._registry,
                    ctx_factory=self._tool_context,
                    turn=self._turn_index,
                    step=self._step_index,
                )
            )
        except RunCancelledError:
            return None

    def _tool_context(self, request: ToolCallRequest) -> ToolContext:
        return ToolContext(
            cwd=self._cwd,
            session_id=self._session.header.id,
            turn=self._turn_index,
            step=self._step_index,
            call_id=request.call_id,
            cancel=self._cancel,
            log=self._tool_log,
            permission=self._permission,
            services=self._services,
        )

    def _tool_log(self, message: str, data: dict[str, object]) -> None:
        """Accept tool log lines; the native loop has no string event bus."""
        del message, data

    def _record_tool_result(self, call: NativeToolCall, outcome: ToolOutcome) -> None:
        result = outcome.result
        message, ts = self._tool_message(call.call_id, result)
        data: dict[str, object] = {"call_id": call.call_id, "message": message.to_dict()}
        if result.error is not None:
            code = result.error.get("code")
            if isinstance(code, str) and code:
                data["error_code"] = code
        self._append_step_event(EventKind.TOOL_RESULT, data, ts=ts)
        self._last_message_id = message.id
        self._require_tracker().record_tool()
        run_id, trace_id, turn_id, step_id = self._require_ids()
        with contextlib.suppress(Exception):
            self._hooks.tool_call_finished(
                ToolCallFinishedEvent(
                    run_id=run_id,
                    trace_id=trace_id,
                    turn_id=turn_id,
                    step_id=step_id,
                    call_id=call.call_id,
                    name=call.name,
                    result=result,
                    duration_ms=outcome.duration_ms,
                )
            )
        self._record_tool_trace(call, outcome)

    def _record_tool_trace(self, call: NativeToolCall, outcome: ToolOutcome) -> None:
        store = self._trace_store
        export = self._trace_export
        if store is None and export is None:
            return
        run_id, trace_id, turn_id, step_id = self._require_ids()
        record = ToolCallRecord(
            trace_id=trace_id,
            span_id=new_span_id(),
            run_id=run_id,
            turn_id=turn_id,
            step_id=step_id,
            call_id=call.call_id,
            tool=call.name,
            args_hash=self._arguments_hash(call.arguments),
            result_size=len(outcome.result.text.encode("utf-8")),
            is_error=outcome.result.is_error,
            retry_count=0,
            latency_ms=outcome.duration_ms,
            ts=now_ms(),
            idempotency_key=self._idempotency_key(call),
        )
        if store is not None:
            store.append(record)
        if export is not None:
            with contextlib.suppress(Exception):
                export.export_trace_record(record)

    def _checkpoint_calls(
        self, calls: Sequence[NativeToolCall], outcomes: Sequence[ToolOutcome]
    ) -> tuple[ToolCallCheckpoint, ...]:
        states = {
            outcome.call_id: ("failed" if outcome.result.is_error else "completed") for outcome in outcomes
        }
        return tuple(
            ToolCallCheckpoint(
                call_id=call.call_id,
                name=call.name,
                state=states.get(call.call_id, "pending"),
                idempotency_key=self._idempotency_key(call),
            )
            for call in calls
        )

    def _arguments_hash(self, arguments: str) -> str:
        try:
            return args_hash(arguments)
        except TraceFormatError:
            return hash_payload({"arguments": arguments})

    def _idempotency_key(self, call: NativeToolCall) -> str:
        return idempotency_key(self._session.header.id, call.call_id, self._arguments_hash(call.arguments))

    # ------------------------------------------------------------- messages

    def _user_message(self, prompt: str) -> tuple[Message, int]:
        ts = self._next_ts()
        message = Message(
            id=new_message_id(),
            role="user",
            content=(TextPart(text=prompt),),
            created_at=ts,
            source=UserSource(),
        )
        return message, ts

    def _assistant_message(self, response: ModelResponse) -> tuple[Message, int]:
        parts: list[ContentPart] = []
        if response.reasoning:
            parts.append(ReasoningPart(text=response.reasoning))
        if response.text:
            parts.append(TextPart(text=response.text))
        for call in response.tool_calls:
            parts.append(ToolCallPart(id=call.call_id, name=call.name, arguments=call.arguments))
        if not parts:
            raise ProviderStreamError("provider returned an empty assistant message")
        ts = self._next_ts()
        message = Message(
            id=new_message_id(),
            role="assistant",
            content=tuple(parts),
            created_at=ts,
            source=ModelSource(provider=self._provider_name, model=self._model),
        )
        return message, ts

    def _tool_message(self, call_id: str, result: ToolResult) -> tuple[Message, int]:
        ts = self._next_ts()
        text = result.text if result.text else " "
        message = Message(
            id=new_message_id(),
            role="tool",
            content=(TextPart(text=text),),
            created_at=ts,
            source=ToolSource(call_id=call_id),
            tool_call_id=call_id,
            is_error=result.is_error,
        )
        return message, ts

    # --------------------------------------------------------------- events

    def _append_run_event(
        self, kind: EventKind, data: Mapping[str, object], *, ts: int | None = None
    ) -> Event:
        return self._session.append(kind, dict(data), run_id=self._run_id, trace_id=self._trace_id, ts=ts)

    def _append_turn_event(
        self, kind: EventKind, data: Mapping[str, object], *, ts: int | None = None
    ) -> Event:
        return self._session.append(
            kind,
            dict(data),
            run_id=self._run_id,
            turn_id=self._turn_id,
            trace_id=self._trace_id,
            ts=ts,
        )

    def _append_step_event(
        self, kind: EventKind, data: Mapping[str, object], *, ts: int | None = None
    ) -> Event:
        return self._session.append(
            kind,
            dict(data),
            run_id=self._run_id,
            turn_id=self._turn_id,
            step_id=self._step_id,
            trace_id=self._trace_id,
            ts=ts,
        )

    def _emit_run_started(self, *, resume: bool) -> None:
        run_id = self._run_id
        trace_id = self._trace_id
        if run_id is None or trace_id is None:
            return
        with contextlib.suppress(Exception):
            self._hooks.run_started(
                RunStarted(
                    run_id=run_id,
                    trace_id=trace_id,
                    session_id=self._session.header.id,
                    resumed=resume,
                )
            )

    def _emit_turn_started(self, *, prompt_message_id: str | None) -> None:
        run_id = self._run_id
        trace_id = self._trace_id
        turn_id = self._turn_id
        if run_id is None or trace_id is None or turn_id is None:
            return
        with contextlib.suppress(Exception):
            self._hooks.turn_started(
                TurnStarted(
                    run_id=run_id,
                    trace_id=trace_id,
                    turn_id=turn_id,
                    prompt_message_id=prompt_message_id,
                )
            )

    def _emit_step_started(self) -> None:
        run_id, trace_id, turn_id, step_id = self._require_ids()
        with contextlib.suppress(Exception):
            self._hooks.step_started(
                StepStarted(
                    run_id=run_id,
                    trace_id=trace_id,
                    turn_id=turn_id,
                    step_id=step_id,
                    step=self._step_index,
                )
            )

    def _emit_assistant_message(self, message: Message, finish_reason: str) -> None:
        run_id, trace_id, turn_id, step_id = self._require_ids()
        with contextlib.suppress(Exception):
            self._hooks.assistant_message(
                AssistantMessageEvent(
                    run_id=run_id,
                    trace_id=trace_id,
                    turn_id=turn_id,
                    step_id=step_id,
                    message=message,
                    finish_reason=finish_reason,
                )
            )

    def _emit_budget_exceeded(self, exc: BudgetExceeded) -> None:
        run_id = self._run_id
        trace_id = self._trace_id
        if run_id is None or trace_id is None:
            return
        with contextlib.suppress(Exception):
            self._hooks.budget_exceeded(
                BudgetExceededEvent(
                    run_id=run_id,
                    trace_id=trace_id,
                    kind=exc.kind,
                    limit=exc.limit,
                    actual=exc.actual,
                )
            )

    def _emit_run_finished(self, status: RunStatus, reason: Mapping[str, object] | None) -> None:
        run_id = self._run_id
        trace_id = self._trace_id
        if run_id is None or trace_id is None:
            return
        with contextlib.suppress(Exception):
            self._hooks.run_finished(
                RunFinishedEvent(
                    run_id=run_id,
                    trace_id=trace_id,
                    session_id=self._session.header.id,
                    status=status.value,
                    reason=reason,
                )
            )

    # ---------------------------------------------------------- checkpoints

    def _write_checkpoint(
        self,
        status: RunStatus,
        *,
        tool_calls: Sequence[ToolCallCheckpoint] = (),
        approvals: Sequence[Mapping[str, object]] = (),
    ) -> Path | None:
        run_id = self._run_id
        trace_id = self._trace_id
        if run_id is None or trace_id is None:
            return None
        tracker = self._require_tracker()
        tracker.check()
        state = RunState(
            run_id=run_id,
            trace_id=trace_id,
            session_id=self._session.header.id,
            status=status,
            last_seq=self._session.last_seq + 1,
            usage=self._usage,
            budget=self._limits,
            started_at=self._started,
            turn_id=self._turn_id if self._turn_open else None,
            step_id=self._step_id if self._step_open else None,
            last_message_id=self._last_message_id,
        )
        store = self._checkpoint_store
        record: CheckpointRecord | None = None
        data: dict[str, object]
        if store is not None:
            record = store.make_record(state, tool_calls=tool_calls, approvals=approvals)
            data = dict(record.data)
        else:
            data = state.checkpoint_data(tool_calls=tool_calls, approvals=approvals)
        event = self._append_run_event(EventKind.RUN_CHECKPOINT, data)
        if event.seq != state.last_seq:
            raise AgentLoopError("checkpoint prediction does not match the appended event")
        path: Path | None = None
        if store is not None and record is not None:
            path = store.write(record)
            self._last_checkpoint_path = path
            self._emit_checkpoint_written(path, event.seq, status)
        return path

    def _emit_checkpoint_written(self, path: Path, last_seq: int, status: RunStatus) -> None:
        run_id = self._run_id
        trace_id = self._trace_id
        if run_id is None or trace_id is None:
            return
        with contextlib.suppress(Exception):
            self._hooks.checkpoint_written(
                CheckpointWrittenEvent(
                    run_id=run_id,
                    trace_id=trace_id,
                    session_id=self._session.header.id,
                    path=str(path),
                    last_seq=last_seq,
                    status=status.value,
                )
            )

    # ------------------------------------------------------------- finalize

    def _finalize_success(self) -> None:
        with contextlib.suppress(Exception):
            self._report_usage(self._last_finish_reason or "stop")
        self._close_open_step()
        if self._turn_open:
            self._append_turn_event(EventKind.TURN_ENDED, {"reason": {"kind": "completed"}})
            self._turn_open = False
        self._write_checkpoint(RunStatus.COMPLETED)
        self._append_run_event(
            EventKind.RUN_FINISHED, {"status": "completed", "reason": {"kind": "completed"}}
        )
        self._emit_run_finished(RunStatus.COMPLETED, {"kind": "completed"})

    def _finalize_abort(self, status: RunStatus, reason: Mapping[str, object]) -> None:
        stop_reason = reason.get("kind")
        with contextlib.suppress(Exception):
            self._report_usage(str(stop_reason) if stop_reason is not None else status.value)
        with contextlib.suppress(Exception):
            self._close_open_step()
        if self._turn_open:
            with contextlib.suppress(Exception):
                self._append_turn_event(EventKind.TURN_ENDED, {"reason": dict(reason)})
            self._turn_open = False
        with contextlib.suppress(Exception):
            self._write_checkpoint(status)
        with contextlib.suppress(Exception):
            self._append_run_event(EventKind.RUN_FINISHED, {"status": status.value, "reason": dict(reason)})
        self._emit_run_finished(status, reason)

    def _close_open_step(self) -> None:
        if not self._step_open:
            return
        self._append_step_event(EventKind.STEP_ENDED, {})
        self._step_open = False


__all__ = ["AgentLoop", "RunResult"]
