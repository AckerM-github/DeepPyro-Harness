# Native agent loop

> **状态**：现行（0.3.0 native-only） · **层级**：AgentLoop 规格 · **唯一实现**：`src/deeppyro/agent/loop.py`
> **关联契约**：[native/schema.md](../native/schema.md)（schema 1 会话、事件顺序、run 事件与 trace）、[hooks.md](hooks.md)（typed `AgentHooks` 与领域总线）、[events.md](events.md)（总线目录）
> **最后核对**：2026-11-19

This document is the contract for the one native agent loop. It replaces the
deleted v4 loop specification: there is no `AgentSession`, no projection
layer, no hook journal, and no second loop implementation in 0.3.0. The session
log is the source of truth; the loop only appends schema 1 events through
`NativeSession.append` and never writes messages or state through another path.

## 1. Position

`AgentLoop` (`src/deeppyro/agent/loop.py:127`) executes **one run at a time**
against a `NativeSession` (`src/deeppyro/session/schema.py`). Everything the
model sees and everything the model produces is a native `Message` from
`src/deeppyro/core/messages.py`. The loop owns:

* run/turn/step lifecycle and the corresponding session events;
* context assembly (fold + `ContextPlan`) for each model step;
* one provider call per step through `core.llm.LlmProvider`;
* tool dispatch through the existing tool pipeline (`src/deeppyro/tools/pipeline.py`);
* run budgets, checkpoints, traces and usage reporting;
* typed `AgentHooks` notifications for observers (TUI, headless sink, tests).

The loop does **not** own session creation, session locking, compaction,
approval interaction, goal rounds, team/subagent orchestration or the web UI.
Those are host responsibilities (see [headless.md](headless.md),
[tui.md](tui.md), [goal.md](goal.md)).

## 2. Public API

### 2.1 Constructor

```python
AgentLoop(
    *,
    session: NativeSession,
    provider: NativeLlmProvider,
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
)
```

* `hooks` defaults to `NullAgentHooks()`; a broken observer can never abort a run
  (every hook dispatch is wrapped in `contextlib.suppress(Exception)`).
* `provider_name` defaults to `provider.name`; it is recorded in the session
  trace and in usage records.
* `services` is a plain mapping read once at construction for
  `UsageSink`, `TraceExportSink` and `HookService` (`src/deeppyro/agent/loop.py:182-184`).
  The `HookService` is only forwarded to the tool pipeline; the loop itself
  dispatches typed hooks, not string-keyed bus events.
* `budget` defaults to `BudgetLimits()` (all limits `None` = unlimited).
* `checkpoint_store` is optional. When `None`, `run.checkpoint` events are
  still appended to the session (they are authoritative); only the sidecar file
  is skipped.

### 2.2 `run()`

```python
result = await loop.run(
    prompt=None,        # required for a fresh run; None when resuming
    *,
    resume=False,       # continue an unfinished run on the same session
    source=None,        # MessageSource for the user message, default UserSource()
    cancel=None,        # optional asyncio.Event for external cancellation
)
```

Guards (`src/deeppyro/agent/loop.py:253-293`):

| Condition | Result |
|---|---|
| a run is already in flight on this loop | `RuntimeError("agent loop already has a run in flight")` |
| the session is closed | `ValueError("session is closed")` |
| fresh run with a missing/blank prompt | `ValueError("a fresh run requires a non-empty prompt")` |
| resume with a prompt supplied | `ValueError("a resumed run does not accept a new prompt")` |

One `AgentLoop` instance therefore consumes exactly one run; calling `run()`
again after success starts a **new** run (new `run_id`/`trace_id`,
`run.started` appended) on the same session. Hosts that need a second turn
create a new `AgentLoop`.

### 2.3 `RunResult`

`RunResult` (`src/deeppyro/agent/loop.py:110`) is a frozen slots dataclass:

| Field | Meaning |
|---|---|
| `run_id`, `trace_id`, `session_id` | ids of the completed run |
| `status` | `RunStatus.completed` for the success path |
| `final_text` | text of the last assistant message |
| `finish_reason` | provider finish reason of the last model call |
| `steps`, `tool_calls` | counters accumulated by the budget tracker |
| `usage` | `BudgetUsage` snapshot (input/output tokens, cost, tool calls, wall ms) |
| `last_message_id` | id of the last persisted message |
| `checkpoint_path` | last sidecar path when a `CheckpointStore` is configured, else `None` |
| `cached_input_tokens` | cached prompt tokens reported by the provider |

Failure paths raise instead of returning a `RunResult` (see §9).

### 2.4 Other surface

* `cancel()` sets the cooperative cancellation event (`src/deeppyro/agent/loop.py:224`).
* Properties `session`, `run_id`, `trace_id`.
* `repair_open_turn(cause=None) -> bool` may also be called by a host before
  resuming; it closes a dangling turn/step without fabricating content (§10).
* `AgentLoop` and `RunResult` are the module's `__all__`.

## 3. Run / turn / step lifecycle

`_begin` (`src/deeppyro/agent/loop.py:334-373`) starts every run:

1. reset per-run usage and turn counters; on resume restore usage from the
   checkpoint sidecar when its status is `running`, then call `repair_open_turn()`;
2. allocate `run_id` and `trace_id`; record `started_at`;
3. create the `BudgetTracker`;
4. make the `RunRecord` and hand it to the trace store / `TraceExportSink`;
5. append `run.started` (`{}`) and emit `RunStarted(resumed=…)`.

`_drive` (`src/deeppyro/agent/loop.py:437-511`) executes exactly one turn:

1. fail with `RunCancelledError("run cancelled before the turn started")` if
   cancellation was already requested;
2. compute the 1-based turn index from existing `turn.started` events and
   allocate `turn_id`; append `turn.started`, emit `TurnStarted`;
3. fresh run: build the native user message via `native_user_message` with
   `MessageSource`, append `user.message`, then emit `TurnStarted(prompt_message_id=…)`;
   resume: append no user message and emit `TurnStarted(prompt_message_id=None)`;
4. per step, in a `while True` loop:
   * fail with `RunCancelledError("run cancelled between steps")` if cancelled;
   * `tracker.start_step()`; allocate `step_id`; append `step.started`; emit `StepStarted`;
   * write a `run.checkpoint` with status `running`;
   * `_build_plan()` = `fold(header, events)` + `build_context_plan(...)`;
   * build the `ModelRequest` (`provider`, `model`, `messages`, `system`,
     `tools`, per-call `max_tokens`, `temperature`, `session_id`);
   * call `complete_model_call` under `_await_cancellable`;
   * `finish_reason == "aborted"` becomes `RunCancelledError("provider aborted the model call")`;
   * compute cost, append the model trace record, update the budget tracker and
     turn counters (`retries`, `latency_ms`, `cached_input_tokens`, `cost`);
   * build the assistant message from the response, append
     `assistant.message {message, finish_reason}`, emit `AssistantMessage`;
   * if the response has tool calls: `_dispatch_tool_calls(...)`, append
     `step.ended`, continue with the next step;
   * otherwise append `step.ended` and return `(assistant_text, finish_reason)`.

On success `_finalize_success` (`src/deeppyro/agent/loop.py:1063-1074`):
report usage, close an open step, append `turn.ended` with
`{"reason": {"kind": "completed"}}`, write a `completed` checkpoint, append
`run.finished {status: "completed", reason: {"kind": "completed"}}`, emit
`RunFinished`.

Typical log order for one run (see native schema §5.5):

```
run.started
turn.started
user.message
  step.started
  run.checkpoint (running)
  assistant.message
  tool.call xN
  run.checkpoint (running, calls pending)
  tool.result xN
  run.checkpoint (running, calls completed/failed)
  step.ended
  ...
turn.ended
run.checkpoint (completed|failed|cancelled)
run.finished
```

## 4. Session log writes

The loop writes only these event kinds (`git grep EventKind` in
`src/deeppyro/agent/loop.py`): `run.started`, `run.checkpoint`,
`run.finished`, `turn.started`, `turn.ended`, `user.message`,
`step.started`, `step.ended`, `assistant.message`, `tool.call`,
`tool.result`.

* Run events carry `run_id` + `trace_id` and no turn/step envelope; turn and
  step events carry the corresponding ids; fold skips `run.*`.
* Event timestamps are monotonically non-decreasing:
  `_next_ts() = max(now_ms(), session.last_ts)` (`src/deeppyro/agent/loop.py:244-245`).
* Streaming text is not persisted: exactly one `assistant.message` is written
  per step, after the provider stream ends and with the provider finish reason.
* `tool.call` is appended for **all** provider tool calls before any tool
  executes; each `tool.result` is appended after its tool returns.
* The loop never appends `model.selected`, `preset.selected`,
  `message.replaced`, `compaction.done` or approval events; hosts and services
  own those.

## 5. Context planning

Each step folds the session once (`src/deeppyro/agent/loop.py:513-515`) and
builds a pure `ContextPlan` (`src/deeppyro/agent/context_plan.py`):

* `messages`: folded conversation messages in session order;
* `system`: folded system message (last `system.message` wins in the fold);
* `tools`: `ToolSpec` schemas in registry order;
* `estimated_tokens`: deterministic estimate (4 chars/token, 256 tokens per
  image, 4 tokens message overhead, 8 tokens tool overhead);
* `source_message_ids`: ids covered by the request, recorded on the model trace
  record as `context_message_ids`.

The loop never compacts on its own. Compaction is a host concern (the TUI
pre-send compactor, a plugin, or an explicit `/compact`); it writes
`message.replaced` / `compaction.done` events that the next fold consumes.

## 6. Model call

`core.llm.LlmProvider` is the only provider interface (`src/deeppyro/core/llm.py`).
`complete_model_call` (`src/deeppyro/agent/model_call.py`) consumes one
`provider.stream(request)` and normalizes it into `ModelResponse`:

* a `Finish` event is mandatory; a stream without one, or one carrying
  `StopReason.ERROR`, is a `ProviderStreamError`;
* tool calls must have a name; argument fragments are joined and default to
  `"{}"`;
* `finish_reason` is forced to `tool_calls` when the response carries calls,
  and a `tool_calls` finish without calls is an error;
* an assistant message with no text, reasoning or tool calls is an error;
* usage is last-wins; latency is measured around the whole call;
* provider-scheduled transport retries (`RetryScheduled`) are counted into
  `ModelResponse.retries` and the model trace record.

The loop wraps the call with `_await_cancellable` so the cancellation event can
win at any time; text and reasoning deltas are forwarded to
`hooks.text_delta` / `hooks.reasoning_delta` as they arrive and are not
persisted. `_model_call` fails with
`RunCancelledError("run cancelled")` when the cancel event wins.

For tracing the request is serialized with native `to_dict()` values
(`_prompt_payload`, `src/deeppyro/agent/loop.py:581-593`) and hashed into
`prompt_hash`. This replaced the v4 `model_dump()` serialization in KI-010:
hash **values** changed once, the `sha256:<64 hex>` format did not.

## 7. Tool dispatch

`_dispatch_tool_calls` (`src/deeppyro/agent/loop.py:667-709`):

1. compute a `ToolCallCheckpoint` (`state="pending"`, idempotency key) for every
   call;
2. append `tool.call {call_id, name, arguments}` and emit `ToolCallStarted`
   for every call **before** executing anything;
3. write a `run.checkpoint` with the pending calls;
4. split calls into batches with `_batches`: consecutive calls whose tool
   declares `parallel_safe` (default `True`) form one parallel batch; any other
   call is its own batch;
5. run each batch through the tool pipeline
   (`run_tool_call`, `src/deeppyro/tools/pipeline.py:464`). A batch with one
   call awaits it directly; larger batches use `asyncio.TaskGroup`. The tool
   context carries `cwd`, `session_id`, `turn`, `step`, `call_id`, the cancel
   event, the tool log callback, `permission` and `services`;
   the `HookService` is passed through so the pipeline dispatches
   `tools/pre-execute`, `tools/execute`, `tools/post-execute` and
   `approval/request`;
6. after each batch: append one `tool.result {call_id, message, error_code?}`
   per outcome (native tool message, `is_error` derived from `ToolResult`),
   emit `ToolCallFinished`, append the tool trace record, write a
   `run.checkpoint` with `completed`/`failed` per call, check the wall budget,
   and fail with `RunCancelledError("run cancelled during tool execution")` if
   cancellation arrived;
7. return the outcomes. Cancellation inside `_await_cancellable` turns into
   `RunCancelledError` (or `None` for the single-call path).

Tool log records go through `append_mapped` (`src/deeppyro/agent/loop.py:765-775`):
records whose type maps to a native event are appended; unsupported record types
are ignored on purpose, so a browser-only record (for example a presented
deliverable) can never fail the tool that produced it.

## 8. Budgets

`BudgetLimits` (`src/deeppyro/agent/budget.py:50`) has four optional cumulative
limits: `max_steps`, `max_tokens`, `max_wall_seconds`, `max_cost`.
`BudgetTracker` raises `BudgetExceeded` as soon as a limit is crossed:

| Kind | Turn reason on abort |
|---|---|
| `max_steps` | `{"kind": "max_steps", "limit": N}` |
| `max_tokens` | `{"kind": "max_tokens", "limit": N}` |
| `max_wall_seconds`, `max_cost` | `{"kind": "error", "message": ..., "code": "budget_exceeded"}` |

The loop catches `BudgetExceeded`, emits `BudgetExceeded`, persists a
`failed` run checkpoint + `run.finished {status: "failed", reason: ...}` and
re-raises. Budget usage is also the `budgets` object of every
`run.checkpoint` (`input_tokens`, `output_tokens`, `cost_usd`, `tool_calls`,
`wall_ms`).

## 9. Cancellation and failures

`run()` persists the terminal run state **before** re-raising
(`src/deeppyro/agent/loop.py:294-332`):

| Raised | Persisted run status | Turn reason |
|---|---|---|
| `BudgetExceeded` | `failed` | budget-kind mapping above |
| `ProviderStreamError` | `failed` | `{"kind": "error", "message": ..., "code": "provider_error"}` |
| `RunCancelledError` | `cancelled` | `{"kind": "aborted", "by": "user"}` |
| `asyncio.CancelledError` | `cancelled` | `{"kind": "aborted", "by": "user"}` |

Abort finalization (`_finalize_abort`) is defensive: closing the open step, the
checkpoint write and the trace write are individually suppressed so the original
exception always propagates. A provider finish reason of `"aborted"` is treated
as cancellation, not as a normal stop.

Guard errors (`ValueError`, `RuntimeError`) are raised before any event is
appended, so they leave no trace in the log.

## 10. Checkpoints and resume

The session log is authoritative; a checkpoint is a cache/scaffold for resume.

* Every step start, every tool dispatch and every finalize appends
  `run.checkpoint` with the status `running`/`completed`/`failed`/`cancelled`
  and the closed payload of native schema §5.5. The event is always appended;
  the sidecar is optional.
* With a `CheckpointStore`, the loop writes `~/.dpro/checkpoints/<session_id>.json`
  (override `DEEPPYRO_CHECKPOINTS_DIR`) atomically; the record wraps the same
  state and must match the appended payload. `_write_checkpoint` asserts that
  the predicted `last_seq` equals the appended event's `seq` and raises
  `AgentLoopError("checkpoint prediction does not match the appended event")`
  on drift (`src/deeppyro/agent/loop.py:1034-1036`).
* Tool call entries record `state` `pending`/`running`/`completed`/`failed`
  plus the idempotency key `idem:sha256(session_id\ncall_id\nargs_hash)`
  (native schema §10.3). A resuming host can use those keys to avoid replaying
  side-effectful calls.
* `run(resume=True)` requires the session lock and an unfinished run. Before
  the new run starts, `repair_open_turn()` closes any dangling turn/step with
  `{"kind": "aborted", "by": "disposed"}`; no synthetic assistant message is
  invented (native schema §7.4 also derives a synthetic tool result for missing
  tool results at fold time).
* Restoring usage happens only when the sidecar status is `running`; a
  completed/failed/cancelled checkpoint does not restore counters.
* A mismatch between checkpoint and session raises `SessionMismatchError`.

## 11. Typed hooks

`AgentHooks` (`src/deeppyro/agent/events.py`) is a Protocol with eleven
methods. `NullAgentHooks` implements all of them as no-ops and is the default.
The loop dispatches, in order:

| Method | Event | When |
|---|---|---|
| `run_started` | `RunStarted(run_id, trace_id, session_id, resumed)` | `_begin` after `run.started` |
| `turn_started` | `TurnStarted(…, turn_id, prompt_message_id)` | after `turn.started` (and `user.message` on a fresh run) |
| `step_started` | `StepStarted(…, step_id, step)` | after `step.started` |
| `text_delta` | `TextDeltaEvent(…, text)` | per streamed text fragment |
| `reasoning_delta` | `ReasoningDeltaEvent(…, text)` | per streamed reasoning fragment |
| `assistant_message` | `AssistantMessageEvent(…, message, finish_reason)` | after `assistant.message` |
| `tool_call_started` | `ToolCallStartedEvent(…, call_id, name, arguments, idempotency_key)` | after each `tool.call`, before execution |
| `tool_call_finished` | `ToolCallFinishedEvent(…, call_id, name, result, duration_ms)` | after each `tool.result` |
| `checkpoint_written` | `CheckpointWrittenEvent(…, path, last_seq, status)` | only when a sidecar file was written |
| `budget_exceeded` | `BudgetExceededEvent(…, kind, limit, actual)` | immediately before the failed-run abort |
| `run_finished` | `RunFinishedEvent(…, session_id, status, reason)` | after `run.finished` |

Event dataclasses are frozen; every dispatch suppresses observer exceptions.
The loop never dispatches string-keyed bus events. It passes the plugin
`HookService` only to the tool pipeline, which owns the domain hooks
(`tools/*`, `approval/request`, `session/*`, etc.). See [hooks.md](hooks.md).

## 12. Traces and usage

* With a `TraceStore` the run creates `~/.dpro/traces/<run_id>.jsonl`
  (native schema §8) and appends `run`, `model_call` and `tool_call` records.
  `context_message_ids` on the model record is `plan.source_message_ids`;
  `retry_count` is the provider retry count; `cost_usd` is set when a
  per-million-token price is configured.
* A `TraceExportSink` in `services` receives the same records; hosts use it
  for the context meter (TUI) and for usage accounting.
* A `UsageSink` in `services` receives one `TurnUsage` per turn:
  provider/model/session/turn/step, input/output/cached tokens, latency,
  retries, stop reason, cost, `run_id`, `trace_id`, `source="native"`.
* Cost is `total_tokens / 1_000_000 * cost_per_million_tokens`, or `0.0`
  when no price is configured.

## 13. Determinism and replay

* Fold and `ContextPlan` are pure functions of the event log; the same log
  yields the same request context.
* Timestamps never go backwards; no wall-clock values leak into message content.
* One `assistant.message` per step, written after the stream; deltas are
  observer-only.
* Failed and cancelled runs leave a complete, replayable log (open step/turn
  closed, terminal checkpoint and `run.finished` present).
* `repair_open_turn` is idempotent: with no open turn it returns `False` and
  appends nothing.

## 14. Known boundaries

* The loop does not compact, summarize or prune messages; a host or plugin must
  append compaction events before the next step if the context is too large.
* Approval UI is not in the loop; the tool pipeline's permission gate and the
  host's `ApprovalService` handle `approval/request`, and their facts are
  persisted by the services, not by the loop.
* Goal rounds are driven externally (`src/deeppyro/goal/native_driver.py`
  calls the loop; `goal.changed` facts drive round counts).
* Team/subagent children are separate sessions and loops; the loop only runs
  the session it was constructed with.
* Checkpoints are a resume aid, not a durability guarantee; the session log is
  the record that must be preserved.
* Only `core.llm.LlmProvider` transports are accepted; the deleted v4 adapters
  and `core.wire` are not a compatibility surface.

## 15. Tests

Primary coverage lives in:

* `tests/native/test_loop.py` — run/turn/step behavior, event order, tools,
  failures;
* `tests/native/test_budget.py` — limits and abort persistence;
* `tests/native/test_checkpoint_resume.py` — checkpoint payloads, resume,
  open-turn repair;
* `tests/native/test_context_plan.py` — deterministic planning and estimates;
* `tests/native/test_headless_native.py` — the loop behind the headless entry;
* `tests/agent/test_model_call.py` — provider stream assembly rules;
* `tests/agent/test_native_host.py`, `tests/agent/test_loop_usage.py` — host
  wiring and usage reporting.

Where this document and the code disagree, the code and tests win.
