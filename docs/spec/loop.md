# Agent loop, prompt, and compaction (`deeppyro.agent`, `deeppyro.prompt`, `deeppyro.compact`)

Status: implemented (owner: loop-dev). Consumes only `core.protocols` / `core.errors` /
`core.wire`; writes durable records through `SessionLogLike.append` and emits UI events
through `EventSink.emit`. The loop knows nothing about the concrete `Session` implementation
and is exercised in tests with a surface-folding fake session.

Modules:

| module | contents |
| --- | --- |
| `src/deeppyro/agent/options.py` | `AgentOptions` |
| `src/deeppyro/agent/session.py` | `AgentSession`, `StreamAccumulator`, `ToolCallDraft` |
| `src/deeppyro/prompt/prompt.py` | `PromptContext`, `build_system_prompt`, `collect_instructions` |
| `src/deeppyro/compact/tokens.py` | heuristic token accounting |
| `src/deeppyro/compact/prompts.py` | DSH-compatible checkpoint framing + summarization instruction |
| `src/deeppyro/compact/surface.py` | surface fold, tool pairing, region selection |
| `src/deeppyro/compact/compactor.py` | `Compactor` (4-record compaction transaction) |

## 1. Public API

```python
from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.headless import JsonLinesSink, RichSink   # frontend layer (INTERFACES section 7)
from deeppyro.prompt import PromptContext, build_system_prompt, collect_instructions
from deeppyro.compact import Compactor

AgentSession(
    *, session: SessionLogLike, provider: LlmProvider, registry: ToolRegistry,
    options: AgentOptions, sink: EventSink, cwd: Path,
    permission: PermissionGate | None = None,          # additive, optional
    services: dict[str, Any] | None = None,            # additive, passed to ToolContext
    compactor: Compactor | None = None,                # additive, mainly for tests
)
await agent.send(text) -> str     # final assistant text of the turn ("" when none)
await agent.interrupt() -> None   # cancel the running turn, record a user abort
agent.busy, agent.model, agent.turn, agent.step
agent.last_compaction_error        # last tolerated auto-compaction failure

Compactor(*, provider, model, keep_recent_tokens=8000, context_window=32768,
          threshold=0.85, max_tokens: int | None = None)
compactor.should_compact(messages) -> bool
await compactor.compact(session, messages=None) -> bool   # False = no safe range
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

## 2. Turn/step state machine

`send` runs exactly one turn:

1. raise `SessionError("agent is busy with another turn")` when `busy` is already set;
2. append `turn/start {"turn": n}` where `n = #turn/start records + 1`;
3. run the step loop;
4. always clear `busy` and resolve the interrupt wait event.

One step:

1. if a user interrupt was requested, abort the turn (section 7);
2. if `step >= options.max_steps`, close the turn with `{"kind": "max-steps"}` and return the
   last assistant text (the overflow step is never started);
3. append `step/start {"turn", "step"}` with `step = 1, 2, ...`;
4. first step of every turn only:
   - `system/message` when the session has no `system/message` record yet (once per session);
   - `user/message` for the prompt text;
   - `request/header` with the tool catalog;
   - `request/context` when the provider/model/context window differs from the last one;
5. emit `{"type": "status", "phase": "thinking"}`;
6. auto-compaction when `Compactor.should_compact(derived_messages)` is true (section 8);
7. derive messages and stream `LlmRequest(provider=provider.name, model=options.model or
   provider.model or "unknown", messages=derived, system=None, tools=registry.schemas(),
   max_tokens, temperature, context_window, session_id)` — the system prompt is a recorded
   `system/message`, never a request-level field;
8. finalize the assistant turn (assistant message, tool calls, closing records).

Turn end reasons (record `turn/end {"turn", "reason"}`):

| reason | when |
| --- | --- |
| `{"kind": "completed"}` | model answered without tool calls |
| `{"kind": "max-steps"}` | `max_steps` exhausted |
| `{"kind": "max-tokens"}` | provider `Finish(kind="length")` |
| `{"kind": "aborted", "reason": {"kind": "user"}}` | `interrupt()` |
| `{"kind": "aborted", "reason": {"kind": "provider"}}` | provider `Finish(kind="aborted")` |
| `{"kind": "aborted", "reason": {"kind": "disposed"}}` | external task cancellation |
| `{"kind": "error", "error": {"message", "code"?}}` | provider failure / unexpected exception |

`request/header.reason`:

* `initial` — the session has no earlier `request/header`;
* `series` — the most recent `turn/end` was `completed` (or there is none);
* `resume` — the most recent turn ended any other way (error/aborted/max-*).

`request/context` is written only when provider, model, or `context_window` changed relative
to the previous `request/context` record.

## 3. Records written by the loop

| record | `data` | envelope |
| --- | --- | --- |
| `turn/start` | `{turn}` | — |
| `turn/end` | `{turn, reason}` | — |
| `step/start` / `step/end` | `{turn, step}` | — |
| `system/message` | `{turn, step, message}` (source `{"kind": "system-prompt"}`) | `surfaceOp="append"` |
| `user/message` | the wire `Message` directly (`role`, `content`, `id`, `source`) | `surfaceOp="append"` |
| `request/header` | `{turn, step, header: {config, adapterDefaults, tools}, reason}` | — |
| `request/context` | `{turn, step, provider, model, contextWindow}` | — |
| `assistant/message` | `{turn, step, message, usage?, stream: []}` | `surfaceOp="append"` |
| `assistant/attempt` | `{turn, step, stream: [usage chunk?, finish-error chunk]}` | — |
| `tool/call` | `{turn, step, callId, name, arguments}` (`arguments` is a JSON string) | — |
| `tool/result` | `{turn, step, message, meta?}` (`message.role="tool"`, `source.kind="tool"`) | `surfaceOp="append"`, `sourceEventSeqs=[assistant/message seq]` |
| `compaction/start`, `compaction/summary`, checkpoint `user/message`, `compaction/end` | section 8 | checkpoint: `surfaceOp={"op": "replace", "startSeq", "endSeq"}`, `sourceEventSeqs` |

`usage` on `assistant/message` is camelCase like the fixtures:
`{"inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens"}`.

The envelope is passed only when the session's `append` accepts `**kwargs`; the loop probes
this once with `inspect.signature`. A minimal `SessionLogLike` without envelope support still
works, it just records events without surface information.

## 4. Events and ordering guarantees

Event shapes are the frozen ones from INTERFACES §7:

```text
{"type": "session", "sessionId"}
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

* `session` is a frontend event: the CLI emits it once before the first `turn/start`; `AgentSession` never emits it, so JSON consumers see it exactly once.
* `status thinking` is emitted after the first-step preamble records and before compaction /
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
* Concrete sinks are a frontend concern and live in `deeppyro/headless.py` (INTERFACES
  section 7): `JsonLinesSink` writes one compact JSON object per line (`ensure_ascii=False`)
  and flushes after each event so a consumer can read a running turn; `RichSink` renders the
  same events for a terminal. The agent package only defines the `EventSink` protocol usage.

## 5. System prompt

`build_system_prompt(tools, ctx)` is deterministic and uses only its arguments:

1. identity — "You are DeepPyro, a coding agent running in a terminal." + one paragraph on
   the job;
2. `## Environment` — cwd, platform, model, session id, UTC start time, permission preset,
   approval policy, and at most 50 sorted `env` entries;
3. `## Project instructions` — numbered `### Instruction block N` sections, or an explicit
   "none found" line;
4. `## Tools` — one `### <name>` block per schema with the description and compact sorted
   JSON parameters; when there are no tools it says so;
5. `## Operating rules` — nine rules (inspect before change, targeted edits, shell usage,
   verify, never fabricate, follow project instructions, ask only when blocked, concise
   replies, adapt after tool failure) plus the "answer directly" closing line.

`collect_instructions(cwd)` walks `cwd` and its parents to the filesystem root, dedupes by
resolved path, reads `AGENTS.md` from root-most to deepest (deeper = later = refinement),
skips unreadable/empty files, truncates each file at `DEFAULT_MAX_INSTRUCTION_CHARS = 20000`
characters with `[instruction file truncated]`, and prefixes every block with
`Instructions from <absolute path>:`. `build_system_prompt` itself never touches the
filesystem.

`AgentSession` builds its `PromptContext` from the session header `createdAt` (fallback:
`now_ms()`), `sys.platform`, `collect_instructions(cwd)` plus
`AgentOptions.instructions`, and permission / approval from the options first, then the
gate attributes (`preset`, `approval`), then defaults `workspace-write` / `ask`.

## 6. Tool execution

Calls returned by one stream are grouped in provider order: consecutive `parallel_safe`
tools form one concurrent group (an unknown tool is treated as parallel-safe, matching the
tool pipeline's "unknown tool becomes an error result" contract), and every barrier tool
gets its own group. Per group the loop:

1. appends all `tool/call` records (then emits `tool_call` events) in call order;
2. executes the group through `deeppyro.tools.run_tool_batch(..., registry, ctx_factory)`
   (lazy import; a self-contained fallback with `Semaphore(8)` exists only for workstream
   isolation), with `ToolContext(cwd, session_id, turn, step, call_id, cancel=async Event,
   log=session.append, permission, services)`;
3. appends one `tool/result` record per call in request order (then emits `tool_result`),
   carrying `ToolResult.meta` when present.

So parallel groups log `call, call, result, result` while barrier tools log
`call, result, call, result` — the same shape as the fixtures. Unknown tools, malformed JSON
arguments, `PermissionDenied`, `ToolError`, and unexpected tool exceptions become
`ToolResult(is_error=True, text="Error: ...")`; only `CancelledError` propagates. The
assistant message is always appended before any `tool/call` record, so a `tool/result`
replacement/fold always has its origin on the surface.

## 7. Cancellation and failures

`interrupt()` sets the interrupt flag and the `ToolContext.cancel` event, cancels the task
running `send` (the turn runs in the caller's task), and waits up to 5 s for the turn to
record its abort. The resulting records are: a partial `assistant/message` when any
reasoning/text/tool-call content arrived, synthetic
`tool/result` records with `text="Error: interrupted"` for calls that were announced but not
finished, `step/end`, and `turn/end {"kind": "aborted", "reason": {"kind": "user"}}`.
`send` returns the partial assistant text; no `final`/error event is emitted; no exception
is raised.

Other failure paths:

* provider `Finish(kind="aborted")` → same as above but reason `provider`;
* provider `Finish(kind="error")` or a stream that ends without `Finish` → append a partial
  `assistant/message` when content exists, otherwise `assistant/attempt` with the failure
  chunk; emit `error`, `step/end`, `turn/end error`; re-raise `LlmError`;
* `Finish(kind="length")` → partial assistant message, `step/end`,
  `turn/end {"kind": "max-tokens"}`, `final` with the partial text;
* an unexpected exception follows the provider-error path (attempt/error/turn-end error) and
  is re-raised;
* external `CancelledError` (`send` cancelled from outside) records
  `{"kind": "aborted", "reason": {"kind": "disposed"}}` and re-raises.

`busy = True` for the whole turn including tool execution, and concurrent `send` calls raise
`SessionError`.

## 8. Compaction

### 8.1 Token accounting

`estimate_message_tokens` = `(len(text) + len(reasoning) + len(tool-call name) +
len(tool-call arguments)) / 3.5 + 4`; `CHARS_PER_TOKEN = 3.5`, `MESSAGE_OVERHEAD = 4.0`.
`should_compact(messages)` is `estimate_messages_tokens(messages) > threshold *
context_window` (`context_window <= 0` disables it).

### 8.2 Surface fold

The durable log is append-only; visible context is a *surface* of system/developer/user/
assistant messages and tool results. Each such record carries `surfaceOp`:

* `"append"` (or missing) pushes one node `(seq, type, data)`;
* `{"op": "replace", "startSeq", "endSeq"}` splices the node at the first replaced position.

`fold_surface` validates replacements exactly like the session writer: `startSeq/endSeq`
must be present on the surface and earlier than the event seq, `sourceEventSeqs` must be
non-empty, unique, all earlier, and include every shadowed node, a `system/message` head may
only be replaced by that same single system node (compaction never shadows node 0), and a
negative tool-call balance is rejected. Derivation: `user/message` yields `data` itself,
`tool/result` yields `data.message`, `system/message` / `developer/message` /
`assistant/message` yield `data.message` unless the content list is empty (then the node
exists but yields no message).

### 8.3 Region selection

`select_region(records, retain_tokens)` folds the surface, never shadows a head system
message, accumulates node token estimates from the tail until `retain_tokens` is reached,
moves the cut older while the cut would split an assistant tool-call from its result, and
returns the contiguous region `[first_index, keep_from-1]` with its shadowed seqs and
estimated tokens. It returns `None` when nothing safe can be shadowed (small conversation,
every span unbalanced) — `compact` then returns `False` without writing anything.

`entry_state(records)` recovers the open turn (`turn/start` without `turn/end`), the latest
`session/end-seed` seq, and an unmatched `compaction/start` (same `compactionId` with no
`compaction/end`). An unmatched start older than the latest end-seed is ignored; otherwise
it means another compaction is running and `compact` raises `SessionError`.

### 8.4 Summarization call

`compact` replays the surface *before* the region (`[system/message?, ...region messages]`)
and appends one final user message with the DSH `COMPACTION_INSTRUCTION` (structured Markdown
checkpoint, merge-prior-checkpoint rule). The request uses the loop model/provider, the tools
advertised by the latest `request/header`, `max_tokens` from the compactor, and the session
id. The stream is consumed for `TextDelta`; `Finish(kind="error")` → `LlmError`,
`aborted` → `AbortedError`, `length` → `LlmError("summarization truncated at the token cap
(incomplete checkpoint)")`, `content-filter` → `LlmError`, empty text → `LlmError`.
The checkpoint message is `role="user"` with the preamble + `<compacted-summary>` framing,
`source={"kind": "compact-checkpoint", "compactionId": ...}`, and a fresh message id.
Before committing, the framed checkpoint must estimate strictly smaller than the shadowed
region; otherwise `LlmError("summary is not smaller than the shadowed content ...")`.

### 8.5 The four-record transaction

A successful compaction appends, in order, with no yields between the commitments:

1. `compaction/start {"compactionId", "turn"?}` — `turn` only when a turn is open (auto
   compaction); it is the durable lock.
2. `compaction/summary {"compactionId", "summary": [text block], "rawOutput": [text block],
   "llmStreamCall": true, "shadowedRange": {"start", "end"}, "shadowedSeqs": [...],
   "shadowedTokenCount", "provider", "model", "maxTokens"?, "usage"?}`.
3. checkpoint `user/message` whose `data` is the checkpoint `Message`, envelope
   `surfaceOp={"op": "replace", "startSeq", "endSeq"}` and
   `sourceEventSeqs=[start seq, summary seq, ...shadowedSeqs]` — this is what removes the old
   span from the surface.
4. `compaction/end {"compactionId", "turn"?}`.

The surface must still contain the selected start/end after summarization (stability check).
After the transaction the compactor calls `session.derive_messages()` and verifies the
checkpoint id is present, raising `SessionError` otherwise. Any failure after the start
record is answered with a single `compaction/end` carrying
`error={message, name, code?}`, and the original exception is re-raised (the record write
failure itself is logged, never masking the original error).

### 8.6 Integration with the loop

Auto-compaction runs inside the open turn, once per step, after `status thinking` and before
deriving the request messages. Failures other than `CancelledError` are logged and stored on
`AgentSession.last_compaction_error`; the turn continues. A `Compactor` passed to
`AgentSession` overrides the default construction (`keep_recent_tokens=8000`,
`context_window=options.context_window`, `threshold=options.compact_threshold`).
`compact(session, messages)` ignores `messages` (kept for the frozen signature) and selects
the region from `session.records`, so seqs remain authoritative.

## 9. Known deviations / risks

1. **Streaming event order.** `text`/`thinking` deltas necessarily precede the
   `assistant/message` append. All record-mirroring events follow their record.
2. **No replay chunks.** Fixtures store the provider `stream` chunks on `assistant/message`;
   the loop records `stream: []` and only synthesizes chunks for `assistant/attempt` on a
   pre-content failure. Replay/debugging fidelity is therefore lower than DSH.
3. **No retry records.** `llm/retry` / `llm/retry-started` are produced by the retry
   wrapper in `deeppyro.llm`, not by the loop.
4. **Header reason heuristic.** The loop cannot see RPC resume semantics, so it infers
   `initial` / `series` / `resume` from the previous `turn/end`.
5. **request/context.** Written on the first step of a turn only when provider, model, or
   context window changed; DSH logs it in a narrower set of cases.
6. **Heuristic token estimate.** `len/3.5 + 4` per message is frozen by INTERFACES and is
   used both for pressure and for region sizing.
7. **Checkpoint shape.** The summary is stored as one text block (`rawOutput` mirrors it);
   DSH may retain multiple output blocks. `errorChain` is simplified to
   `{message, name, code?}`.
8. **No `sourceCommandId`.** Command/job integration (and therefore source-command ids on
   compaction records) is not part of this module; callers can extend the lifecycle dict
   later without breaking the format.
9. **Session dependency.** Envelopes require the real `Session.append(type, data, **envelope)`.
   The loop probes this; a reduced `SessionLogLike` cannot record surface ops and thus cannot
   support compaction. `Compactor` requires `session.records`.
10. **Additive options/kwargs.** `AgentOptions.model`, `permission_preset`,
    `approval_policy`, `instructions`, and `AgentSession.permission=None`,
    `services`, `compactor` are additions beyond INTERFACES §7; all have defaults, so the
    frozen constructor contract still holds.
11. **Prompt content is DeepPyro-specific** (not DSH's runtime prompt), and it never embeds
    host memory protocols.
12. **Tools fallback.** `_execute_calls_fallback` duplicates the tool pipeline contract for
    workstream isolation; it is only used when `deeppyro.tools.pipeline` is unavailable.

## 10. Test coverage (`uv run pytest tests/agent -q`)

38 tests, all green: prompt 7, loop 15, compaction 11, plus sink-free loop assumptions.
(Sink behavior itself is covered by `tests/tui/test_headless.py`.)

* `tests/agent/fakes.py` — `FakeSessionLog` (append with seq + envelope, surface-fold
  `derive_messages`, `records_of`/`seq_for`), `ScriptedProvider` (scripted stream turns,
  pause events for interrupt tests, recorded requests), `FakeTool`/`FakeRegistry`,
  `RecordingSink` (records `len(session.records)` at emit time to prove append-before-emit),
  `FakePermission`. The fake session implements the same fold/validation rules as
  `compact.surface`, so compaction tests cannot pass by accident.
* prompt: environment/model/tool/instruction rendering, determinism, AGENTS.md root-first
  order, truncation, unreadable/empty files skipped.
* loop: exact simple-turn record and event sequences; header reasons; no duplicated system
  message / request context on later turns; parallel and barrier tool ordering; error and
  unknown tools; interrupt mid-stream; busy guard; max-steps; provider error →
  `assistant/attempt` + `turn/end error`; reasoning parts; length finish; auto-compaction
  inside the open turn; tolerated auto-compaction failure.
* compaction: token estimator math and threshold; the full 4-record transaction and its data
  shapes; checkpoint envelope/sourceEventSeqs/derived fold; tool-schema replay in the
  summarization request; no-safe-range no-op; second compaction shadowing the prior
  checkpoint; failure closing with error; unmatched-start lock; oversized/empty/truncated
  summaries; missing `records` → `SessionError`.

Lint: `uv run ruff check src/deeppyro/agent src/deeppyro/prompt src/deeppyro/compact tests/agent`
and `uv run ruff format --check ...` are clean (line-length 110).
