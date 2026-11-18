# Hooks and the event bus

> **状态**：现行（0.3.0 native-only） · **层级**：内核契约（typed loop hooks + plugin event bus）
> **关联契约**：[loop.md](loop.md)（typed `AgentHooks` 的调用方）、[events.md](events.md)（内核事件目录与声明的生产者）、[native/schema.md](../native/schema.md)（事件落盘）、[tui.md](tui.md)（/hooks 视图）
> **最后核对**：2026-11-19

DeepPyro 0.3.0 has exactly **two hook surfaces**, and they do not overlap:

1. **Typed loop hooks** — a synchronous Python `Protocol` (`AgentHooks` in `src/deeppyro/agent/events.py`)
   that the one native `AgentLoop` calls with frozen dataclasses. This is how
   in-process observers (the TUI, the headless sink, tests) watch a run. It is
   not a bus: no names, no ordering between independent observers, no ability to
   change the loop's result.
2. **Plugin hooks** — string-named dispatch through `HookService` /
   `EventBus` (`src/deeppyro/hooks/service.py`, `src/deeppyro/core/bus.py`). Plugins use
   this to intercept tool calls, answer approval requests, observe session
   lifecycle and subscribe to domain events. Five dispatch modes give a listener
   a chance to veto, replace or observe a decision.

This document is the contract for both. Code and tests win over prose; the
event names themselves are catalogued in [events.md](events.md).

## 1. Relationship to the deleted v4 stack

The 0.3.0 hard cut removed the v4 loop-boundary hook machinery. There is no
second path and no fallback:

* the v4 `AgentSession` hook dispatch (the old `agent/*` catalogue fired from
  `session_turn.py`/`session_stream.py`) is gone with the deleted session stack;
* the hook journal (`builtin/hook_journal.py`) and `hooks/records.py` are deleted;
  there is no journal file and the TUI `/hooks` view is fed by
  `NativeHookRecorder` on the live hook service instead ([tui.md](tui.md));
* `model/selection`, `tool/call`, `approval/asked` and `approval/decided` are
  retired as dispatch names (`DEPRECATED_NAMES`). Their constants remain
  importable for one minor, but registration is rejected — see §3.6;
* the native loop never raises string-keyed bus events for its own lifecycle.
  It calls `AgentHooks` and appends schema 1 session events instead
  (`src/deeppyro/agent/events.py` docstring; [loop.md](loop.md)).

## 2. Surface 1 — typed loop hooks

### 2.1 The protocol

```python
class AgentHooks(Protocol):
    def run_started(self, event: RunStarted) -> None: ...
    def turn_started(self, event: TurnStarted) -> None: ...
    def step_started(self, event: StepStarted) -> None: ...
    def text_delta(self, event: TextDeltaEvent) -> None: ...
    def reasoning_delta(self, event: ReasoningDeltaEvent) -> None: ...
    def assistant_message(self, event: AssistantMessageEvent) -> None: ...
    def tool_call_started(self, event: ToolCallStartedEvent) -> None: ...
    def tool_call_finished(self, event: ToolCallFinishedEvent) -> None: ...
    def checkpoint_written(self, event: CheckpointWrittenEvent) -> None: ...
    def budget_exceeded(self, event: BudgetExceededEvent) -> None: ...
    def run_finished(self, event: RunFinishedEvent) -> None: ...
```

Rules that follow from the implementation
(`src/deeppyro/agent/loop.py`, emit helpers at lines 908–996 and the stream
callbacks at 519–555):

* **The default is `NullAgentHooks`**; every method is a no-op, so hooks are
  entirely optional. Pass `hooks=...` to `AgentLoop.__init__` to observe.
* **Calls are synchronous and inline.** The loop calls the method directly. The
  observer must not block the event loop; offload slow work to a queue or
  another task (see code/tests).
* **Observer failures are isolated.** Every dispatch is wrapped in
  `contextlib.suppress(Exception)`: an exception from an observer is swallowed
  and can never fail the run, skip a session event or change the result. This
  makes typed hooks observation-only by construction.
* **The loop calls hooks after the corresponding session event is written** (the
  dataclass docstrings state this), so an observer that consults the session log
  sees the event that triggered the callback.
* There is no global ordering contract between separate observers: the loop has
  exactly one `AgentHooks` instance. Compose several observers yourself if
  needed.

### 2.2 Event payloads

All payloads are frozen slotted dataclasses in
`src/deeppyro/agent/events.py`. Every event carries `run_id` and `trace_id`;
turn/step events carry `turn_id` and `step_id`.

| Callback | Payload (besides ids) | Fires |
|---|---|---|
| `run_started` | `session_id`, `resumed: bool` | after `run.started` was appended; `resumed` is true for a resumed run |
| `turn_started` | `prompt_message_id: str | None` | after `turn.started`; `None` when the turn resumes without a new prompt |
| `step_started` | `step: int` | after `step.started` |
| `text_delta` | `text: str` | per streamed assistant text fragment (never persisted) |
| `reasoning_delta` | `text: str` | per streamed reasoning fragment (never persisted) |
| `assistant_message` | `message: Message`, `finish_reason: str` | after `assistant.message` was appended |
| `tool_call_started` | `call_id`, `name`, `arguments` (JSON string), `idempotency_key` | after `tool.call` was appended, **before** dispatch |
| `tool_call_finished` | `call_id`, `name`, `result: ToolResult`, `duration_ms: int` | after the result is available; also fires for error results |
| `checkpoint_written` | `session_id`, `path`, `last_seq: int`, `status: str` | after the checkpoint sidecar was atomically replaced |
| `budget_exceeded` | `kind: BudgetKind`, `limit`, `actual` | immediately before the run aborts on a limit |
| `run_finished` | `session_id`, `status: str` (`completed`/`failed`/`cancelled`), `reason: Mapping | None` | after `run.finished` was appended |

### 2.3 Ordering

The authoritative order is the session log ([native/schema.md](../native/schema.md)
§5.5). As observed through typed hooks:

1. `run_started` once, before any turn.
2. `turn_started` once per turn.
3. For each model step: `step_started`, then zero or more
   `text_delta`/`reasoning_delta` fragments, then `assistant_message`; if
   the assistant message contains tool calls, `tool_call_started` for every
   call, then `tool_call_finished` as results arrive, then the next
   `step_started`.
4. `checkpoint_written` interleaves wherever the loop appends
   `run.checkpoint` (start of a step, before tool dispatch, after tool
   results, and once at the terminal state).
5. `budget_exceeded` (when applicable) is immediately followed by
   `run_finished` with `status="failed"`.
6. `run_finished` is last, exactly once.

Delta counts vary by provider and are not guaranteed: a non-streaming provider
may emit none. `checkpoint_written` count varies with tool use.

### 2.4 Consumers

* **TUI** — `TuiAgentHooks` (`src/deeppyro/tui/native_hooks.py`) maps
  `run_started` → status `thinking`, `step_started` → `step`,
  `text_delta` → `text`, `reasoning_delta` → `thinking`,
  `tool_call_started` → `tool_call`, `tool_call_finished` → `tool_result`;
  `assistant_message`/`checkpoint_written`/`budget_exceeded`/`run_finished`
  produce no TUI event (`run_finished` only clears the active marker). The
  TUI also reads the session log/fold directly for the durable transcript.
* **Headless** — `_NativeSinkHooks` (`src/deeppyro/headless.py`) turns
  `text_delta` into a JSON `{"type":"text"}` line, `reasoning_delta` into
  `{"type":"thinking"}`, and the tool callbacks into `tool_call`/`tool_result`;
  the runner itself emits `session`/`final`/`usage`/`error` frames
  ([headless.md](headless.md)).
* Tests inject recording implementations of `AgentHooks` (for example
  `tests/tui/test_native_hooks.py` and the native loop tests listed in
  [loop.md](loop.md) §15).

## 3. Surface 2 — the plugin hook service

### 3.1 One registry, two doors

`HooksPlugin` (`src/deeppyro/builtin/hooks.py`) allocates one `EventBus` and
one `HookService` over it, and provides both as plugin services:

```python
`HookService` facade   methods: on, has, listeners, waterfall, serial, emit, bail, parallel
`EventBus` transport  raw registration/dispatch, no domain names
```

The facade is the historical hook surface used by in-tree subscribers; the bus
is name-agnostic and can dispatch any catalogue name. Both share the same
registry: a listener registered through either door runs for dispatches through
the other, and observers/scope accounting are global. The kernel hands the same
`EventBus` to plugins (PluginContext `on`) and the loop/tool pipeline receives
the `HookService`. There is exactly one bus per runtime
(`tests/integration/test_single_bus.py`).

### 3.2 Registration

```python
disposer = hooks.on(name, handler, *, prepend=False, once=False, scope=None, global_=False)
```

| Option | Semantics |
|---|---|
| `scope` | Mapping of plugin-scope keys. A listener runs only when its scope is a subset of the dispatch scope (empty scope normalizes to `None`). A scoped listener does **not** run for an unscoped dispatch. |
| `global_=True` | Bypass scope filtering entirely. |
| `prepend=True` | Insert at the head of the listener list (position 0). |
| `once=True` | The registration disposes itself before the wrapped handler runs. |
| disposer | Idempotent; calling twice is safe. |

`has(name)` and `listeners(name)` report the registry; observers can be
added with `add_observer` (§3.5).

### 3.3 Dispatch modes

Five modes, implemented in `src/deeppyro/core/bus.py`:

| Mode | Semantics |
|---|---|
| `waterfall` | Chains listeners; a non-`None` return wins. A listener that returns `None` **without** calling `next()` vetoes the chain (dispatch returns `None`). Calling `next()` twice raises `RuntimeError("next() called more than once for hook {name!r}")`. A listener that awaits `next()` and returns `None` yields the next listener's result. `fallback` (callable or awaitable) resolves at the end of the chain and on cancellation. |
| `serial` | Runs every listener in order, collecting return values; stops early when the payload's `signal` is set. |
| `emit` | Notifies listeners in order. Every listener failure is isolated with `logger.exception("hook handler for %r failed")`; an `emit` cannot fail its dispatcher. |
| `bail` | Runs listeners until one returns a value that is neither `None` nor `False`; returns it, otherwise `None`. |
| `parallel` | `asyncio.gather(..., return_exceptions=True)`; results are returned, collected exceptions raise `HookAggregateError` with message `"N hook listener(s) failed for hook {name!r}"`. |

Cancellation is cooperative: when a dispatch payload carries a `signal`
object whose `is_set()` becomes true, `serial` stops and `waterfall` resolves its
fallback instead of continuing the chain. Tool and approval payloads carry the
run's cancel signal this way.

### 3.4 Payloads

Before dispatch, payload mappings are replaced by `MappingProxyType` views,
lists/tuples by tuples; other objects keep their identity. Subscribers must
treat payloads as read-only. A mode's return value is the dispatcher's decision;
for `emit` the return values are observations only.

The catalogue in `src/deeppyro/core/events.py` declares each name's mode and its
declared producer ([events.md](events.md)). A dispatch uses the mode the caller
chooses; the declared mode is the contract for that name.

### 3.5 Observers

`hooks.add_observer(callback) -> disposer` and `hooks.observed` give a global
view of every dispatch. For each dispatch the bus emits exactly one
`HookObservation(kind="invoked")` before listeners run, and exactly one
`kind="resolved"` from a `finally` block. Fields: `kind`, `hook`, `mode`,
`scope`, `payload`, `result`, `error`, `duration_ms`. An observer's own
failure is logged and cannot change the dispatch result.

In-tree observers: `builtin/hook_observability.py` (curated recent decisions,
used by the TUI `/hooks` view), `tests/hooks/test_observer.py`, and telemetry.

### 3.6 Name policy

`HookService._validate` checks every registration and dispatch:

* Names in `HOOK_NAMES` (`src/deeppyro/hooks/types.py`, 15 names) or any
  name accepted by `is_bus_event()` (the [events.md](events.md) catalogue) are
  allowed.
* `DEPRECATED_NAMES` — `model/selection`, `tool/call`, `approval/asked`,
  `approval/decided` — raise `ValueError`:
  `"hook {name!r} was retired from the bus; subscribe to what replaces it
  (tools/pre-execute, tools/execute, tools/post-execute for tool calls;
  llm/stream or agent/request for model selection; approval/request for
  approvals)"`. The constants stay importable for one minor; they no longer
  register or dispatch.
* Unknown names raise `ValueError("unknown hook {name!r}; expected one of: ...")`.

Adding or changing a name/mode is a kernel change: update
`src/deeppyro/core/events.py` and `src/deeppyro/hooks/types.py` together and extend
the tests.

## 4. What actually fires in 0.3.0

The catalogue declares producers, but the call sites are authoritative. After
the v4 deletion the verified dispatch sites are:

| Name | Mode | Dispatched from | Contract |
|---|---|---|---|
| `tools/pre-execute` | waterfall | `src/deeppyro/tools/pipeline.py:258` | Decision `{"kind":"allow"|"deny"|"ask"|"cancel", ...}`. `deny`/`ask`/`cancel` short-circuit with an error `ToolResult`; a non-dict decision raises `ToolError`. Fallback = allow. |
| `tools/execute` | waterfall | `src/deeppyro/tools/pipeline.py:314` | Fallback executes the tool. A listener may return a `ToolResult` or `{"kind":"result","result": ...}` to replace execution. |
| `tools/post-execute` | waterfall | `src/deeppyro/tools/pipeline.py:349` | `{"kind":"accept", "content"|"value"}` may replace the result text/value; `{"kind":"block","feedback":...}` turns the result into an error. Fallback = accept. |
| `approval/request` | waterfall | `src/deeppyro/sandbox/approval.py:179` | Decides an approval ask. Payload: id, request, approval preview, agent, policy, signal. Fallback `"unavailable"`; `None`/`"ask"`/`"unavailable"` defer to the normal answerer; unknown listener results become `"unavailable"`. |
| `session/created` | emit | `src/deeppyro/headless.py:855`, `src/deeppyro/tui/runtime_services.py:77` | Notifies that a native session was opened (payload includes session id, cwd, preset). |
| `session/disposed` | emit | `src/deeppyro/headless.py:977` | Notifies that a session was closed; failures are isolated. |
| `command/executed` | emit | `src/deeppyro/builtin/commands.py:114` | A slash command ran; publish failures are logged, never raised. |
| `workspace/changed` | emit | `src/deeppyro/working_directory/service.py:261` | The working directory changed; the kernel plugin wires the emitter. |
| `goal/changed` | emit | `src/deeppyro/goal/service.py` (GOAL_BUS_EVENT emit helper) | Goal state changed; the native round driver subscribes (`src/deeppyro/goal/native_driver.py:182`). |
| `mcp/connected` / `mcp/disconnected` | emit | `src/deeppyro/mcp/service.py:184` / `:298`, `:338` | MCP server lifecycle; payload `{"server", "tools"}`. |

The native loop routes every tool call through the tool pipeline with the
runtime `HookService` (`src/deeppyro/agent/loop.py:744`), which is why the three
tool hooks are the supported way for a plugin to police tool use on the native
runtime.

### 4.1 Catalogued names with no current dispatcher

These names are declared in the catalogue and accepted by `HookService`, but
there is **no dispatch call site in `src` after the v4 stack deletion** (verified
2026-11-19). Registering them is allowed; delivery must not be assumed:

* `agent/pre-step`, `agent/request`, `agent/request-error`,
  `agent/turn-stopping`, `agent/assistant-stream`, `agent/status`,
  `agent/error` — these were v4 loop-boundary hooks. The native loop reports
  through typed `AgentHooks` instead (§2).
* `llm/stream` and `system-prompt/assemble` — declared producers are
  `agent.loop` / `prompt.service`, but no emitter remains in `src`.
* `goal/activation-changed` — catalogued; no emitter found at 2026-11-19
  (`src/deeppyro/goal/service.py` has a comment about publishing it).
* `agent/inbox/inserted`, `agent/inbox/claimed`, `agent/inbox/discarded` —
  the producer module (`agent/inbox.py`) was deleted with the v4 stack.

Several builtin plugins still subscribe to some of these names
(`builtin/usage.py`, `builtin/session_title.py`, `builtin/prompt_extension.py`,
`builtin/hook_observability.py`). They tolerate no delivery today. Do not
document them as live integration points without re-checking the code; the
catalogue producer field records intent, the call site is the fact.

## 5. Builtin listeners

| Plugin | Registers on | Effect |
|---|---|---|
| `builtin/hook_observability.py` | observer (curated: `llm/stream`, `system-prompt/assemble`, `approval/request`, `agent/error`, `session/created`, `session/disposed`) + `approval/request` | Keeps recent hook decisions for the TUI `/hooks` view. |
| `builtin/approval_policy.py` | `approval/request` (waterfall) | Applies the configured approval policy before the interactive answerer. |
| `builtin/prompt_extension.py` | `system-prompt/assemble` (waterfall) | Declared extension point; see §4.1. |
| `builtin/session_title.py` | `agent/turn-stopping` (serial) | Declared title-update rule; see §4.1. |
| `builtin/usage.py` | `agent/pre-step`, `agent/turn-stopping`, `llm/stream`, `session/disposed` | Legacy usage accounting; native usage is recorded by `agent/model_call.py` and `TurnUsage` records ([loop.md](loop.md) §12). |
| `observability/telemetry.py` | several names | Telemetry spans; see ops/telemetry. |

## 6. Migration notes

* Use `from deeppyro.hooks import HookService, HookObservation, HookAggregateError`
  and the name constants. `deeppyro.hooks.records` and
  `deeppyro.builtin.hook_journal` are deleted; importing them is a plugin bug
  in 0.3.0.
* `import deeppyro.core.wire`, `deeppyro.session.store`,
  `deeppyro.projections` and `deeppyro.session_query` are gone; plugins that
  imported them no longer load (see release notes).
* The v4 flags (`--legacy-v4`, `--native-loop`, `DEEPPYRO_NATIVE_LOOP`,
  `DEEPPYRO_TUI_NATIVE`) are removed; passing them is an argument error, and
  there is no hook-journal fallback.
* Plugins do not receive `AgentHooks`; typed hooks are host/observer code.
  Plugins interact through services and the string-named bus.

## 7. Tests

* `tests/hooks/test_service.py`, `tests/hooks/test_observer.py`,
  `tests/hooks/test_scope.py`, `tests/hooks/test_plugin.py` — facade,
  observers, scope filtering, plugin-provided service.
* `tests/integration/test_bus_modes.py`, `test_single_bus.py`,
  `test_bus_consumers.py`, `test_builtin_hooks_real.py`,
  `test_event_harness.py`, `test_observability_services.py` — dispatch
  semantics end to end.
* `tests/builtin/test_approval_policy.py`, `test_hook_observability.py`,
  `test_prompt_extension.py`, `test_session_title.py`, `test_usage.py` —
  builtin listeners.
