# Agent loop, prompt, hooks, and compaction (`deeppyro.agent`, `deeppyro.prompt`, `deeppyro.compact`)

Status: implemented (owner: loop-dev). Consumes only `core.protocols` / `core.errors` /
`core.wire`; writes durable records through `SessionLogLike.append` and emits UI events
through `EventSink.emit`. The loop knows nothing about the concrete `Session` implementation
and is exercised in tests with a surface-folding fake session.

Modules:

| module | contents |
| --- | --- |
| `src/deeppyro/agent/options.py` | `AgentOptions` |
| `src/deeppyro/agent/session.py` | `AgentSession`, `StreamAccumulator`, `ToolCallDraft` |
| `src/deeppyro/agent/inbox.py` | `Inbox` (durable next-turn / next-step queues) |
| `src/deeppyro/agent/request.py` | header planning, tool-change messages, runtime context payloads |
| `src/deeppyro/agent/stream.py` | `AssistantStreamAccumulator` (merged stream snapshots) |
| `src/deeppyro/agent/retry.py` | `decide_retry` (pure retry decision + record payloads) |
| `src/deeppyro/prompt/prompt.py` | `PromptContext`, `build_system_prompt`, `collect_instructions` |
| `src/deeppyro/prompt/projection.py` | system prompt + runtime-context projection |
| `src/deeppyro/prompt/runtime_context.py` | runtime-context sections/rendering |
| `src/deeppyro/hooks/` | `HookService`, hook names/modes (see `docs/spec/hooks.md`) |
| `src/deeppyro/compact/` | token accounting, prompts, surface fold, `Compactor` |

## 1. Public API

```python
from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.agent.images import ImageRequestPlan, plan_request_images
from deeppyro.prompt import PromptContext, build_system_prompt, collect_instructions
from deeppyro.compact import Compactor

AgentSession(
    *, session: SessionLogLike, provider: LlmProvider, registry: ToolRegistry,
    options: AgentOptions, sink: EventSink, cwd: Path,
    permission: PermissionGate | None = None,   # additive, optional
    services: dict[str, Any] | None = None,     # hooks / retryPolicy / tool services
    compactor: Compactor | None = None,         # additive, mainly for tests
    prompt_builder: Callable | None = None,     # additive, tests/custom prompts
)

await agent.send(text) -> str        # queue next-turn text, drive until idle, return final text
agent.followup(text, *, source=None) -> None   # queue next-turn text and wake an idle driver
agent.steer(text, *, source=None) -> None      # queue next-step text and wake the driver
agent.inject(text, *, source=None) -> None     # queue next-step text without waking the driver
await agent.wait_for_idle() -> None  # wait for every turn the driver was woken for
await agent.interrupt(*, clear_inbox=True) -> None
agent.busy, agent.model, agent.turn, agent.step, agent.inbox
agent.last_compaction_error          # last tolerated auto-compaction failure

Compactor(*, provider, model, keep_recent_tokens=8000, context_window=32768,
          threshold=0.85, max_tokens: int | None = None)
compactor.should_compact(messages) -> bool
await compactor.compact(session, messages=None, *, source_command_id=None) -> bool
```

`AgentOptions` (`@dataclass(slots=True)`) — frozen fields first, then additive ones:

```python
max_steps: int = 100
max_tokens: int | None = None
temperature: float | None = None
compact_threshold: float = 0.85
context_window: int = 32768
model: str = ""                     # additive: overrides provider model for requests
permission_preset: str | None = None  # additive: override for the system prompt
approval_policy: str | None = None    # additive: override for the system prompt
instructions: list[str] = field(default_factory=list)  # additive: extra instruction blocks
```

## 2. Driver, inbox, and turn/step state machine

One `AgentSession` owns one durable inbox. All user input is queued there; the driver
claims it turn by turn:

* `send(text)` pushes a next-turn message. If the driver is idle it runs every pending
  turn and returns the last assistant text; if a driver is already running, `send` returns
  `""` immediately and the message is consumed by the running driver. It never raises
  `SessionError("busy")` anymore.
* `followup` queues a next-turn message and wakes an idle driver.
* `steer` queues a next-step message and wakes the driver; `inject` queues a next-step
  message without waking an idle driver.
* The driver loop is `while not interrupted and inbox.has_pending: turn = _run_turn()`.
  `_drive` always publishes `running` then `idle` through the `agent/status` emit hook.

Each turn:

1. claim every pending **next-turn** message (`Inbox.claim`) — the turn runs even when the
   claim became empty, and then closes as `completed` with no request;
2. append `turn/start {"turn": n}` where `n = #turn/start records + 1`;
3. run the `agent/pre-step` waterfall; a `{"kind": "reject"}` result writes
   `turn/end {"kind":"blocked","reason":{"kind":"pre-step",...}}` without a step, and an
   `{"kind": "enter", "messages": [...]}` result may replace the admitted user messages and
   set `startsRequestSeries`;
4. run steps until the turn closes.

One step:

1. abort on a pending interrupt;
2. close with `max-steps` when `step >= options.max_steps`;
3. append `step/start` and emit the UI-only `step` event;
4. claim next-step inbox messages (except step 1, which uses the turn claim);
5. commit the system prompt projection and `user/message` records for the claimed messages;
6. commit the runtime-context projection (only when its rendered text changes);
7. write `request/header` when needed, a `developer/message` tool-change diff when the tool
   catalog changed, and `request/context` only when provider/model/context window changed;
8. emit `status thinking`, run auto-compaction, build and derive the request;
9. run the `agent/request` waterfall (controls or `LlmRequest`; provider/model may not change);
10. stream, append `assistant/message` and run tools, then close the step.

Turn end reasons (`turn/end {"turn", "reason"}`):

| reason | when |
| --- | --- |
| `{"kind": "completed"}` | model answered without tool calls, or no admitted messages |
| `{"kind": "max-steps"}` | `max_steps` exhausted |
| `{"kind": "max-tokens"}` | provider `Finish(kind="length")`; unexecuted tool calls are dropped |
| `{"kind": "blocked", "reason": {"kind": "pre-step", ...}}` | pre-step rejected the turn |
| `{"kind": "aborted", "reason": {"kind": "user"}}` | `interrupt()` |
| `{"kind": "aborted", "reason": {"kind": "provider"}}` | provider `Finish(kind="aborted")` |
| `{"kind": "aborted", "reason": {"kind": "disposed"}}` | external task cancellation |
| `{"kind": "error", "error": {"message", "code"?}}` | provider failure / unexpected exception |

## 3. Hooks at the loop boundary

The loop reads `services["hooks"]` (a `HookService`); without the service every hook is a
no-op. The nine names are fixed and documented in `docs/spec/hooks.md`. Loop call sites:

| hook | where | payload / result |
| --- | --- | --- |
| `agent/pre-step` | once per turn, before step 1 | `{messages, turn, step, signal, modelSelection, tools, agent}`; waterfall returns `{"kind":"enter","messages"}` or `{"kind":"reject","reason"}` (fallback `{"kind":"enter","messages":claimed}`) |
| `agent/request` | after the request is built | `{request, turn, step, signal, agent}`; waterfall fallback is the controls dict (`provider/model/reasoningEffort/maxTokens/temperature/contextWindow`); returns `LlmRequest` or controls, provider/model/reasoningEffort are immutable |
| `agent/request-error` | after a failed stream attempt | `{turn, step, provider, failure, retryPolicy, signal, agent}`; returning `{"kind":"retry"}` enables `decide_retry` and `llm/retry` / `llm/retry-started` records, otherwise the failure propagates as `LlmError` |
| `agent/turn-stopping` | serial, before closing a completed turn | `{turn, signal, agent}`; a listener may steer the loop |
| `agent/assistant-stream` | emit, for each transient stream frame | `{frame}` (`start`, `chunk`, `end`) |
| `agent/status` | emit, on phase changes | `{"status": "running"|"idle"}` |
| `tools/pre-execute`, `tools/execute`, `tools/post-execute` | inside `run_tool_batch` | see `docs/spec/hooks.md` §5 |

The provider keeps its own low-level `RetryPolicy` wrapper; the loop-level retry only runs
when a plugin registers an `agent/request-error` listener that answers `{"kind":"retry"}`.
The core bundle mounts the `llm-retry` plugin, which provides the `retryPolicy` service from
the `[retry]` config table and answers retry for retryable failures (normal mode) or every
failure (always mode), so a default runtime does emit `llm/retry` records. Disabling that
plugin (or a bundle without it) leaves the payload policy empty, no listener answers, and the
failure propagates as before.

The listener receives `{agent, turn, step, provider, failure, retryPolicy, signal}` and only
the `{"kind":"retry"}` answer starts the loop; the delay and the record pair come from
`deeppyro.agent.retry.decide_retry` (`llm/retry`, then `llm/retry-started` after the delay),
and the same request is streamed again without new request/header or request/context records.
See `docs/spec/llm.md` §6.1 for the plugin and its config table.

## 4. Records written by the loop

| record | `data` | envelope |
| --- | --- | --- |
| `turn/start` | `{turn}` | — |
| `turn/end` | `{turn, reason}` | — |
| `step/start` / `step/end` | `{turn, step}` | — |
| `system/message` | `{turn, step, message}` (source `{"kind":"system-prompt"}`) | `surfaceOp="append"` or a replace projection |
| `user/message` | the wire `Message` directly (`role, content, id, source`) | `surfaceOp="append"` or a replace projection (runtime-context) |
| `developer/message` | tool-change message from `tool_change_data` | `surfaceOp="append"` |
| `request/header` | `{header: {config, adapterDefaults, tools}, reason[, startsSeries: true]}` — no turn/step; `reason` is `initial` (first header of a new session), `resume` (first header after a restart with an existing baseline), `change` (header differs from the latest one) or `series` (unchanged header that merely starts a new request series). `startsSeries` appears on initial/resume/change records only. | — |
| `request/context` | `{provider, model[, contextWindow][, systemPromptUpdate]}` — no turn/step; optional fields are omitted rather than null, and the record is written only when one of the four tracked fields changed (missing counts as different from null). | — |
| `assistant/message` | `{turn, step, message, usage?, stream: [], interrupted?}` | `surfaceOp="append"` |
| `assistant/attempt` | `{turn, step, stream: [merged chunks]}` | — |
| `tool/call` | `{turn, step, callId, name, arguments}` (`arguments` is a JSON string) | — |
| `tool/result` | `{turn, step, message, meta?, error?}` (`message.role="tool"`, `source.kind="tool"`); `error` is `{name, code}` and only present for failures that carry error info | `surfaceOp="append"`, `sourceEventSeqs=[tool/call seq]` when known |
| `llm/retry`, `llm/retry-started` | retry record pair from `decide_retry` | — |
| `compaction/start`, `compaction/summary`, checkpoint `user/message`, `compaction/end` | section 9 | checkpoint replace envelope |
| `agent/inbox/spliced` | inbox mutation from `Inbox` | — |
| `image/offload` | `{targets: [{seq, imageIndexes}]}` — durable request-image offload decision (not a surface event; consumed by `derive_messages`) | — |

Cancellation while tools are in flight mirrors the reference pipeline: a call that was already
dispatched records `Error: tool call aborted` with `error={"name":"AbortError","code":"ABORTED"}`;
a call that had not been dispatched records `Error: tool call aborted before dispatch` with
`error={"name":"AbortError","code":"ABORTED_BEFORE_DISPATCH"}`, after the loop appends its own
`tool/call` so the pair stays valid. Both results carry `sourceEventSeqs=[tool/call seq]`.
A failure is written with `message.isError == true`; `error` is omitted when the tool result has
no error info (the session validator requires that pairing).

`usage` on `assistant/message` is camelCase like the fixtures:
`{"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens"}`;
`reasoningTokens` is added only when the provider reports reasoning token usage.

The envelope is passed only when the session's `append` accepts `**kwargs`; the loop probes
this once with `inspect.signature`. A minimal `SessionLogLike` without envelope support still
works, it just records events without surface information.

### 4.1 Request-side image planning

The durable log only ever stores attachment references, never image bytes. Before building a
provider request, each step runs `deeppyro.agent.images.plan_request_images` over the current
records:

1. fold the surface into `(record seq, Message)` pairs and apply the existing
   `image/offload` projection (so already-omitted occurrences are placeholder text);
2. resolve the remaining attachment references to inline `data:` URLs **in memory** through the
   `attachments` service; unresolvable refs stay as refs;
3. measure every occurrence (base64 length for inline data URLs) against the route
   `ImageBudget`; when the request exceeds it, select the oldest occurrences and return both the
   updated messages and the targets of a new `image/offload` record.

When `record_data()` is non-empty the loop appends exactly one `image/offload` record (before
`assistant/message`, after the step preamble) and then builds the request from the planned
messages, so the durable log and the wire request agree. Without an `attachments` service the
planner degrades to the pre-offload behaviour: derived messages unchanged, nothing written.
See `docs/spec/image-request.md` for the pipeline and `docs/spec/image-offload.md` for the
placeholder text.

## 5. Prompt and runtime context projections

The system prompt is a recorded `system/message`, never a request-level field. At every step
the loop renders `build_system_prompt(registry.schemas(), prompt_context)` and feeds it to
`SystemPromptProjection.project(rendered, in_history=True, starts_series=...)`:

* append a new head system node when there is none;
* replace the current head system node when the rendered text changed at a series start;
* otherwise leave the existing node untouched (a later step reuses the same prompt).

The runtime context is a second, append/replace stream of `user/message` records with
`source.kind="runtime-context"`. `RuntimeContextProjection` retains the newest snapshot and
projects the newly rendered text; when it changes, it replaces the retained snapshot so old
runtime-context records disappear from the derived history. `PromptContext` carries cwd,
platform, session id, start time, permission preset, approval policy, model, and instructions;
`collect_instructions(cwd)` walks `cwd` and its parents to the filesystem root, dedupes by
resolved path, reads `AGENTS.md` from root-most to deepest, and truncates each file at
`DEFAULT_MAX_INSTRUCTION_CHARS = 20000`.

## 6. Assistant stream snapshots and retries

`AssistantStreamAccumulator` merges provider deltas into the DSH-shaped `stream` array stored
on `assistant/message` / `assistant/attempt`: text and reasoning chunks, tool-call chunks with
`index/id/name/argumentsDelta`, block boundaries (`{"type":"block-start","index","blockType"}`
and `{"type":"block-end","index","block"}`), usage and finish chunks, each with `time`/`dt`.
Deltas carry their provider block `index` (text/reasoning no longer assume index 0), so a future
Anthropic-style adapter can interleave blocks without changing the record format. Usage chunks
may include `cacheWriteTokens` and `reasoningTokens`; a finish chunk carries
`reason.replayState` when the provider returned replay state. Adjacent fragments of the same
kind merge while their gap is at most `MAX_MERGE_GAP_MS = 60000`; `expand_stream` is the
inverse used by tests and tooling. On a failed attempt the snapshot is written to
`assistant/attempt` before `agent/request-error` runs.

`decide_retry` is pure: given the sanitized failure, `retryPolicy`, previous retry, provider,
turn, and step it returns a `RetryDecision` with `retry`/`retryId`/`delay_ms`/`mode`/
`policy_key` plus the two durable payloads. Normal mode retries only known retryable codes
within `maxRetries`; `always` ignores the budget; `providerRetryAfterMs` wins over local
backoff when the policy allows it; the retry id is reused per provider+policy chain. The loop
appends `llm/retry`, waits `delayMs`, appends `llm/retry-started`, and re-runs the step.

## 7. Events and ordering guarantees

Event shapes are the frozen ones from INTERFACES §7:

```text
{"type": "session", "sessionId"}
{"type": "step", "turn", "step"}
{"type": "status", "phase": "thinking" | "tool" | "done"}
{"type": "text", "text"}
{"type": "thinking", "text"}
{"type": "tool_call", "callId", "name", "arguments"}
{"type": "tool_result", "callId", "isError", "text"}
{"type": "usage", "input", "output", "cached"}
{"type": "final", "text"}
{"type": "error", "message"}
```

Ordering rules implemented here:

* `step` is a UI-only hint emitted immediately after the `step/start` record and before the
  model stream; the durable `step/start` record remains the only step bookkeeping.
* `session` is a frontend event: the headless runner emits it once before the first
  `turn/start`; `AgentSession` never emits it, so JSON consumers see it exactly once.
* `status thinking` is emitted after the step preamble records and before compaction /
  the provider request; `status tool` after the assistant record and before the first
  `tool/call`.
* `text` / `thinking` deltas are emitted **while the provider stream is running**, i.e. before
  the `assistant/message` record exists. A live UI cannot wait for the record; every other
  event mirrors a record and is emitted only after that record was appended:
  `usage` after `assistant/message`, `tool_call` after `tool/call`, `tool_result` after
  `tool/result`, `error` after the assistant message/attempt + before `turn/end`.
* `final` is emitted only for successful turn ends (completed, max-tokens, max-steps) and
  never for errors/aborts.
* `status done` is emitted at the end of every turn, after `turn/end`.
* Concrete sinks live in `deeppyro/headless.py` and `deeppyro/tui/sink.py`; the agent package
  only defines the `EventSink` protocol usage.

## 8. Tool execution

Calls returned by one stream are grouped in provider order: consecutive `parallel_safe`
tools form one concurrent group (an unknown tool is treated as parallel-safe), and every
barrier tool gets its own group. Per group the loop:

1. lets `run_tool_batch` append all `tool/call` records through its `log` callback before any
   pre-execute hook runs;
2. executes through `deeppyro.tools.run_tool_batch(..., registry, ctx_factory, hooks=...,
   turn=..., step=..., log=...)`, with `ToolContext(cwd, session_id, turn, step, call_id,
   cancel=async Event, log=session.append, permission, services)`;
3. appends one `tool/result` per call in request order (then emits `tool_result`), carrying
   `ToolResult.meta` and `ToolOutcome.call_seq` as `sourceEventSeqs`.

So parallel groups log `call, call, result, result` while barrier tools log
`call, result, call, result`. Unknown tools, malformed JSON arguments, `PermissionDenied`,
`ToolError`, hook failures, and unexpected tool exceptions become
`ToolResult(is_error=True, text="Error: ...")`; only `CancelledError` propagates. The
assistant message is always appended before any `tool/call` record.

## 9. Compaction

### 9.1 Token accounting

`estimate_message_tokens` = `(len(text) + len(reasoning) + len(tool-call name) +
len(tool-call arguments)) / 3.5 + 4`; `CHARS_PER_TOKEN = 3.5`, `MESSAGE_OVERHEAD = 4.0`.
`should_compact(messages)` is `estimate_messages_tokens(messages) > threshold *
context_window` (`context_window <= 0` disables it).

### 9.2 Surface fold

The durable log is append-only; visible context is a *surface* of system/developer/user/
assistant messages and tool results. Each such record carries `surfaceOp`:

* `"append"` (or missing) pushes one node `(seq, type, data)`;
* `{"op": "replace", "startSeq", "endSeq"}` splices the node at the first replaced position.

`fold_surface` validates replacements exactly like the session writer: `startSeq/endSeq`
must be present on the surface and earlier than the event seq, `sourceEventSeqs` must be
non-empty, unique, all earlier, and include every shadowed node, a `system/message` head may
only be replaced by that same single system node, and a negative tool-call balance is
rejected. The fold accepts both pydantic `ReplaceOp` objects and plain dicts, so folding a
real `Session` twice cannot fail on its own record type.

### 9.3 Region selection

`select_region(records, retain_tokens)` folds the surface, never shadows a head system
message, accumulates node token estimates from the tail until `retain_tokens` is reached,
moves the cut older while the cut would split an assistant tool-call from its result, and
returns the contiguous region `[first_index, keep_from-1]` with its shadowed seqs and
estimated tokens. It returns `None` when nothing safe can be shadowed; `compact` then
returns `False` without writing anything.

`entry_state(records)` recovers the open turn (`turn/start` without `turn/end`), the latest
`session/end-seed` seq, and an unmatched `compaction/start` (same `compactionId` with no
`compaction/end`). An unmatched start older than the latest end-seed is ignored; otherwise
it means another compaction is running and `compact` raises `SessionError`.

### 9.4 Summarization call

`compact` replays the surface *before* the region (`[system/message?, ...region messages]`)
and appends one final user message with the DSH `COMPACTION_INSTRUCTION` (structured Markdown
checkpoint, merge-prior-checkpoint rule). The request uses the loop model/provider, the tools
advertised by the latest `request/header`, `max_tokens` from the compactor, and the session
id. The stream is consumed for `TextDelta`; `Finish(kind="error")` → `LlmError`,
`aborted` → `AbortedError`, `length` → `LlmError("summarization truncated at the token cap
(incomplete checkpoint)")`, `content-filter` → `LlmError`, empty text → `LlmError`.
The checkpoint is `role="user"` with the preamble + `<compacted-summary>` framing,
`source={"kind": "compact-checkpoint", "compactionId": ...}`, and a fresh message id.
Before committing, the framed checkpoint must estimate strictly smaller than the shadowed
region; otherwise `LlmError("summary is not smaller than the shadowed content ...")`.

### 9.5 The four-record transaction

A successful compaction appends, in order, with no yields between the commitments:

1. `compaction/start {"compactionId", "sourceCommandId"?, "turn"?}` — `turn` only when a turn
   is open (auto compaction); it is the durable lock;
2. `compaction/summary {"compactionId", "sourceCommandId"?, "summary": [text block],
   "rawOutput": [text block], "llmStreamCall": true, "shadowedRange": {"start","end"},
   "shadowedSeqs": [...], "shadowedTokenCount", "provider", "model", "maxTokens"?, "usage"?}`;
3. checkpoint `user/message` whose `data` is the checkpoint `Message`, envelope
   `surfaceOp={"op":"replace","startSeq","endSeq"}` and
   `sourceEventSeqs=[start seq, summary seq, ...shadowedSeqs]` — this removes the old span;
4. `compaction/end {"compactionId", "sourceCommandId"?, "turn"?}`.

`sourceCommandId` is copied from the `source_command_id` keyword and omitted when absent.
The surface must still contain the selected start/end after summarization (stability check).
After the transaction the compactor calls `session.derive_messages()` and verifies the
checkpoint id is present, raising `SessionError` otherwise. Any failure after the start
record is answered with a single `compaction/end` carrying
`error={message, name, code?}`, and the original exception is re-raised (the record-write
failure itself is logged, never masking the original error).

### 9.6 Integration with the loop

Auto-compaction runs inside the open turn, once per step, after `status thinking` and before
deriving the request messages. Failures other than `CancelledError` are logged and stored on
`AgentSession.last_compaction_error`; the turn continues. A `Compactor` passed to
`AgentSession` overrides the default construction (`keep_recent_tokens=8000`,
`context_window=options.context_window`, `threshold=options.compact_threshold`).
`compact(session, messages)` ignores `messages` (kept for the frozen signature) and selects
the region from `session.records`, so seqs remain authoritative.

## 10. Cancellation and failures

`interrupt()` sets the interrupt flag and `ToolContext.cancel`, clears the inbox by default,
cancels the driver task when called from another task, and waits up to 5 s for the turn to
record its abort. The resulting records are: a partial `assistant/message` when any
reasoning/text/tool-call content arrived (tool-call parts are dropped because they never ran),
synthetic `tool/result` records with `text="Error: interrupted"` for calls that were announced
but not finished, `step/end`, and `turn/end {"kind":"aborted","reason":{"kind":"user"}}`.
`send` returns the partial assistant text; no `final`/error event is emitted.

Other failure paths:

* provider `Finish(kind="aborted")` → same shape but reason `provider`;
* provider `Finish(kind="error")` or a stream that ends without `Finish` → append a partial
  `assistant/message` when content exists, otherwise `assistant/attempt`; emit `error`,
  `step/end`, `turn/end error`; re-raise `LlmError`;
* `Finish(kind="length")` → partial assistant message without tool calls, `step/end`,
  `turn/end {"kind":"max-tokens"}`, `error` event naming the dropped tool calls;
* an unexpected exception follows the provider-error path and is re-raised;
* external `CancelledError` records `{"kind":"aborted","reason":{"kind":"disposed"}}` and
  re-raises.

## 11. Known deviations / risks

1. **Streaming event order.** `text`/`thinking` deltas necessarily precede the
   `assistant/message` append. All record-mirroring events follow their record.
2. **Interrupt tool results lack `sourceEventSeqs`.** Synthetic `Error: interrupted` results
   are appended without the original `tool/call` seq (the abort path does not retain it).
   Fold validation tolerates this; a later improvement should carry the seq.
3. **Idle `inject` is consumed at step 2.** A next-step message injected while idle is not
   claimed by step 1 of the next turn (step 1 consumes the turn claim); it lands at the next
   step boundary. This is intentional queue semantics but differs from a fully synchronous
   "inject before step 1" reading.
4. **Loop-level retry needs a listener.** `agent/request-error` retry records appear only when
   a plugin registers a listener returning `{"kind":"retry"}`; the provider's own
   `RetryPolicy` retries transparently before that point.
5. **Header reason heuristic.** The loop infers `initial` / `series` / `resume` from the
   previous `turn/end`; it does not see RPC resume semantics.
6. **Heuristic token estimate.** `len/3.5 + 4` per message is frozen by INTERFACES and is
   used both for pressure and for region sizing.
7. **Checkpoint shape.** The summary is stored as one text block (`rawOutput` mirrors it);
   DSH may retain multiple output blocks. `errorChain` is simplified to
   `{message, name, code?}`.
8. **No loop-level `sourceCommandId`.** `Compactor.compact` accepts and writes it, but the
   agent loop does not pass one yet (there is no command/job id in the turn context).
9. **Session dependency.** Envelopes require the real `Session.append(type, data, **envelope)`.
   The loop probes this; a reduced `SessionLogLike` cannot record surface ops and thus cannot
   support compaction. `Compactor` requires `session.records`.
10. **Additive options/kwargs.** `model`, `permission_preset`, `approval_policy`,
    `instructions`, `permission=None`, `services`, `compactor`, and `prompt_builder` are
    additions beyond INTERFACES §7; all have defaults, so the frozen constructor holds.
11. **Prompt content is DeepPyro-specific** (not DSH's runtime prompt), and it never embeds
    host memory protocols.
12. **Image offload is proactive only.** The loop budgets before the request and writes
    `image/offload`; it does not implement DSH's `IMAGE_OFFLOAD_REQUIRED` error/retry path.
    P1 sends inline base64 only (no files route, no normalized-path placeholder variant), and
    `http(s)` image URLs are forwarded while counting zero represented bytes.

## 12. Test coverage

`uv run pytest -o addopts= -q tests/agent tests/hooks tests/prompt tests/tools/test_pipeline_hooks.py`
covers the loop, inbox, stream, retry, projection, hooks, image planning, and pipeline
integration. The full suite at the P1 close was 2224 passed.

* `tests/agent/fakes.py` — `FakeSessionLog` (real `LogRecord` + strict canonical surface fold),
  `ScriptedProvider`, `FakeTool`/`FakeRegistry`, `RecordingSink`, `FakePermission`.
* loop: exact record/event sequences; header reasons; system + runtime-context projections;
  parallel and barrier tool ordering; tool hooks; error/unknown tools; interrupt mid-stream;
  queued sends/followup/steer/inject; busy driver semantics; max-steps; length finish with
  dropped tool calls; provider error → `assistant/attempt` + `turn/end error`;
  auto-compaction inside the turn; tolerated compaction failure; failed attempt then retry.
* inbox: replay of `agent/inbox/spliced`, claim order, clear, splice/remove/replace, id
  uniqueness, no-op mutations.
* stream: merge gaps, snapshot deep copy, `expand_stream` inverse, tool-call deltas.
* retry: normal/always modes, retryable codes, provider Retry-After, jitter bounds, retry-id
  reuse, record key order.
* projection: append/replace rules, retained runtime-context snapshot, cleared text.
* hooks: waterfall/serial/emit semantics, unknown names, disposal, plugin row.
* compaction: unchanged from the pre-P0 suite plus `sourceCommandId` on all four records.
* images: `tests/agent/test_images.py` covers surface-seq pairing, existing-offload projection,
  attachment expansion (copy-on-write, dedup, bare ids, unresolvable refs), budget-triggered
  oldest-prefix selection and placeholder merging; `tests/agent/test_agent_images.py` covers
  the loop wiring (one `image/offload` per step before the request, refs-only durable log).

Lint: `uv run ruff check src tests` and `uv run ruff format --check src tests` are clean.
