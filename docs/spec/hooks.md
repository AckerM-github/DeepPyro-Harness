# Runtime hooks

Status: implemented and aligned with the cordis event-bus semantics (P1 bus).
This document is the contract for the runtime hook service, the builtin hooks
plugin row, the nineteen well-known hook names, and the payload/decision
conventions call sites use. The implementation lives in src/deeppyro/hooks
(types.py, service.py) and src/deeppyro/builtin/hooks.py. The agent loop and
the tool pipeline are wired to the service as described in section 6; the last
ten names are declared constants whose call sites belong to later layers.

## 1. Design rules

* One service per kernel. HooksPlugin creates a single HookService and
  provides it as the hooks service, so every plugin and the runtime share the
  same listener registry.
* Names are fixed. Only the nineteen names in HOOK_NAMES may be registered or
  dispatched; typos raise ValueError instead of silently doing nothing.
* Payloads are plain dicts, not dataclasses. The service accepts the call
  site's dict and freezes it recursively before the first handler runs: nested
  mappings become MappingProxyType and lists become tuples. Handlers read keys
  but cannot mutate the payload; they return new values instead.
* Fallback results and values created by handlers are never frozen. Only the
  dispatch payload is read-only.
* Ordering is explicit. waterfall, serial, emit, and bail run handlers in
  registration order; prepend puts a handler first. parallel starts handlers
  concurrently and does not promise an observation order.
* Listener scope is explicit and local. A listener may register a scope mapping
  and dispatchers may pass one; matching uses subset semantics. global_ and
  unscoped listeners remain the default broadcast behaviour (section 2.10).
* Async or sync handlers are both accepted. The service awaits awaitable
  return values and otherwise uses plain values.
* Only asyncio. The service must run on the kernel event loop; registration and
  dispatch are safe for concurrent coroutines on that loop but there is no OS
  thread lock.
* Disposal. on() returns an idempotent disposer. A plugin normally wraps it
  with ctx.effect(disposer, ...) so the listener is removed when the plugin
  fiber unloads. A once listener disposes itself before its handler runs.
* The hooks plugin registers no tools, commands, or prompt sections. It only
  provides the service.

## 2. Service API

Module: src/deeppyro/hooks/service.py. Exports:
deeppyro.hooks.HookService and deeppyro.hooks.HookAggregateError.

    from deeppyro.hooks import HookAggregateError, HookService

    HookService()
    on(name, handler, *, prepend=False, once=False, scope=None, global_=False) -> disposer
    has(name) -> bool
    listeners(name) -> tuple[handler, ...]
    await waterfall(name, payload, fallback=None, *, scope=None) -> Any
    await serial(name, payload, *, scope=None) -> list[Any]
    await emit(name, payload, *, scope=None) -> None
    await bail(name, payload, *, scope=None) -> Any
    await parallel(name, payload, *, scope=None) -> None

on() validates the name (ValueError for an unknown hook, with the valid names
in the message), the handler (TypeError when it is not callable), and the scope
(TypeError for a non-mapping, a non-string key, or an unsupported value type;
an empty mapping is normalized to None), inserts at the front when prepend is
true, and returns a disposer. Calling the disposer
again is a no-op, and disposing one listener never removes another. With
once=True the wrapped listener removes itself before invoking the handler, so
a handler that dispatches the same hook again does not recurse; the returned
disposer stays idempotent.

has() and listeners() are read-only helpers: an unknown name yields False and
an empty tuple. The five dispatch methods reject unknown names with ValueError.

Every dispatch checks the cancellation signal before calling each listener
(section 2.8). waterfall, serial, emit, bail, and parallel all freeze the
payload first (section 2.7).

### 2.1 waterfall

Waterfall handlers have the signature handler(payload, next). next() is an
async callable that runs the rest of the chain and returns its result. The
polarity is the cordis one: the handler return value is the dispatch result.

* Veto: returning None without calling next() stops the dispatch. The result is
  None, downstream listeners do not run, and the fallback is not called.
* Continuation: calling await next() runs the rest of the chain and returns its
  result. If the handler then returns None, the dispatch result is that
  downstream result; if it returns a non-None value, that value overrides the
  downstream result.
* Short-circuit: returning a non-None value without calling next() becomes the
  dispatch result immediately; downstream listeners and the fallback are
  skipped.
* When the chain is exhausted the fallback is returned. A callable fallback is
  invoked with no arguments and an awaitable result is awaited; any other
  fallback is returned as is. The call-site fallbacks in this document are
  parameterless callables.
* Calling next() more than once raises RuntimeError. The error message names
  the hook.
* Handler exceptions (including CancelledError) propagate to the waterfall
  caller. Call sites decide whether that aborts a turn or becomes a tool error.

A veto is only "stop and return None"; it does not raise. Call sites that
require a decision object translate a None result into their own error or
default. Current consumers:

* agent/pre-step: a None result blocks the turn (turn/end {"kind": "blocked"}
  with reason kind "pre-step"). The block only applies when the service has a
  pre-step listener: with no listener the call site does not dispatch at all,
  so the built-in enter decision always runs.
* agent/request: keep the original request controls.
* agent/request-error: fail the turn; no retry.
* model/selection: keep the model selected by the step.
* system-prompt/assemble: keep the rendered prompt (no rewrite).
* approval/request: fall back to the configured approval policy
  (always -> allow, never -> deny, ask -> interactive question).
* tools/pre-execute, tools/execute, tools/post-execute: the pipeline raises
  ToolError for a None decision, which surfaces as an error result
  (section 5).

### 2.2 serial

Serial handlers have the signature handler(payload). Every handler runs in
order, the service collects each return value into a list, and it never
short-circuits. Exceptions propagate immediately; later handlers do not run.
If the cancellation signal is set before a listener, the dispatch stops and
returns the values collected so far.

### 2.3 emit

Emit handlers also have the signature handler(payload). Every handler runs in
order, but a handler exception is logged with logger.exception under the
deeppyro.hooks.service logger and the remaining handlers still run. emit()
returns None. CancelledError is a BaseException, so cancellation is not
swallowed. The signal check stops the remaining listeners without raising.

### 2.4 bail

Bail handlers have the signature handler(payload). Handlers run in order and
the first bailed value wins: bail() returns it immediately and later listeners
do not run. The isBailed rule mirrors cordis: a value bails when it is neither
None nor False. That means 0, an empty string, an empty dict, and every other
non-None truthy or falsey object bail; only None and False continue. When no
listener bails, bail() returns None. Exceptions propagate, and a set
cancellation signal stops the dispatch with None.

### 2.5 parallel

Parallel handlers have the signature handler(payload). All listeners start
concurrently with asyncio.gather(..., return_exceptions=True). When no handler
fails, parallel() returns None. When one or more handlers fail, the failures
are collected in registration order and raised as HookAggregateError:

    class HookAggregateError(Exception):
        errors: tuple[BaseException, ...]
        hook: str | None

The exception is raised from the first error (raise ... from errors[0]) and its
message names the hook and the number of failures. A set cancellation signal
prevents the dispatch and returns None.

### 2.6 once

on(name, handler, once=True) registers a listener that fires at most once for
that registration. The wrapper removes itself from the registry before calling
the handler, so inside the handler has(name) is already False and a nested
dispatch of the same hook cannot reach it again. The disposer returned by on()
still works and is idempotent; disposing early simply prevents the one call.
With a scope, once waits for the first matching dispatch; non-matching
dispatches do not consume the registration (section 2.10).

### 2.7 Payload freezing

Before dispatch every mode freezes the payload recursively:

* dict and other Mapping instances become types.MappingProxyType with frozen
  values.
* list and tuple instances become tuples with frozen items.
* Everything else, including asyncio.Event, AgentSession, ToolResult, and other
  custom objects, is kept by reference and is not traversed.
* The frozen view is created once per dispatch and shared by all listeners of
  that dispatch; listeners receiving the payload in different positions see
  the same object.
* The caller's original dict is not modified and is not the object handlers
  receive. A handler that writes a key or calls a mutating method gets
  TypeError or AttributeError.
* Fallback return values and values returned by handlers are not frozen: a
  listener may return a mutable dict and call sites may edit it.

Because custom objects keep identity, payload keys such as agent, signal, and
result behave as before; only the containers around them are read-only.

### 2.8 Cancellation

Before invoking each listener the service looks at payload.get("signal"). If a
signal key is present, the object has a callable is_set method, and is_set()
returns a true value, the dispatch stops without raising:

* waterfall returns the resolved fallback (the chain is not treated as
  exhausted, but the remaining listeners are skipped).
* serial returns the list collected so far.
* emit returns None.
* bail returns None.
* parallel dispatches no handlers and returns None when the signal is already
  set. A signal set by one parallel handler does not cancel siblings that have
  already started.

The check happens before each listener, so a handler can set the signal and the
next listener is skipped. Keys without is_set, non-callable is_set, or a false
is_set() result are ignored. Cancellation is a cooperative stop, not an
exception: the service never raises AbortedError by itself. A handler-raised
CancelledError still propagates like any other exception.

### 2.9 Concurrency and reentrancy

Each dispatch starts from a snapshot of the listener tuple, so adding or
removing listeners while a dispatch is running only affects later dispatches.
Separate dispatches have independent next() closures and may run concurrently.
A single handler must not treat its next() as a reusable or per-request object:
it may be called at most once, and calling it after the handler returned is not
supported.

### 2.10 Scope filtering (scope / global)

A listener may be narrowed to a subset of dispatches with on(..., scope={...}).
A dispatcher may identify itself with the keyword-only scope argument of any of
the five modes. The rules are:

* global_=True wins over every filter: the listener receives every dispatch of
  that hook, whatever the dispatch scope is. A listener may set both global_ and
  scope; global_ takes precedence.
* A listener with scope=None (the default, and also the normalization of {}) is
  unscoped and receives every dispatch.
* A dispatch with scope=None only reaches unscoped and global listeners.
  Scoped listeners stay dormant for that dispatch.
* A dispatch with scope=M reaches a scoped listener when the listener scope is a
  subset of M: every key of the listener scope exists in M and maps to an equal
  value. Extra keys in M do not matter.
* Filtering happens after the dispatch takes its registration snapshot and
  before the mode runs. A filtered-out listener is not called at all and cannot
  affect ordering, return values, cancellation, or HookAggregateError. prepend
  applies within the filtered set, so a matching prepended listener still runs
  first.
* once composes with scope: the registration stays dormant until the first
  matching dispatch, then disposes itself. A scoped once listener that never
  matches never fires.
* A dispatcher's invalid scope raises TypeError before any listener runs (the
  same validation as registration): non-mapping, non-string keys, or values
  other than str/int/bool are rejected. Booleans are accepted and compare
  distinctly from integers only by Python equality rules.
* The registered scope is copied into a read-only mapping, so mutating the
  caller's dict afterwards does not change the filter.

Recommended keys are session (the session id), agent (the agent name or id),
preset (the agent preset), and layer (a pipeline layer). Keep values scalar; the
service rejects None, floats, containers, and arbitrary objects so matching
stays deterministic and comparable.

Dispatch scope is supplied by the caller, not inferred from payload. Scoped
listeners only run when a dispatch carries a matching scope; an unscoped
dispatch skips them, while unscoped and global listeners always run.

Current wiring:

* src/deeppyro/agent/session.py dispatches every agent-side hook with
  scope = {"session": session_id}, plus {"preset": preset_id} when the agent
  has a preset id. One dispatch therefore isolates a Lead from its subagent
  sessions, and a preset-scoped listener can target a whole agent mode.
* src/deeppyro/tools/pipeline.py derives the same scope from the agent that
  owns the tool call (the agent passed as run_tool_batch(..., agent=...)), so
  tool/call and the three tool hooks follow the executing session.
* The approval gate (src/deeppyro/sandbox/gate.py) and the lifecycle emits in
  the runtime/headless entry points dispatch without a scope today, so their
  scoped listeners are dormant unless the caller supplies one.

The pipeline tolerates duck-typed hook objects that predate scope: when the
bound dispatch call rejects the scope keyword it retries without it, so test
doubles keep working. Real HookService always accepts the keyword.

## 3. Hook catalogue

HOOK_MODES in deeppyro.hooks.types records the dispatch mode. The table below
is the authoritative list of when each hook fires. Every name has a producer in
the default bundle; see sections 4 to 6 for payloads and decisions.

| Hook | Mode | When |
| --- | --- | --- |
| agent/pre-step | waterfall | before a step builds its request |
| agent/request | waterfall | after the request is built, before it is sent |
| agent/request-error | waterfall | after a request/stream failed, before the turn fails or retries |
| agent/turn-stopping | serial | while a turn is closing, before turn/end is recorded |
| agent/assistant-stream | emit | for each transient assistant stream frame |
| agent/status | emit | when the agent status phase changes |
| system-prompt/assemble | waterfall | when the system prompt is assembled |
| llm/stream | emit | for each raw provider stream event |
| model/selection | waterfall | when the model for a request is selected |
| session/created | emit | after a session is created (runtime/headless entry points) |
| session/disposed | emit | after a session is disposed (runtime/headless entry points) |
| agent/error | emit | when the agent records a fatal error |
| tools/pre-execute | waterfall | before a tool call is validated/executed |
| tools/execute | waterfall | instead of calling the tool implementation |
| tools/post-execute | waterfall | after the tool returned, before the result is finalized |
| tool/call | emit | when a tool call record is appended |
| approval/request | waterfall | before an approval request is raised |
| approval/asked | emit | when an approval request is shown to a human |
| approval/decided | emit | when an approval decision is made |

## 4. Agent hooks

The agent loop dispatches these hooks; payloads follow the reference agent loop
(turn/step integers, a cancellation signal, model selection and tool set
snapshots). A call site may add keys, but must keep these stable. Values are
frozen as described in section 2.7: lists such as messages or tools become
tuples inside the payload, while agent, message objects, and signal keep their
identity. Listeners that want the fallback or the downstream decision must
explicitly await next(); returning None without next is a veto.

### 4.1 agent/pre-step

    payload = {messages, turn, step, signal, modelSelection, agent}
    fallback = lambda: {"kind": "enter", "messages": claimed_messages}

waterfall: before a step starts, after the inbox claim and tool resolution.
The fallback enters the step with the claimed messages; the loop then appends
the runtime-context snapshot (if any). A listener should call next() to receive
the fallback when it needs the seed messages, then return
{"kind": "enter", "messages": [...], "startsRequestSeries": true} to replace
the admitted messages and force a new request series, or
{"kind": "reject", "reason": "..."} to block the turn. A rejection closes the
turn with turn/end {"kind": "blocked"} and writes no step/start. Any other kind
raises ValueError. A None dispatch result (a veto) is treated like no decision:
the step proceeds with the claimed messages.

### 4.2 agent/request

    payload = {turn, step, signal}
    fallback = lambda: controls

waterfall: once per model attempt, after the request controls are assembled.
fallback returns the seed controls (provider, model, reasoning effort, sampling
knobs); a listener returns a replacement controls mapping (usually after
await next()). provider/model/reasoningEffort are fixed by the step selection:
changing them raises an error instead of silently sending a different model.
A None result (veto) keeps the original request unchanged.

### 4.3 agent/request-error

    payload = {turn, step, provider, failure, retryPolicy, signal}
    fallback = lambda: None

waterfall: after assistant/attempt has been recorded and before the turn fails.
failure is {message, code, ...}; retryPolicy is the resolved retry policy or
None. Return {"kind": "retry"} to retry the same attempt; anything else fails
the turn, including the fallback None and the veto None. The loop writes
llm/retry and llm/retry-started through deeppyro.agent.retry.decide_retry
before retrying.

### 4.4 agent/turn-stopping

    payload = {turn, signal}
    await serial(...)

serial: after the last step ended and before turn/end, only when the inbox has
no pending next-step message. A listener may steer by appending to the inbox,
which keeps the turn open and runs another step. Return values are ignored;
exceptions propagate. A set signal stops the remaining serial listeners.

### 4.5 agent/assistant-stream

    payload = {frame}

emit: one process-local event per assistant stream frame
({"type": "start"|"chunk"|"end", ...}); frame is also sent to the UI sink.
Frames are not durable records; the payload is frozen, so listeners must not
mutate the frame. A failing listener is logged and does not stop the stream.

### 4.6 agent/status

    payload = {status}

emit: one process-local event whenever the agent phase changes between idle and
running; the existing status sink event is still emitted separately. This is
not a durable session event.

### 4.7 system-prompt/assemble

    payload = {agent, turn, step, prompt, tools, signal}
    fallback = lambda: prompt

waterfall: after the runtime-context projection and the prompt sections are
rendered, before the text becomes the session's system message. prompt is the
rendered string, tools is the tuple of tool names visible to the agent, and
signal is currently None (the commit is not cancellable). A non-None return
value replaces the prompt; returning a non-str raises SessionError
("system-prompt/assemble hook returned <type>, expected str"). A veto (None
without next) keeps the rendered prompt unchanged, so a listener cannot
accidentally blank the system message.

### 4.8 model/selection

    payload = {agent, turn, step, provider, model}
    fallback = lambda: {"model": model}

waterfall: at the top of a request, before the request controls are built. The
fallback carries the model selected when the step was admitted. A listener may
return a mapping with a replacement model; other keys are ignored, and changing
provider raises SessionError ("model/selection hook may not change the
provider"). A veto keeps the current model. The resulting model is written into
the request header and the request/context record.

### 4.9 llm/stream

    payload = {agent, frame, provider, model}

emit: once per raw provider stream event (start/chunk/end, text, reasoning, and
tool-call frames). It is dispatched after agent/assistant-stream, so UI
listeners should prefer the agent-level hook and tracing listeners this one.
The frame is a frozen dict; it is a transient observation and is not written to
the session log.

### 4.10 agent/error

    payload = {agent, error, phase}

emit: when the agent aborts a turn with a fatal error, just before turn/end
records the failure. error is the exception object. phase is one of "turn",
"request", or "tool" and says where the failure happened (provider errors
report "request"). A cancellation (Ctrl+C) does not dispatch this hook.
Listener errors are isolated by emit.

### 4.11 session/created

    payload = {sessionId, cwd, preset}

emit: after the TUI runtime creates a session through new_session or
create_schedule_session, and after headless creates the session for its run.
Opening an existing session with resume, or re-opening a stored schedule
session, does not dispatch it. preset is the agent preset id (or None). This is
emitted by process-local entry points, not by SessionStore/Session objects.

### 4.12 session/disposed

    payload = {sessionId}

emit: when the TUI runtime closes (once per session created in that process) and
when headless finishes its run, before the session is closed. It is not a
destructor callback on Session objects, and a session reused by later processes
is not dispatched again.

## 5. Tool hooks

The tool pipeline in src/deeppyro/tools/pipeline.py already dispatches these
three hooks when it receives a hooks object with an async waterfall method. The
payloads and decisions below match that implementation exactly.

Tool call payloads share the wire-shaped call dict:

    call = {"callId": str, "name": str, "arguments": str}

arguments is the raw JSON string requested by the model. turn and step may be
None when the caller cannot provide them. The pipeline appends the tool/call
record before pre-execute runs. Payloads are frozen before dispatch: call and
arguments become MappingProxyType, so a listener reads them and returns a new
decision instead of mutating them.

Every tool payload also carries agent and toolSet:

* agent is the executing AgentSession (the object the loop passes as
  run_tool_batch(..., agent=self)), or None for a direct pipeline caller that
  did not provide one. The key is always present, so a handler can distinguish
  "no agent" from "old payload shape".
* toolSet is the sorted list of tool names visible for this batch when the
  caller supplies tool_set; the agent loop passes its surface registry names
  (native or ptc). After freezing, handlers see it as a tuple with the same
  order and items. The key is omitted when the caller supplies no tool set.

Nested transports (the PTC dispatcher) pass the same two values so their tool
hooks look identical to the model-driven path.

A listener that returns None without awaiting next() vetoes the tool hook. The
pipeline requires a decision object from pre-execute and post-execute and a
ToolResult from execute, so such a veto becomes a ToolError and therefore an
error result for the call. To continue, a listener must explicitly
return await next() (or return its own decision or result).

### 5.1 tools/pre-execute

    payload = {"call": call, "tool": name, "turn": turn, "step": step,
               "agent": agent, "toolSet": tool_set}
    fallback = lambda: {"kind": "allow"}

Decision kinds:

* allow: execute normally.
* deny: the tool is not run; the result is ToolResult(text="Error: " + reason,
  is_error=True), where reason defaults to "tool execution denied".
* ask: the tool needs approval. Without an approval mechanism wired into the
  pipeline this becomes an error result that says approval is unavailable (the
  reason is appended when present). A runtime with an approval gate may handle
  ask before the pipeline or replace this decision.
* cancel: the call is cancelled; the result is an error result, default text
  "tool execution was cancelled".

An invalid kind or a non-dict decision raises ToolError; the pipeline turns
that into an error result, not a turn-fatal exception.

### 5.2 tools/execute

    payload = {"call": call, "tool": name, "turn": turn, "step": step,
               "arguments": arguments_dict, "agent": agent, "toolSet": tool_set}
    fallback = lambda: await tool.run(arguments_dict, ctx)

The fallback runs the real tool implementation. A listener that wants the
normal execution but with different arguments cannot mutate the frozen payload;
it builds a new arguments dict, calls the tool or returns its own result, or
returns await next() to run the real tool unchanged. A handler may return a
ToolResult directly to replace the result, or return
{"kind": "result", "result": ToolResult} (the result key is also accepted
without the kind). Any other non-None value is invalid and leads to a
ToolError. Handler exceptions become tool error results.

A handler that runs a different implementation should return a ToolResult; the
pipeline verifies the type after the waterfall.

### 5.3 tools/post-execute

    payload = {"call": call, "tool": name, "turn": turn, "step": step,
               "result": ToolResult, "agent": agent, "toolSet": tool_set}
    fallback = lambda: {"kind": "accept"}

Decision kinds:

* accept: keep the result. accept may carry content (replace ToolResult.text)
  or value (carried as meta["value"]); specifying both raises ValueError, and
  replacing the value of a failed result raises ValueError. Both are surfaced
  as ToolError and become error results.
* block: replace the result with ToolResult(text=feedback or a default
  message, is_error=True); the model sees the tool as failed.

Pipeline order for one call: tool/call record, tools/pre-execute, approval
handling (if any), tools/execute, post-execute, retention/truncation, then the
tool/result record. The approval gate is not part of the hooks layer; a call
site that handles ask does so before or around the pipeline.

## 6. Lifecycle, approval, and observation hooks

These hooks are dispatched by the approval gate, the tool pipeline, and the
process entry points rather than by the agent turn loop.

### 6.1 tool/call

    payload = {agent, callId, name, turn, step}

emit: after the tool/call record was appended to the session log and before the
call is validated or executed, so a tracing listener sees the call even when it
is denied, blocked, or aborted. It is dispatched by the batch pipeline path (the
agent loop calls run_tool_batch) and by the PTC fallback path; the single-call
PTC transport entry (run_tool_call) does not append a record and does not emit.
arguments are deliberately absent: they are available from the tool/call
record, and the hook must stay cheap for every call.

### 6.2 approval/request

    payload = {request, preset, policy, agent, signal}
    fallback = lambda: "ask"

waterfall: inside the permission gate, before the policy or a human is
consulted. request is the approval request object (kind, summary, and details),
preset is the session permission preset, policy is the approval policy name,
and agent/signal are None because the gate is not owned by one agent turn.

A listener returns one of "allow", "deny", "ask", or "cancel". allow/deny/cancel
short-circuit with reason "approved by approval plugin" / "denied by approval
plugin" / "approval canceled" and via "hook"; "ask" falls back to the normal
policy path (always/never decide directly, ask produces approval/asked and a
question). Any other string raises SessionError. A veto (None) is treated as
"ask", so a veto cannot silently grant or deny access.

### 6.3 approval/asked

    payload = {request, policy}

emit: just before the interactive approval question is shown to a human. It is
not dispatched for policy shortcuts (never/always) or hook decisions.

### 6.4 approval/decided

    payload = {request, allowed, reason, via}

emit: after every gate decision. via is "hook" for request-hook decisions,
"user" for an interactive answer, and "policy" for always/never shortcuts;
allowed is the boolean outcome and reason is the human-readable explanation.

### 6.5 hook/observability consumer

src/deeppyro/builtin/hook_observability.py is the in-tree consumer that makes
the observation hooks observable without a tracing plugin:

* It subscribes to ten hooks with an explicit await next() / no-op listener so
  a P1 consumer exists for each one.
* llm/stream and tool/call payloads are logged at DEBUG because they are hot;
  the rest are logged at INFO under the deeppyro.hooks logger. Log lines read
  "hook <name> key=value ...".
* snapshot() exposes per-hook dispatch counters for tests and diagnostics, and
  close() disposes every listener idempotently.

The plugin is a normal core-bundle row (id "hook-observability") and does not
change behaviour: every listener continues the chain.

## 7. Wiring

The hooks plugin is a normal kernel plugin row. The core bundle mounts it after
tools-builtin (inject is empty, so its position only affects availability
order):

    [[plugins]]
    id = "hooks"
    plugin = "deeppyro.builtin.hooks:HooksPlugin"

The runtime makes the service available to tool contexts by placing it in the
ToolContext.services dict under the hooks key, and to the agent loop by passing
it in AgentSession.services. Call sites use duck typing:

    hooks = services.get("hooks")
    if hooks is not None:
        result = await hooks.waterfall("tools/pre-execute", payload, fallback)

The tools/pipeline.py code already does exactly this and falls back to direct
execution when no hooks object is present, so old callers keep working. The
agent loop uses the same pattern: check for the service, dispatch the hook, and
apply the decision described above. Missing service means no listeners, not an
error.

The core bundle also mounts llm-retry (after hooks) and hook-observability
(after llm-retry). Both are ordinary rows with their own ctx.effect cleanup:

    [[plugins]]
    id = "llm-retry"
    plugin = "deeppyro.builtin.llm_retry:LlmRetryPlugin"

    [[plugins]]
    id = "hook-observability"
    plugin = "deeppyro.builtin.hook_observability:HookObservabilityPlugin"

Current call sites:

* src/deeppyro/agent/session.py dispatches agent/pre-step, agent/request,
  agent/request-error, agent/assistant-stream, agent/status,
  agent/turn-stopping, agent/error, model/selection, llm/stream,
  system-prompt/assemble, and the fallback tool/call emit; every dispatch
  carries the session scope described in section 2.10.
* src/deeppyro/tools/pipeline.py dispatches tools/pre-execute, tools/execute,
  tools/post-execute, and tool/call with the owning agent's scope.
* src/deeppyro/sandbox/gate.py dispatches approval/request, approval/asked, and
  approval/decided (no scope).
* src/deeppyro/tui/runtime.py and src/deeppyro/headless.py dispatch
  session/created and session/disposed.
* src/deeppyro/builtin/llm_retry.py registers an agent/request-error listener
  that returns {"kind": "retry"} or continues the chain.
* src/deeppyro/builtin/hook_observability.py registers a continuing listener
  for ten observation hooks and logs them.

No hook name is left without a producer in the default bundle.

## 8. Differences from DSH

Aligned with the cordis event bus:

* waterfall polarity matches cordis: the handler return value is the dispatch
  result, so returning None without next() vetoes; next() is an explicit
  continuation that runs the downstream chain.
* bail uses the cordis isBailed rule (value is neither None nor False), so 0
  bails.
* parallel uses gather with return_exceptions=True and aggregates failures into
  HookAggregateError, raised from the first failure.
* once removes the listener before invoking it, matching cordis on/once.

Remaining deviations:

* Payloads are dicts; DSH describes typed event payloads. The keys documented
  here are the compatibility surface.
* Payloads are frozen recursively before dispatch. DSH consumers receive the
  event object; deeppyro listeners cannot mutate payload containers. Use return
  values for decisions.
* There is no event bubbling or async event emitter hierarchy: one flat
  registry per kernel and explicit dispatch by name. DSH EventOptions also
  carries global/prepend; global_ is aligned with it, and prepend matches the
  cordis ordering flag. Scope filtering is a deeppyro extension local to this
  registry (section 2.10): a dispatch scope is explicit data, not an emitter
  tree, and there is no propagation of scope through ancestors.
* The service does not implement retry delays or approvals itself.
  agent/request-error only answers retry/stop; the delay and the llm/retry
  records come from deeppyro.agent.retry. ask/cancel from tools/pre-execute are
  normalized by the pipeline when no approval gate is wired.
* serial is a deeppyro extension that collects return values; cordis serial
  returns the first bailed value. bail provides the cordis-shaped API.
* Cancellation is a cooperative signal check before each listener, not an
  AbortedError raised by the service.
* Listener disposal is cooperative: plugins must dispose in their fiber
  cleanup (ctx.effect) so unloading a plugin removes its listeners.
* emit isolates handler exceptions; waterfall, serial, bail, and parallel
  propagate them (parallel aggregates them). This contract states the policy
  per mode.

Migration from the pre-P1 service:

* Lists in a payload are now tuples. Code that compared payload["toolSet"] to
  a list must compare list(payload["toolSet"]) or tuple(...).
* A waterfall listener that returned None to continue must now explicitly
  return await next(). Returning None without next() now vetoes, which at tool
  hooks is a ToolError and at agent hooks is a no-decision None.
* A waterfall listener can no longer mutate the payload to feed downstream
  listeners; build a new mapping or use the next() result instead.
* The "next() called more than once" RuntimeError is retained; its message
  names the hook.

## 9. Testing

    uv run pytest -o addopts= -q tests/hooks tests/integration/test_p0_bus_alignment.py \
        tests/integration/test_p1_bus.py

tests/hooks/test_service.py covers waterfall order, veto, short-circuit,
explicit continuation, fallback (plain and async), next-once enforcement,
exception propagation, prepend, serial ordering/exceptions/cancellation, emit
isolation and cancellation, bail rules, parallel concurrency and aggregation,
once registration and disposal, recursive payload freezing in every mode,
custom-object identity, fallback mutability, signal cancellation mid-chain,
empty dispatches, dispatch snapshots, and concurrent dispatches.
tests/hooks/test_scope.py covers session isolation, global and unscoped
broadcast, dormant scoped listeners under an unscoped dispatch, multi-key subset
matching, scope copying at registration, empty-scope normalization, all five
dispatch modes, prepend ordering after filtering, once with scope, idempotent
disposal, invalid registration and dispatch scopes, unknown names, cancellation,
payload freezing, and scope registration for every known hook.
tests/hooks/test_plugin.py covers the plugin contract (provide hooks, inject
empty) and a real kernel boot mounting HooksPlugin through PluginSpec.
tests/integration/test_p0_bus_alignment.py verifies the P0 bus surface against
the service, including frozen tuple payload lists and observer listeners that
explicitly await next().
tests/integration/test_p1_bus.py drives the real AgentSession and pipeline:
scoped listeners across two agents, dispatch without scope, tool/call scope
isolation, pre-step veto blocking the turn, system-prompt/assemble veto keeping
the rendered prompt, and approval/request veto falling back to the policy.
tests/agent/test_session_hooks_p1.py covers the five agent-side consumer hooks
(payload shapes, vetoes, provider/model constraints, error phases).
tests/builtin/test_hook_observability.py covers the observation plugin counters,
log levels, wiring, and idempotent close; tests/tui/test_runtime_hooks.py covers
session/created and session/disposed from the runtime; tests/tui/test_headless.py
covers the same for headless; tests/tools/test_sandbox.py covers the three
approval hooks.
