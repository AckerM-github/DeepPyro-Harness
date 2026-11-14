# Runtime hooks

Status: implemented. This document is the contract for the runtime hook
service, the builtin hooks plugin row, the nine well-known hook names, and the
payload/decision conventions call sites use. The implementation lives in
src/deeppyro/hooks (types.py, service.py) and src/deeppyro/builtin/hooks.py.
The agent loop and the tool pipeline are not modified by this layer; the
runtime wires them to the service as described in section 6.

## 1. Design rules

* One service per kernel. HooksPlugin creates a single HookService and
  provides it as the hooks service, so every plugin and the runtime share the
  same listener registry.
* Names are fixed. Only the nine names in HOOK_NAMES may be registered or
  dispatched; typos raise ValueError instead of silently doing nothing.
* Payloads are plain dicts, not dataclasses. The service forwards the exact
  dict object to every handler of one dispatch, so handlers can read extra
  keys without an API change.
* Ordering is explicit. waterfall and serial run handlers in registration
  order; prepend puts a handler first. Emit also runs in registration order.
* Async or sync handlers are both accepted. The service awaits awaitable
  return values and otherwise uses plain values.
* Only asyncio. The service must run on the kernel event loop; registration and
  dispatch are safe for concurrent coroutines on that loop but there is no OS
  thread lock.
* Disposal. on() returns an idempotent disposer. A plugin normally wraps it
  with ctx.effect(disposer, ...) so the listener is removed when the plugin
  fiber unloads.
* The hooks plugin registers no tools, commands, or prompt sections. It only
  provides the service.

## 2. Service API

Module: src/deeppyro/hooks/service.py. Export: deeppyro.hooks.HookService.

    HookService()
    on(name, handler, *, prepend=False) -> disposer
    has(name) -> bool
    listeners(name) -> tuple[handler, ...]
    await waterfall(name, payload, fallback=None) -> Any
    await serial(name, payload) -> list[Any]
    await emit(name, payload) -> None

on() validates the name (ValueError for an unknown hook, with the valid names
in the message) and the handler (TypeError when it is not callable), inserts at
the front when prepend is true, and returns a disposer. Calling the disposer
again is a no-op, and disposing one listener never removes another.

has() and listeners() are read-only helpers: an unknown name yields False and
an empty tuple. The three dispatch methods reject unknown names with ValueError.

### 2.1 waterfall

Waterfall handlers have the signature handler(payload, next). next() is an
async callable that runs the rest of the chain and returns its result. The
service starts at the first handler and follows next-step links:

* A non-None return value short-circuits the chain and becomes the result.
* A None return value continues the chain. If the handler did not call next()
  itself, the service calls it once after the handler returns; if the handler
  already called next(), the result of that call is returned.
* When the chain is exhausted the fallback is returned. A callable fallback is
  invoked with no arguments and an awaitable result is awaited; any other
  fallback is returned as is. The call-site fallbacks in this document are
  parameterless callables.
* Calling next() more than once raises RuntimeError. The error message names
  the hook.
* Handler exceptions (including CancelledError) propagate to the waterfall
  caller. Call sites decide whether that aborts a turn or becomes a tool error.

### 2.2 serial

Serial handlers have the signature handler(payload). Every handler runs in
order, the service collects each return value into a list, and it never
short-circuits. Exceptions propagate immediately; later handlers do not run.

### 2.3 emit

Emit handlers also have the signature handler(payload). Every handler runs in
order, but a handler exception is logged with logger.exception under the
deeppyro.hooks.service logger and the remaining handlers still run. emit()
returns None. CancelledError is a BaseException, so cancellation is not
swallowed.

### 2.4 Concurrency and reentrancy

Each dispatch starts from a snapshot of the listener tuple, so adding or
removing listeners while a dispatch is running only affects later dispatches.
Separate dispatches have independent next() closures and may run concurrently.
A single handler must not treat its next() as a reusable or per-request object:
it may be called at most once, and calling it after the handler returned is not
supported.

## 3. Hook catalogue

HOOK_MODES in deeppyro.hooks.types records the recommended dispatch mode. The
table below is the authoritative list of when each hook fires.

| Hook | Mode | When |
| --- | --- | --- |
| agent/pre-step | waterfall | before a step builds its request |
| agent/request | waterfall | after the request is built, before it is sent |
| agent/request-error | waterfall | after a request/stream failed, before the turn fails or retries |
| agent/turn-stopping | serial | while a turn is closing, before turn/end is recorded |
| agent/assistant-stream | emit | for each transient assistant stream frame |
| agent/status | emit | when the agent status phase changes |
| tools/pre-execute | waterfall | before a tool call is validated/executed |
| tools/execute | waterfall | instead of calling the tool implementation |
| tools/post-execute | waterfall | after the tool returned, before the result is finalized |

## 4. Agent hooks

The agent loop dispatches these hooks; payloads follow the reference agent loop
(turn/step integers, a cancellation signal, model selection and tool set
snapshots). A call site may add keys, but must keep these stable.

### 4.1 agent/pre-step

    payload = {messages, turn, step, signal, modelSelection, agent}
    fallback = lambda: {"kind": "enter", "messages": claimed_messages}

waterfall: before a step starts, after the inbox claim and tool resolution.
The fallback enters the step with the claimed messages; the loop then appends
the runtime-context snapshot (if any). A listener may call next() to receive the
fallback, then return {"kind": "enter", "messages": [...],
"startsRequestSeries": true} to replace the admitted messages and force a new
request series, or {"kind": "reject", "reason": "..."} to block the turn. A
rejection closes the turn with turn/end {"kind": "blocked"} and writes no
step/start. Any other kind raises ValueError.

### 4.2 agent/request

    payload = {turn, step, signal}
    fallback = lambda: controls

waterfall: once per model attempt, after the request controls are assembled.
fallback returns the seed controls (provider, model, reasoning effort, sampling
knobs); a listener returns a replacement controls mapping (usually next() then
edit it). provider/model/reasoningEffort are fixed by the step selection:
changing them raises an error instead of silently sending a different model.

### 4.3 agent/request-error

    payload = {turn, step, provider, failure, retryPolicy, signal}
    fallback = lambda: None

waterfall: after assistant/attempt has been recorded and before the turn fails.
failure is {message, code, ...}; retryPolicy is the resolved retry policy or
None. Return {"kind": "retry"} to retry the same attempt; anything else (None
included) fails the turn. The loop writes llm/retry and llm/retry-started
through deeppyro.agent.retry.decide_retry before retrying.

### 4.4 agent/turn-stopping

    payload = {turn, signal}
    await serial(...)

serial: after the last step ended and before turn/end, only when the inbox has
no pending next-step message. A listener may steer by appending to the inbox,
which keeps the turn open and runs another step. Return values are ignored;
exceptions propagate.

### 4.5 agent/assistant-stream

    payload = {frame}

emit: one process-local event per assistant stream frame
({"type": "start"|"chunk"|"end", ...}); frame is also sent to the UI sink.
Frames are not durable records; listeners must not mutate the dict.

### 4.6 agent/status

    payload = {status}

emit: one process-local event whenever the agent phase changes between idle and
running; the existing status sink event is still emitted separately. This is
not a durable session event.

## 5. Tool hooks

The tool pipeline in src/deeppyro/tools/pipeline.py already dispatches these
three hooks when it receives a hooks object with an async waterfall method. The
payloads and decisions below match that implementation exactly.

Tool call payloads share the wire-shaped call dict:

    call = {"callId": str, "name": str, "arguments": str}

arguments is the raw JSON string requested by the model. turn and step may be
None when the caller cannot provide them. The pipeline appends the tool/call
record before pre-execute runs.

Every tool payload also carries `agent` and `toolSet`:

* agent is the executing `AgentSession` (the object the loop passes as
  `run_tool_batch(..., agent=self)`), or None for a direct pipeline caller that
  did not provide one. The key is always present, so a handler can distinguish
  "no agent" from "old payload shape".
* toolSet is the sorted list of tool names visible for this batch when the
  caller supplies `tool_set`; the agent loop passes its surface registry names
  (native or ptc), so a handler can tell which tools the model could call. The
  key is omitted when the caller supplies no tool set.

Nested transports (the PTC dispatcher) pass the same two values so their tool
hooks look identical to the model-driven path.

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

The fallback runs the real tool implementation. A handler may return None to
continue to the next handler, return a ToolResult directly to replace the
result, or return {"kind": "result", "result": ToolResult} (the result key is
also accepted without the kind). Any other non-None value is invalid and leads
to a ToolError. Handler exceptions become tool error results. The arguments
field is the decoded JSON object, so a handler can inspect or rewrite it by
mutating the payload before calling next().

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

## 6. Wiring

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
agent loop should use the same pattern: check for the service, dispatch the
hook, and apply the decision described above. Missing service means no
listeners, not an error.

## 7. Differences from DSH

* Payloads are dicts; DSH describes typed event payloads. The keys documented
  here are the compatibility surface.
* Only the nine names above exist. DSH has additional lifecycle events (for
  example request-header and tool-call bookkeeping); those remain ordinary
  runtime sink events, not hooks.
* There is no event bubbling, global/local scope, or async event emitter
  hierarchy: one flat registry per kernel and explicit dispatch by name.
* The service does not implement retry delays or approvals itself.
  agent/request-error only answers retry/stop; the delay and the llm/retry
  records come from deeppyro.agent.retry. ask/cancel from tools/pre-execute are
  normalized by the pipeline when no approval gate is wired.
* Listener disposal is cooperative: plugins must dispose in their fiber
  cleanup (ctx.effect) so unloading a plugin removes its listeners.
* emit isolates handler exceptions; waterfall and serial propagate them. DSH
  consumers decide their own error policy; this contract states it per mode.

## 8. Testing

    uv run pytest -o addopts= -q tests/hooks

tests/hooks/test_service.py covers waterfall order, prepend, short-circuit,
automatic continuation, fallback (plain and async), next-once enforcement,
exception propagation, serial ordering and exceptions, emit isolation and
cancellation, has/listeners/disposer, invalid names, non-callable handlers,
empty dispatches, dispatch snapshots, and concurrent dispatches.
tests/hooks/test_plugin.py covers the plugin contract (provide hooks, inject
empty) and a real kernel boot mounting HooksPlugin through PluginSpec.