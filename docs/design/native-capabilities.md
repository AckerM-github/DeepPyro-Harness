# Native capabilities port — implementation design (0.3.0)

Status: **implemented (0.3.0)**. This was the implementation design for the 0.3.0 hard
cut (target repository state: D:/code/deeppyro at `7d03ef6` plus the provider-port
working tree). The native capabilities are now the shipped path; plan-time references
to flags (`--legacy-v4`, `DEEPPYRO_TUI_NATIVE`), v4 seams and `dpyro migrate` describe
intermediate steps that no longer exist. Body retained as the design record.

Hard constraints honoured throughout:

1. Everything is a plugin. New behaviour is contributed by plugin services over
   the kernel, or by registered tools/projections; core code gains semantics only
   where the session schema itself is the contract.
2. Pythonic design. Frozen dataclasses, Protocol, StrEnum, TaskGroup,
   asyncio-based composition. No v4-to-native object graphs copied from the wire
   era, no TypeScript-shaped records.
3. AI-native. A session log records facts of the conversation and of durable
   workflow state. Token counts, latency, retries, cost and prompt hashes stay in
   the trace (docs/native/schema.md section 8.5; docs/native/coverage.md L71-83).
   Every fact must be replayable from the log; every derived view must be
   reconstructible from the log plus trace.

Inputs already landed or landing in the working tree (treated as available):

- Provider port (KI-010): src/deeppyro/core/llm.py (StopReason L40, Usage L51,
  ToolSpec L68, ModelRequest L90, stream events L109-158, NativeToolCall L169,
  ModelResponse L182, LlmProvider L196), src/deeppyro/agent/model_call.py
  (complete_model_call L55), src/deeppyro/llm/native_provider.py,
  llm/native_messages.py, llm/attachments.py, llm/http.py. tools/pipeline.py
  remains on the v4 Tool contract for now.
- Native loop and session: src/deeppyro/agent/loop.py (RunResult L112-127,
  __init__ L151-208, run L254, _drive L424, _build_plan L492, _model_call L498,
  _dispatch_tool_calls L641, _tool_context L725, message factories
  _user_message L824, _assistant_message L835, _tool_message L855, event
  appenders L871-1056), src/deeppyro/session/schema.py (EventKind L103-135,
  SessionHeader L141-218, Event L221-254, NativeSession L407 create L446 open
  L492 append L505 close L559, sessions_path_for L1252, validate_event_data
  L655), src/deeppyro/session/fold.py (fold L607, SessionView L139,
  _compaction L374-417, _replace_message L346), src/deeppyro/session/native_index.py,
  src/deeppyro/agent/native_host.py (NativeHeaderView L82, NativeSessionHandle
  L214, NativeAgentHost L434, send L499, interrupt L533, aclose L547, busy L589,
  usage L598).
- Native session maintenance (parallel wave, working tree): session/native_fork.py
  (fork_native_session, NativeForkError codes NATIVE_FORK_*; whole-session copy,
  header origin="fork", parent_id, depth+1), session/native_query.py; the
  projection registry's working-tree docstring (projection/registry.py L22-48)
  already records why the v4 LogRecord notify path cannot consume native events
  (resolved by 6.4).
- Baseline and drift: line numbers refer to 7d03ef6 plus the working tree as
  observed while this design was written. Parallel waves (provider, session,
  tools) are moving these files; before implementing a step verify the current
  symbol names (grep the symbol, not only the line), and treat this document's
  module/API contracts as authoritative over its line numbers.

- Docs: docs/design/native-provider-port.md, docs/design/native-host.md,
  docs/native/schema.md (compaction.done L177, goal.changed L184, reserved M4
  events L194-203, run.* trace rules L206, source kinds L298-299, compaction fold
  rules L321-322), docs/adr/0001-three-track-native-agent.md (D2/D3/D4/D7/D8),
  docs/release/0.2.0-known-issues.md (KI-001, KI-010, KI-011, KI-015, KI-016,
  KI-017, KI-037), docs/native/coverage.md (gaps L21-35).

Definition of done: every capability below reads and writes only schema-1 facts,
native traces and plugin sidecars; src/deeppyro/core/wire.py,
session/{session,events,derive,fork,reader,writer,store,metadata}.py,
agent/{session_state,session_turn,session_tools,session_records,session_support,
session_stream,session_prompt,inbox,native_provider}.py, compact/{surface,tokens}.py,
core/protocols.py, core/payloads.py and the v4 llm converters are deleted, and
tests/integration/test_wire_boundary.py has an empty allowlist.

---

## 0. Current state per capability

| Capability | Current v4 binding (verified) | Native target |
| --- | --- | --- |
| compaction | compact/surface.py L86/227/313 fold v4 LogRecord surface; compact/tokens.py v4 Message estimators; compact/compactor.py L94 reads session.records, writes compaction/start, compaction/summary, user/message checkpoint (surfaceOp replace), compaction/end; compact/compress_tool.py L234 same transaction | fold(SessionView) + EventKind.COMPACTION_DONE; ModelRequest/complete_model_call; native RefStore over message ids |
| goal service | goal/service.py L68 _records_of reads session.records/session.log; goal/replay.py L355 folds v4 goal/change records and advances rounds via user/message goal source L295 | EventKind.GOAL_CHANGED snapshots read through fold().state.goal; delete goal/replay.py |
| goal round driver | goal/round_driver.py L63-79 v4 record strings, L82-120 DriverSession/DriverAgent over v4 Inbox, L547 pre-step fence, L668-784 event-bus handlers | native agent handle + status listeners; no inbox; no pre-step hook |
| team | team/manager.py L90 _Member / L112 _MemberSink / L147 TeamService; L536 _create_child_session (v4 SessionHeader + write_new_session), L618 _create_agent (v4 AgentSession), L651 _run (agent.send), L1037 _fold_team_records | NativeSession children + NativeAgentHost; plugin.team.* facts; trace usage |
| subagent | subagent/manager.py L98 SubagentService; L557 child, L580 v4 fork, L661 _create_agent, L727 _record_subagent (v4 subagent/catalog+descriptor), L764 _notify_parent (v4 followup) | NativeSession children + native fork; plugin.subagent.* facts; notices; trace usage |
| prompt projection | prompt/projection.py L25 LogRecord/fold_surface; L127 SystemPromptProjection, L194 RuntimeContextProjection | pure SessionView fold; system.message last-wins; message.replaced for runtime context |
| projection registry | projection/registry.py L33 LogRecord/SessionHeader; L273 notify(session, record), L315 checkpoint; projection/checkpoint.py L32 sidecar over session.path | native Event/SessionHeader/NativeSession; sidecar unchanged version 1 |
| task tools | builtin/task_tools.py dual today: native branch L267, _append L565, v4 helpers L211-256, v4 branches L272-281/L572 | native only; delete v4 branch lines; task payload builders move into agent/task.py |
| eval/replay | tests/native/replay.py L21-29 imports v4 core.protocols provider types; eval/live.py L36 imports AgentLoop, L496 _default_provider_factory v4 LlmProvider | ReplayProvider emits core.llm events; live runner uses native provider factory |
| native host | agent/native_host.py L434 NativeAgentHost takes v4 Tool (L49), no prompt projection, no goal-source, no status broadcast, no send-and-await | extended host is the single per-session native agent handle |

---

## 1. Shared native conversion layer (the one-adapter rule)

Every capability that turns runtime state into session facts must go through the
same three helpers. Ad-hoc copies of loop message builders are the main
duplication risk in the v4 tree (compact/compactor.py L257,
subagent/manager.py L418, team/manager.py L802, eval/live.py L320 each rebuilt
pieces of it).

### 1.1 New module: src/deeppyro/agent/turn_messages.py

Wire-free; imports only core.messages, core.llm, core.ids, and (until phase T)
core.protocols.ToolResult. The loop stops owning message construction
(agent/loop.py L824/L835/L855 keep thin wrappers or are deleted).

    def user_message(text, *, source=None) -> Message
    def assistant_message(response) -> Message
        # Role assistant: ReasoningPart (if response.reasoning),
        # TextPart (if response.text), ToolCallPart per NativeToolCall in order;
        # source=ModelSource(provider, model); created_at=now_ms(); new_message_id().
    def tool_message(call_id, result) -> Message
        # Role tool, ToolSource(call_id), tool_call_id=call_id,
        # text = result.text or " "; converts result.parts image entries into
        # ImagePart(attachment, media_type) via image_part_from_wire().
    def image_part_from_wire(part) -> ImagePart
        # accepts {"type":"image",...} and {"attachment":..., "media_type":...};
        # mirrors llm/attachments.image_part_url L33 so provider and session agree.
    def tool_specs(tools) -> tuple[ToolSpec, ...]
        # moves agent/context_plan.py _tool_spec L44 here; context_plan imports it
    def notice_message(text, *, plugin, form=None) -> Message
        # NativeSessionHandle._drain_notices (native_host.py L313) uses this so
        # injected notices and team/subagent notifications share one shape:
        # PluginSource(plugin=...), TextPart(text).

Rationale: the loop, compaction writer, team/subagent hosts and eval all need
the same mappings, and assistant_message is the only place that knows
ModelResponse -> session message. The module lives in agent/ to avoid a cycle:
core.llm must not import agent; core.messages must not import core.llm.

### 1.2 Token accounting and context plans: src/deeppyro/agent/context_plan.py

Already native (constants L20-23, estimate_text_tokens L26,
estimate_message_tokens L31, _tool_spec L44, ContextPlan L61,
build_context_plan L99). This module is the only token estimator - delete
compact/tokens.py (CHARS_PER_TOKEN=3.5, MESSAGE_OVERHEAD=4.0) instead of
reimplementing. Consequences are behavioural (section 9).

Add two public helpers:

    def message_tokens(messages) -> int              # sum of estimate_message_tokens
    def plan_tokens(plan) -> int                     # plan.estimated_tokens alias

Compaction calls estimate_message_tokens for each covered message and for the
framed summary, exactly as the v4 compactor did at
compact/compactor.py L144-150, so the "summary must be smaller" rule survives.

### 1.3 Native tool contract: new src/deeppyro/tools/contract.py (phase T)

The capability work needs a stable tool call context. The v4 contract
(src/deeppyro/core/protocols.py: ToolSchema L28, ToolResult L126 with
parts: list[dict] wire images, ToolContext L139 with session_id:str,
agent/conclude_turn, Tool L161) cannot survive the v4 deletion, and 58 modules
import core.protocols today. Phase T introduces a native home and moves imports
mechanically:

    # tools/contract.py — no core.wire import
    class Tool(Protocol):
        name: str; description: str; parameters: dict
        parallel_safe: bool; output_schema: dict | None
        async def run(self, args, ctx) -> ToolResult

    @dataclass(frozen=True)
    class ToolResult:
        text: str = ""
        is_error: bool = False
        meta: dict | None = None
        parts: tuple[ImagePart, ...] = ()      # native image parts, not wire dicts
        error: dict | None = None              # {"name": str, "code": str}

    @dataclass(frozen=True)
    class ToolContext:
        cwd: Path
        session: NativeSession | None          # set by AgentLoop._tool_context
        session_id: str
        turn: int
        step: int
        call_id: str
        cancel: asyncio.Event
        permission: Any
        services: Mapping[str, Any]
        # native envelope for tools that append facts themselves (compress, task):
        run_id: str | None = None
        turn_id: str | None = None
        step_id: str | None = None
        trace_id: str | None = None

AgentLoop._tool_context (loop.py L725-748) fills session/envelope, and
turn_messages.tool_message consumes the native parts. Tools that still hand-build
wire image dicts (read_image/memory/screenshot paths) are converted in phase T;
keeping list[dict] is rejected because it re-introduces a wire-shaped adapter in
the model path.

Import sites to swap (phase T checklist): tools/{pipeline,registry,permissions}.py,
tools/builtin/*.py (24 files), memory/tools.py, mcp/{bridge,resources}.py,
ptc/{runtime,executor,tool,sdk}.py, plugins/template.py, agent/events.py,
agent/context_plan.py, agent/native_host.py, builtin/task_tools.py. Each
capability below assumes ctx.session and ctx.services are available.

### 1.4 One writer, one reader

- Writer: only NativeSession.append (schema.py L505) writes facts. Capabilities
  never create records; plugin events use the plugin namespace
  (PLUGIN_EVENT_PATTERN schema.py L56, fold collects them at fold.py L219-232).
- Reader: every capability reads SessionView from fold(session.header,
  session.events) (fold.py L607). No capability keeps a second fold of raw events
  (the v4 tree had three: compact/surface L86, goal/replay L355, team/manager
  L1037).

### 1.5 Shared child-session factory: new src/deeppyro/agent/children.py

Team and subagent duplicate child creation, permission derivation, usage
measurement and agent construction today (team/manager.py L536/L597/L618/L802;
subagent/manager.py L557/L580/L645/L661/L418). One helper used by both:

    @dataclass(frozen=True)
    class ChildSession:
        session: NativeSession
        inherited_event_count: int
        mode: str                     # "spawn" | "fork"

    def create_child_session(sessions, *, parent, preset, origin,
                             delegation_depth, session_id=None) -> ChildSession
    def create_fork_child(sessions, *, parent, preset, boundary_seq=None) -> ChildSession
    def child_permission(parent, cwd, sandbox, session) -> Any
    def child_tools(registry) -> ToolRegistry   # filter_child_tools body moves here

Fixed header values: NativeSession.create(path=sessions_path_for(child_id,
sessions.native_root), cwd, preset=preset, session_id=child_id,
parent_id=parent.header.id, origin="subagent" (or "fork"), delegation_depth=
parent.header.delegation_depth + 1). Header has no model/system/preset_state:
those belong to runtime configuration, never to facts.

Fork support: session/fork.py is v4-only (SessionForkError L56, fork_session L64,
build_fork_seed L117) and is deleted. session/native_fork.py already lands in the
working tree:

    def fork_native_session(session_id, *, root=None, new_id=None,
                            created_at=None) -> NativeSession
        # reads the source fully, refuses an existing target, writes header
        # origin="fork", parent_id=source.header.id, depth+1, copies all events
        # preserving type/data/envelope ids (seq renumbered by the append path,
        # ADR D7), shifts ts by the created_at delta, returns the session open.

P1 adds one keyword for the subagent prefix case: through_seq: int | None = None
(copy events with seq <= through_seq only; return the copied count). The subagent
fork boundary is children.fork_boundary(parent.events) = last turn.ended seq,
else the parent's last seq, so an open parent turn is never copied; this is the
v4 build_fork_seed semantic. children.create_fork_child wraps the function, so
lock/exists/corrupt error behaviour (NATIVE_FORK_EXISTS, NATIVE_FORK_CORRUPT,
NATIVE_FORK_LOCKED) stays in one place.

### 1.6 Deleted in phases 1-2

agent/session_support.py, agent/session_records.py, agent/session_prompt.py
(v4), agent/session_stream.py, agent/session_state.py, agent/session_turn.py,
agent/session_tools.py, agent/inbox.py, agent/native_provider.py (KI-010 seam),
compact/{tokens,surface}.py, core/payloads.py. The wire boundary test
(tests/integration/test_wire_boundary.py L26-39 footprint, L43-55 allowlist)
must shrink per phase; the allowlist is the regression gate for the whole port.

### 1.7 Fact placement rules (AI-native invariants)

| Fact | Home |
| --- | --- |
| conversation messages, system prompt, runtime context | user/assistant/tool/system/message.replaced events |
| compaction boundary + summary | compaction.done |
| goal snapshot + rounds_started | goal.changed |
| task state and handoff | task.state, task.handoff, task.handoff.result |
| team roster / queued messages / team task board | plugin.team.* events |
| subagent lifecycle | plugin.subagent.* events on the parent session |
| per-call tokens, latency, retries, cost, prompt hash | trace ModelCallRecord (observability/trace.py L240; required fields L82-100) |
| tool/run lifecycle, budgets | session run.* / turn.* events and trace records |
| compaction block summaries, masks, refs, projection cells | sidecars next to the session file (deletable caches) |

## 2. Compaction

Goal: auto-compaction and the compress/search/decompress tools run on native
sessions without the v4 surface (compact/surface.py, compact/tokens.py) and
without v4 records (compaction/start, compaction/summary, user/message checkpoint
with surfaceOp, compaction/end).

### 2.1 Module layout

Keep, ported to native types:

- compact/prompts.py unchanged (CHECKPOINT_PREAMBLE L9, SUMMARY_OPEN_TAG L16,
  SUMMARY_CLOSE_TAG L17, COMPACTION_INSTRUCTION L73, frame_summary L76). The
  frame and instruction are model-visible contract; keep them byte-identical so
  recorded replay fixtures and eval expectations stay valid.
- compact/blocks.py: BlockStore, BlockRecord (L67), CompactError (L41). File
  layout stays .jsonl blocks + .refs.jsonl per session; v4 record ids in block
  metadata are replaced by message-id based fields (2.4).
- compact/refs.py: RefStore and range resolution, re-based on message ids (2.4).
- compact/compress_tool.py, compact/block_tools.py: tools, re-based on the
  native Tool contract (phase T) and native session.
- compact/compactor.py: rewritten in place as the native auto-compactor (2.2).

Delete: compact/surface.py and compact/tokens.py. The surface fold is replaced
by SessionView.messages from session/fold.py L607; the token estimators by
agent/context_plan.py L26-31 (section 1.2). compact/__init__.py __all__ shrinks
accordingly.

### 2.2 Native auto-compactor (compact/compactor.py)

    @dataclass(frozen=True)
    class CompactionRegion:
        start_index: int                 # index into SessionView.messages
        end_index: int                   # inclusive
        message_ids: tuple[str, ...]
        message_tokens: int

    class Compactor:
        def __init__(self, *, provider: LlmProvider, model: str,
                     keep_recent_tokens: int = 8000,
                     context_window: int = 32768,
                     threshold: float = 0.85,
                     max_tokens: int | None = None,
                     trace_store=None) -> None
        def should_compact(self, view: SessionView) -> bool
        async def compact(self, session: NativeSession, *,
                          view: SessionView | None = None,
                          tools: Sequence[ToolSpec] = (),
                          envelope: dict | None = None) -> CompactionDone | None

should_compact (replaces compact/compactor.py L87): fold view.messages through
context_plan.estimate_messages_tokens and compare with
threshold * context_window. SessionView.messages excludes the system prompt
(fold.py stores it separately, L238-240), matching v4 behaviour where the
surface head system message was not part of derive_messages. Document as 9.3.

Region selection (replaces surface.py select_region L227 and its helpers
tool_pairing_cuts L145, balanced_before/after L180/186):

1. Pin the prefix: find the last message whose source is CompactionSource and
   start candidates after it. This is the native replacement of the v4 rule that
   a summary cannot itself be covered (fold.py L374-417 rejects a summary id in
   covers and rejects already-covered ids). Keeping the pinned summary at the
   front also keeps the coverage ranges contiguous under repeated compaction.
2. Walk newest to oldest accumulating estimate_message_tokens until the
   accumulated tail reaches keep_recent_tokens. That index is keep_from.
3. Extend keep_from left until the cut is tool-pair safe: the cut between
   messages[i-1] and messages[i] is safe when messages[i].role != "tool" and
   every ToolCallPart seen before the cut has a tool result before the cut.
   Compute unmatched call ids in one forward pass; this reproduces
   balanced_before/balanced_after without the v4 surface nodes.
4. Region = messages[first:keep_from]. Return None when empty.

compact():

1. view = view or fold(session.header, session.events).
2. region = select_region(...); None -> return None.
3. compaction_id = str(uuid4()).
4. Summarize with the native provider (replaces _summarize L199-254):
   ModelRequest(provider=self.provider.name, model=self.model,
   messages=(*view.messages[:region.start_index], instruction),
   system=view.system, tools=tuple(tools), max_tokens=self.max_tokens,
   temperature=None, context_window=None, session_id=session.header.id).
   view.system is passed separately because native fold keeps the system message
   out of messages; v4 folded it into the prefix as a surface node.
   instruction = Message(role="user", content=(TextPart(COMPACTION_INSTRUCTION),),
   source=PluginSource("compact"), ...) - the v4 source kind
   "condensation-instruction" does not exist in the native source set
   (core/messages.py L102-178); it is never persisted, so the only effect is
   Message validation.
5. complete_model_call(provider, request). StopReason handling keeps the v4
   error strings: ERROR -> LlmError(f"summarization failed: ..."),
   ABORTED -> AbortedError("summarization was aborted"),
   LENGTH -> LlmError("summarization truncated at the token cap (incomplete
   checkpoint)"), CONTENT_FILTER -> LlmError("summarization was blocked by a
   content filter"), TOOL_CALLS -> LlmError, empty text -> LlmError(
   "summarization produced no text summary content").
6. summary_message = Message(role="user", content=parts-from-frame_summary(rendered),
   source=CompactionSource(compaction_id), id=new_message_id()). This is exactly
   the message schema.py compaction.done L722-738 requires (role user,
   CompactionSource with matching id).
7. Size guard (replaces compactor.py L144-150): when
   estimate_message_tokens(summary_message) >= region.message_tokens, raise
   LlmError with the same message shape ("summary is not smaller than the
   shadowed content (...)"). Nothing has been appended yet, so the session is
   untouched.
8. session.append(EventKind.COMPACTION_DONE,
   {"compaction_id": compaction_id,
    "covers": list(region.message_ids),
    "message": summary_message.to_dict()}, **envelope) where envelope carries
   run_id/turn_id/trace_id when compaction ran inside the loop (the event has no
   required envelope, schema.py L1052-1102, but carrying it associates the fact
   with its run for replay/join). This single append is the whole transaction:
   no start/end bracket, no partial states, and fold applies it atomically
   (fold.py L374-417 inserts the summary at the region start).
9. Trace: append a ModelCallRecord (observability/trace.py L240, required fields
   L82-100) with the enclosing run/turn/step ids, new span_id, provider, model,
   prompt_hash of the summary ModelRequest payload, input/output tokens from
   response.usage, latency_ms, finish_reason from StopReason, retry_count,
   ts. Tokens stay out of the session log (schema section 8.5). BudgetTracker is
   NOT charged (v4 did not charge compaction either); this is stated in 9.1.
10. Store the raw summary in BlockStore as a block with source="auto" so
    search_context/decompress keep working across 0.3. If the block write fails,
    log and continue: the session fact is authoritative, the sidecar is a cache.
11. Verify: re-fold and assert summary_message.id is present
    (replaces _verify_checkpoint L270-279). A missing summary raises SessionError
    after the append, which is a bug signal, not a fallback path.

Failure policy: any exception other than CancelledError is caught by the loop
(2.3) and never fails the turn, matching v4
agent/session_state.py / session_turn.py L458-469 ("step compaction failed ...
continuing the turn"). The exception is stored on the loop as
_last_compaction_error and logged.

### 2.3 Trigger point: agent/loop.py

- __init__ (loop.py L151-208) gains keyword-only compactor: Compactor | None =
  None and keeps no v4 SessionState. The compactor is built by whoever wires the
  loop, because only the wiring knows the provider and the compact config table:
  headless (headless.py _run_native_async L1001-1046, next to the AgentLoop
  construction at L1033), NativeAgentHost (native_host.py L448-497), the eval
  live runner (eval/live.py L320-347), and the future TUI runtime.
- _drive: after _build_plan (loop.py L455) insert:

      plan = self._build_plan()
      if self._compactor is not None and self._compactor.should_compact(
              fold(self._session.header, self._session.events)):
          await self._maybe_compact(plan)
          plan = self._build_plan()        # messages changed; rebuild

  _maybe_compact is a small method next to _build_plan (L499):
  try: await self._compactor.compact(self._session, tools=plan.tools,
  envelope=self._envelope(run/turn/step/trace)); except CancelledError: raise;
  except Exception as exc: self._last_compaction_error = exc; logger.warning(...).
  v4 compaction sat inside session_turn.py L277 before _build_request; here it
  runs between checkpoint/step.started and the model call, so a resumed run never
  replays a compaction request (the checkpoint already recorded the step).
- No other loop change is required: the next _build_plan folds the new summary
  into view.messages automatically.
- RunResult is unchanged; compaction is visible only through the session event,
  the trace records, and the context-token counter that follows naturally.

### 2.4 Refs, compress tool, block tools on native sessions

Refs (compact/refs.py) become message-id addressed:

    @dataclass(frozen=True)
    class RefEntry:
        ref: str                 # "m00042"
        message_id: str          # msg_<16 hex>
        assigned_at: int

    class RefStore:
        load(session_id) / entries() / offsets() / rollback()
        register(mapping)                         # message_id -> ref
        ensure(view) -> tuple[RefEntry, ...]      # assign refs to new messages

canonical_ref/ref_ordinal stay (L31/L54, REF_WIDTH=5). addressable_messages
(L248) becomes addressable_messages(view: SessionView, refs: RefStore) returning
[(ref, Message)] ordered by position in view.messages; resolve_refs (L266)
resolves ref -> Message -> index in view.messages; ref_ordinal is used for
ordering/adjacency checks. The refs.jsonl schema changes (message_id instead of
messageId+seq); it is a sidecar cache, so no session-schema migration is needed
and the file is rebuilt on demand (ensure(view) is idempotent and deterministic:
existing entries in file order, new messages in view order).

Why refs stay out of the prompt: no prompt/projection/agent code renders refs to
the model today (only compact/* reads RefStore). Native port keeps that property:
refs are a tool-side addressability layer, and the model sees message text only.
Do not add ref prefixes to Message text - that would pollute facts and break
byte-identical replay of recorded requests.

CompressTool (compact/compress_tool.py L186, run L211, _compress L234):

- Session source: ctx.session from the native ToolContext (section 1.3); the
  fallback sessions store lookup (_resolve_session L571) becomes
  sessions.open_native(session_id), used only when a tool is invoked outside a
  loop (tests, headless export). Do not fall back silently when both are absent:
  CompactError(COMPACT_SESSION_UNAVAILABLE) with the same message text.
- Validation (_validate L354) works on view.messages indices instead of seqs:
  COMPACT_INVALID_RANGE (reversed), COMPACT_REF_NOT_ON_SURFACE (ref resolves to a
  message that is no longer in view.messages - i.e. already compacted; keep the
  legacy message text "refs point at already-compressed messages" for CLI
  compatibility), out-of-order blocks, COMPACT_OVERLAP, COMPACT_ADJACENT_RANGES
  (same adjacency rule on indices), COMPACT_EMPTY_RANGE. COMPACT_SYSTEM_PROMPT
  disappears: the system prompt is not a message in native sessions (fold.py
  L238-240), so nothing can cover it. That is a deliberate 0.3 change (section 9)
  and lets block 1 start at the first message.
- Tool-span safety (_tool_spans L444, _check_tool_spans L494) is re-expressed as
  the same cut-safety check as select_region step 3, over indices: a block must
  not start on a tool result whose call is before the block, and must not end on
  an assistant message with tool calls whose results are after the block. Same
  COMPACT_TOOL_SPAN error strings.
- Everything-compressed (_everything_compressed L531) uses pinned-summary plus
  remaining-message rules: refuse when the visible region contains only the
  pinned summary.
- Write path (_compress L234): validate everything first; snapshot refs offsets;
  per block: blocks.add(...) -> BlockRecord; then append one COMPACTION_DONE per
  block (same shape as 2.2 step 8, covers = that block's message ids, message =
  frame_summary(body) with CompactionSource(compaction_id)). One compaction_id
  per compress invocation is allowed by schema (schema.py only requires the
  source id to equal the event's id). Order: append blocks oldest-first; coverage
  stays contiguous because blocks are disjoint and each range's internal order
  is preserved when earlier ranges are removed by fold.
  Partial failure: the first append either happened or not; there is no
  compaction/start to close. If append N fails, blocks 1..N-1 are already facts
  in the log; return the existing COMPACT_PARTIAL_WRITE ToolResult with the ids
  of committed events (record ids -> message ids) and roll refs back only for
  uncommitted blocks. This removes the v4 _close_failed bracket (L342) entirely.
- Result meta {compactionId, blocks:[{id, range:{start,end} now refs+message ids,
  startSeq/endSeq renamed start_index/end_index}, savedTokens]} and result text
  formatting stay as L324-340 so the TUI/CLI message does not change.
- BlockTools (compact/block_tools.py L211/L263) only import changes: format_local
  stays in refs.py; no v4 types are used.

Auto-compaction block metadata: BlockRecord gains source: Literal["auto","compress"]
and compaction_id, start_ref/end_ref (empty for auto), start_message_id/
end_message_id. Search/decompress keep working for both.

### 2.5 Session facts for compaction

Only EventKind.COMPACTION_DONE is written. The full fact is
{compaction_id, covers (message ids, contiguous at fold time), message
(user-role framed summary, source CompactionSource)}. No token counts, no
provider/model, no rawOutput, no shadowed seqs, no surface ops, no
compaction/start or compaction/end. Token/latency/retry of the summary call live
in the trace; the raw summary lives in the blocks sidecar. A session can be
replayed without the sidecars; decompress/search are best-effort caches.

### 2.6 Tests and wire-boundary effect

- New tests/native/test_compaction_native.py: selection on a synthetic
  SessionView (tool-pair boundary, pinned summary, keep_recent_tokens), summary
  compaction.done shape, size guard, failure leaves log untouched, second
  compaction covers only messages after the pinned summary.
- tests/compact/test_compress_tool.py and test_refs.py rewritten to native
  sessions; test_blocks.py/test_block_tools.py only import swaps; support.py
  builds NativeSession fixtures (helper in tests/native/support or existing).
- Differential: the synthetic compaction pair described in
  docs/native/coverage.md L57 (v4 surface replace range vs native covers; summary
  text after stripping preamble/tags) moves to the native fixture set; the
  adapter (tests/native/synthetic.py) maps v4 surfaceOp replace to compaction.done
  for comparison.
- Wire boundary allowlist: after this phase remove compact/{surface,tokens}.py,
  core/payloads.py, session/{derive,events,writer}.py entries from
  tests/integration/test_wire_boundary.py L26-55.

---

## 3. Team native sessions

Scope: src/deeppyro/team/manager.py (TeamService L147), src/deeppyro/team/types.py,
src/deeppyro/builtin/team.py (TeamPlugin L15), tools/builtin/team.py (team_tools
L373). Public API (spawn L248, send L318, wait L346, cancel L359, close L383,
restore L398, task board L877-1008) and the tool surface stay unchanged; only the
session/agent/usage internals are replaced.

### 3.1 Session facts (parent session)

Every team fact is a plugin event on the PARENT session. EventKind stays closed
(schema.py L103-135); a capability adds no core event types.

    # payloads, snake_case, validated by a small decoder in team/manager.py
    plugin.team.member.changed   {member_id, name, description, provider, model,
                                  preset, session_id, phase, created_at,
                                  last_reply?, error?}
    plugin.team.message.queued   {member_id, text, created_at}
    plugin.team.message.delivered {member_id, text, created_at}

phase is a StrEnum TeamPhase {SPAWNING, IDLE, RUNNING, COMPLETED, FAILED,
CANCELLED} replacing the v4 string constants. Member ids keep the v4 shape
("member-<hex>"): they are plugin-local identifiers, not session ids.

Why these are session facts: the roster, the queue of prompts that have not run
yet, and whether a member failed must survive a crash and be replayable. Token
usage is NOT a session fact (section 9.4): plugin.team.usage (v4 _record_usage
L819) is deleted, and _fold_team_usage (L1084) with it.

The team task board stops using v4 team/task records and writes
EventKind.TASK_STATE on the parent session, using the native payload builders
that move into agent/task.py (section 7). Task ids therefore become
task_<16 hex> instead of task-<12 hex>; TaskInfo in team/types.py gets a
validating constructor. The fold's SessionView.tasks (fold.py L139) becomes the
board's source of truth; TeamService keeps a read cache keyed by last_seq.
Delegation is unchanged: TeamService writes its own tasks, TaskService writes
the task-tool tasks, both in the same event set; neither mutates the other's
task ids.

### 3.2 Member lifecycle

spawn (L248) keeps validation (max_members, unique name, bounded parent, cwd) and
then:

1. preset_id = resolved preset for the member (presets service).
2. child = children.create_child_session(sessions, parent=parent_session,
   preset=preset_id, origin="subagent", delegation_depth=parent.header.
   delegation_depth + 1). Replaces _create_child_session (L536) and its v4
   SessionHeader/write_new_session/flat_session_path path. The child header is
   schema-1 SessionHeader (schema.py L141-218); created_at=now_ms(), cwd=member
   cwd, preset=preset_id, parent_id=parent.header.id. session id = child session
   id; the member id stays independent so a member can be renamed/removed without
   touching the child log.
3. member = _Member(id=..., session_id=child.session.id, ...) with
   session: NativeSession, host: NativeAgentHost | None, baseline: RunUsage.
4. append plugin.team.member.changed phase=SPAWNING.
5. host = agents.open_native_child(session=child.session, provider=...,
   model=..., preset=preset_id, tools=children.child_tools(team child set),
   permission=children.child_permission(parent, cwd, sandbox, child.session),
   cwd=member cwd, usage_sink=parent_usage_sink(source=f"team:{member_id}"),
   trace_store=parent trace store). This replaces _create_agent (L618) and its
   v4 AgentsPlugin.create/AgentSession arguments (builtin/agents.py L119-184).
   The new factory owns exactly the v4 responsibilities: preset resolve and
   materialize, spec.model fallback, prompt build, tool registry resolve/filter
   (_registry_for L196, _filter_registry L251), runtime context, provider
   native_provider lookup (llm/registry.py L185 in the provider-port tree) and
   context window default (v4 _DEFAULT_CONTEXT_WINDOW L54, keep 32768 until the
   model registry carries context windows).
6. append delivered/queued facts as the run proceeds and start
   asyncio.create_task(self._run(member, prompt, initial=True)) as today
   (L276).

_run (L651): replace "reply = await member.agent.send(text)" (inside _send
L684) with "result = await member.host.run_turn(text)". The manager records
plugin.team.message.delivered before the call, runs, then maps the RunResult:

- phase COMPLETED on the first successful run, IDLE on later ones (unchanged
  semantics), last_reply = result.final_text.
- status FAILED / aborted: phase FAILED with error from the RunResult status
  (CANCELLED -> phase CANCELLED when the task was cancelled, preserving L702
  cancellation behaviour).
- exceptions: phase FAILED, member.error = f"{type(exc).__name__}: {exc}"
  (verbatim v4 shape).
finally: task=None, rest.set(), drain queue. No _MemberSink (L112) and no stream
event passthrough: the child writes its own session facts, and telemetry reaches
the UI through the child's UsageSink only.

send (L318) keeps the queued/started contract: queued appends
plugin.team.message.queued and appends to member.queue; started lazily opens the
host (restore case) and starts _run. cancel (L359) cancels the asyncio task and
calls host.interrupt() (native_host.py L533) - the native equivalent of
agent.interrupt(). close (L383) cancels tasks, gathers, and closes every host
(L523 _close_agent becomes host.aclose()).

### 3.3 Native agent handle (dependency on section 6 and phase P1)

The manager must be able to send a prompt and await the resulting run on a child
session, interrupt it, read usage, and read messages. NativeAgentHost already
owns the loop and session but its public send() (native_host.py L499) schedules a
task and returns None; add:

    async def run_turn(self, text: str) -> RunResult   # drains notices, awaits loop.run
    async def wait(self) -> RunResult                  # await the in-flight run
    def run_state(self) -> AgentRunState               # StrEnum idle/running

send() keeps its scheduling behaviour for the TUI (TUI migration is KI-001), but
its task is now the run_turn task. run_turn must be re-entrancy guarded (busy ->
TeamService's queue, not an error) and must translate RunCancelledError the same
way the loop does (loop.py run L308-315). If a run is resumed after a crash,
loop.run(resume=True) (loop.py run L291) repairs the unfinished turn
(_repair_unfinished_turn L382); no v4 _close_open_turn (team/manager.py L501)
is needed because repair is a native loop invariant, not a manager concern.

### 3.4 Restore after restart

restore (L398) becomes:

1. if self._restored: return roster (unchanged).
2. view = fold(parent.header, parent.events).
3. members: iterate view.plugin_events in seq order, group per member_id by
   member.changed (last wins), and collect queued/delivered pairs in order; a
   queued entry is dropped when a delivered entry with the same text follows it
   before another queued entry (same matching rule as v4
   _fold_team_records L1037, now over plugin events instead of records).
4. phases: SPAWNING/RUNNING restore to IDLE (v4 L429); FAILED/CANCELLED stay.
5. member.session = NativeSession.open(sessions_path_for(session_id,
   sessions.native_root)); failure sets member.error and phase FAILED with the
   v4 message "cannot restore team member session: ..." (L447).
6. baseline usage: RunUsage.from_session(child_session) reading the last
   run.started run_id from the child events, then observability usage_from_trace
   (3.5). Missing trace -> RunUsage.zero() plus usage_known=False; the UI shows
   it as unknown rather than inventing numbers (AI-native).
7. _restore_runs (L452) is deleted: MemberRun history is reconstructed from the
   child sessions' run.* facts plus trace joins when a panel needs it; the team
   service no longer persists a separate run list.
8. tasks: board from view.tasks (native task.state), not _fold_team_records.
9. queues: group queued texts, then _resume_member (L460) for members with a
   non-empty queue: phase RUNNING and create the run task, exactly L460-473.

### 3.5 Usage and latency

Delete _measure_usage (L802), _member_records (L784), _record_usage (L819),
_report_usage (L856), _fold_team_usage (L1084). Replace with one shared helper
(moved from subagent/types.py usage_from_records L163 to a provider-free module):

    # src/deeppyro/observability/usage.py (new) - trace-backed only
    @dataclass(frozen=True)
    class RunUsage:
        provider: str; model: str
        input_tokens: int = 0; output_tokens: int = 0; cached_tokens: int = 0
        reasoning_tokens: int = 0; retries: int = 0; steps: int = 0
        latency_ms: int = 0; usage_known: bool = True
        total_tokens property; merge(other); to_dict/from_dict
    def usage_from_trace(trace_store, run_id, *, provider, model,
                         latency_ms, steps) -> RunUsage
        # TraceStore.read(run_id) (observability/trace.py L472), sum
        # ModelCallRecord input/output/cached/reasoning/retry_count; steps from
        # RunResult; latency measured by the caller (perf_counter around
        # run_turn, as team/manager.py L684 does today).

SubagentUsage (subagent/types.py L58) becomes an alias of RunUsage so the
supervisor (subagent/supervisor.py L158/L229) and WorkerOutcome need no change;
subagent.types.usage_from_records L163 and sum_usages L221 are deleted. Live
reads prefer host.session_handle.usage() deltas (native_host.py L598) and use
traces only after restart, so no extra trace read is needed in the hot path when
the trace store is absent.

### 3.6 Notifications and status

Team members do not notify the parent agent (unchanged from v4: only subagent
notifications existed). Completion becomes visible to the UI through member.changed
facts and the TeamService roster; a parent-visible notice would be a product
change and is not part of the port. The goal driver only manages the root agent
(section 5), so no agent-status bus is needed for team.

### 3.7 File work list

- team/manager.py: rewrite _Member (L90), delete _MemberSink (L112), rewrite
  _member_payload (L119) to snake_case plugin payload + decoder, rewrite restore
  (L398-535) and usage (L784-875), _create_child_session (L536), _child_permission
  (L597) -> children.child_permission, _create_agent (L618), _run/_send
  (L651-700), _record_task (L963) -> task.state, _fold_team_records (L1037),
  _fold_team_usage (L1084).
- team/types.py: TeamPhase StrEnum; TaskInfo id validation; MemberRun removed or
  reconstructed.
- builtin/agents.py: add open_native_child(...) (or a new
  builtin/agents_native.py service) next to the v4 create() which disappears
  in P9; keep preset/filtering responsibilities.
- agent/children.py: new (section 1.5).
- builtin/team.py: unchanged except wiring the new managers.
- tools/builtin/team.py: unchanged API; TaskInfo construction adjusted.
- tests/team/{test_team_service,test_cold_resume,test_usage,test_task_board}.py
  rewritten on NativeSession fixtures; helpers.py builds a parent session with
  child logs under a tmp native root and a fake LlmProvider; test_usage asserts
  RunUsage from a fake TraceStore, no session token fields.

---

## 4. Subagent native sessions and supervisor

Scope: src/deeppyro/subagent/manager.py (SubagentService L98), subagent/types.py,
subagent/supervisor.py, src/deeppyro/builtin/subagent.py (SubagentPlugin L15),
tools/builtin/subagent.py (subagent_tools L153).

The service keeps its API (start L230, run L200, wait_children L491,
cancel_all L502, close L525, usage L386) and the SubagentResult/SubagentActivation
shapes. Only sessions, records, usage and notifications change; the supervisor
(section 4.4) is almost untouched.

### 4.1 Session facts and lifecycle events

Parent session records a subagent lifecycle as plugin events (the M4 names
reserved in docs/native/schema.md L194-203 are not EventKind members, so do not
extend the core set for a capability):

    plugin.subagent.started  {child_session_id, label?, provider, model,
                              mode ("spawn"|"fork"), inherited_event_count,
                              created_at}
    plugin.subagent.finished {child_session_id, status, is_error, error?,
                              text?, usage_known}

Child session is created with children.create_child_session for spawn
(replaces _create_child_session L557) and children.create_fork_child for fork
(replaces _create_fork_session L580): origin="fork" for fork, "subagent" for
spawn, parent_id=parent.header.id, delegation_depth+1, preset=preset_id,
cwd=parent cwd. Fork reuses session/native_fork.fork_native_session with
through_seq = children.fork_boundary(parent.events) (last parent turn.ended seq,
so the mid-tool-call parent turn is excluded); the helper renumbers seq while
keeping message ids and data, sets header origin="fork"/parent_id/depth+1 and
returns the open session. inherited_event_count = number of copied events, as
today (L289 baseline logic). No v4 build_fork_seed / session/fork.py import
remains.

_start (L253) changes only in that steps 3-5 of section 3.2 are reused:
provider resolution via the llm service native_provider() factory (login does not
need a provider override in spawn mode; still rejected in fork mode, L270),
usage baseline is RunUsage.zero() for spawn and RunUsage from the inherited
window for fork (from trace, section 3.5), _create_agent L661 becomes
agents.open_native_child (section 3.2 step 5) with
children.child_tools(registry, disabled=CHILD_DISABLED_TOOLS L? subagent manager
filter_child_tools L78 becomes a thin call into children.child_tools and stays
exported because builtin/subagent.py imports it).

_activate (L330): "reply = await agent.send(prompt)" L342 becomes
"result = await member_host.run_turn(prompt)"; SubagentResult.from_result(...)
builds text=result.final_text, is_error from result.status, error from the
RunResult reason/status mapping. Cancellation (L346-356) maps CANCELLED to
cancelled=True; any other failure keeps the exact f"{type(exc).__name__}: {exc}"
text. _close_agent L? becomes host.aclose(). _ChildSink (L91) is deleted: the
child writes its own session and telemetry; there is no v4 stream event to
mirror.

### 4.2 Usage

Delete USAGE_RECORD_TYPE (types.py L15), _measure_usage (L418), _publish_usage
(L434), _publish_usage_fallback (L472), usage_from_records (types.py L163),
sum_usages (L221) and _child_records (L401). Replace with observability/usage.py
RunUsage (section 3.5): live usage = host.session_handle.usage() delta from the
per-child baseline; after restart = usage_from_trace on the child's last
run.started run_id. Publish through the existing UsageSink.record_turn chain with
TurnUsage.source=f"subagent:{child_id}" instead of the ad-hoc record(provider,...)
Protocol (types.py L147); delete that Protocol and its imports. SubagentUsage
becomes an alias of RunUsage so SubagentResult.usage, supervisor WorkerOutcome
and SupervisorResult keep their shapes.

### 4.3 Parent notification

_notify_parent (L764) used agent.followup with source kind "subagent-settled".
Native: NativeSessionHandle.inject(text, source=PluginSource("subagent")) queues
the notice into the bounded deque (native_host.py L295-311); the parent's next
run drains it (_drain_notices L313) and appends user.message facts before the
turn. There is deliberately no mid-turn inbox in the native loop. If the parent
is running, the notice waits for the next prompt (documented 0.3 change, 9.5);
the finished event already makes the fact durable in the session, so nothing is
lost. Notices keep the v4 wording ("Subagent <label> finished: ...") so prompt
fixtures stay comparable.

### 4.4 Supervisor

subagent/supervisor.py has no v4 imports: Worker Protocol L292, Supervisor L333,
run L401, _run_rounds L428, per-worker budgets WorkerBudget L72 /
_BudgetAccount L305, convergence L485-504, stall L505-517, round limit L518,
_run_worker L560 and SubagentServiceWorker L631 all stay. Only the
SubagentUsage import in WorkerOutcome/SupervisorResult and the
SubagentServiceWorker result mapping (L649-664: make result.usage a RunUsage
alias) change. The supervisor remains a pure orchestration plugin; it never
touches sessions, so it works unchanged once the service returns native results.

### 4.5 File work list and tests

- subagent/manager.py: L78 filter_child_tools -> children; L91 delete _ChildSink;
  L253-327 _start on children.create_* / agents.open_native_child; L330-385
  _activate on host.run_turn + RunUsage; L386 usage; L401-486 delete
  usage-from-records; L557/L580 delete; L645/L661 children helpers; L727
  _record_subagent -> plugin.subagent.started payload; L764 _notify_parent ->
  handle.inject.
- subagent/types.py: keep limits/activation/result; SubagentUsage=RunUsage;
  delete USAGE_RECORD_TYPE/usage_from_records/sum_usages/UsageSink.
- tests/subagent/*: test_manager, test_fork, test_usage, test_handoff,
  test_plugin, test_tool, test_supervisor rewritten against a FakeNativeHost and
  a fake TraceStore; test_fork asserts copied events (renumbered seq, same
  message ids, inherited count) and that the child fold matches the parent fold
  window.

---

## 5. Goal round driver on the native loop

Scope: src/deeppyro/goal/round_driver.py (932 lines), goal/service.py,
goal/replay.py, builtin/goal.py, builtin/goal_round_driver.py, goal/types.py.

The whole v4 signal surface of the driver (agent/inbox events, agent/pre-step
waterfall, agent/status, v4 record types next-step/next-turn/turn-end/user
message) disappears with the v4 agent stack. The native driver is therefore a
rewrite around the native host, not a mechanical port. The user-visible
contract in docs/spec/goal.md is preserved: one goal round = one full AgentLoop
run whose user message renders render_goal_round_prompt(goal, round)
(L122-138, keep byte-for-byte, tests assert it), rounds are capped by
max_goal_rounds (default 256, DEFAULT_MAX_GOAL_ROUNDS), and the failure codes
'round-limit' (L77), 'queue-failed' (L78), 'prompt-rejected' (L79) stay in
BlockedReason codes.

### 5.1 Facts: rounds advance through goal.changed

Native schema validates goal.changed as exactly {goal, rounds_started}
(schema.py L776-781) and fold is last-wins (fold.py L297-303). There is no
user-message round admission rule, so goal/replay.py (goal/replay.py fold L355,
_admit_round L295, SnapshotChange/ClearedChange L50/L70) is deleted. Instead:

- GoalService.state(session) = view = fold(header, events); snapshot from
  view.state.goal, rounds from view.state.rounds_started; cache keyed by
  view.last_seq. _records_of (L68) and the RecordFn seam (L47) are replaced by a
  NativeSession-aware append(kind, data) seam typed
  Callable[[EventKind, dict], Any].
- GoalService._commit (L390-412) appends EventKind.GOAL_CHANGED with
  {"goal": snapshot.to_dict()|None, "rounds_started": n}. create (L238), edit
  (L267), pause (L295), resume (L298), complete (L318), block (L321), clear
  (L332) keep identical validation and publish GOAL_BUS_EVENT="goal/changed"
  (L? _publish) for the driver and TUI.
- New GoalService.admit_round(goal: GoalRef, round: int) -> GoalSnapshot: the
  only writer that advances rounds_started. It re-reads the fold, verifies the
  goal is active, same id, round == rounds_started + 1, round <= max_goal_rounds,
  then appends goal.changed with the same snapshot (revision unchanged; the
  event seq orders facts) and rounds_started=round, updates the cache, and
  returns the pre-round snapshot. Concurrent callers get GOAL_INVALID_TRANSITION;
  the driver retries once on conflict.

### 5.2 Driver design (src/deeppyro/goal/native_driver.py)

A single asyncio task per root agent session; no inbox, no pre-step fence.

    @dataclass RoundState: attempt_round: int; running: bool; stopping: bool
    class NativeRoundDriver:
        def __init__(self, *, goals, agents, settings): ...
        def attach(self, session_id) -> Disposer     # or adopt via agents.on_native_host
        async def drive(self) -> None

drive():
1. If stopping/not attached -> return. If the host is running (host.run_state()
   == RUNNING) -> return: a user turn wins over an automatic round.
2. view = fold; if the last turn is unfinished (last event is turn.started or an
   open step) -> return; resume is a user decision (unlike v4
   ready_after_checkpoint L401 which drove itself).
3. snapshot = goals.state(session); activation = goals.activation(session)
   (process-local, default "disarmed", service.py L217). If snapshot is None or
   phase != active or activation != "armed" -> return.
4. If snapshot.rounds_started >= snapshot.max_goal_rounds: goals.block(round-
   limit BlockedReason) and return.
5. round = snapshot.rounds_started + 1; pre = goals.admit_round(ref, round);
   prompt = render_goal_round_prompt(pre, round).
6. run = await host.run_turn(prompt, source=GoalSource(goal_id, revision,
   round)). AgentLoop.run gains a source kwarg (section 6.3) so the user.message
   fact carries GoalSource, exactly the v4 source.kind=="goal" fact; round 0
   (the user's original objective) still uses UserSource, so
   _is_round_zero's semantics (L920) become source is GoalSource and round==0
   never occurs.
7. Map the result: CANCELLED with user abort -> disarm and stop; CANCELLED
   because a goal.changed event set stopping -> stop without blocking;
   FAILED with a budget reason -> goals.block(code from
   BudgetExceeded.turn_reason: max_steps/max_tokens/max_wall_seconds); other
   failure -> goals.disarm() and stop (v4 _on_agent_error L693 equivalent).
   SUCCEEDED -> loop again from step 1 (the next round re-reads the goal; if the
   model completed/paused/blocked it, the loop stops).

Note the semantic change: v4 admitted a queued permission before the model step
and could reject it (prompt-rejected) - native sends the run and then the run is
interrupted if the goal changes (goals._publish -> driver._on_goal_changed
equivalent). A "prompt-rejected" code remains reserved but is no longer produced
by the native driver; only queue-failed/round-limit are. The driver is a
listener, not a gate: it subscribes GOAL_BUS_EVENT from the kernel event bus
(builtin/goal_round_driver.py already injects hooks; L83-102 wiring stays) and
calls host.interrupt() when the goal becomes paused/blocked/complete while a
round is running.

Budgets: each round is a fresh AgentLoop.run with its own BudgetLimits
(agent/budget.py; configured by agent options max_steps/max_tokens/
max_wall_seconds). Rounds do not share a token budget; max_goal_rounds is the
only cross-round cap. This matches v4 behavior (each turn had its own tracker)
and is documented as such. Trace: every round is a normal run with run_id,
trace_id, model_call/tool_call records; rounds are joined to the goal via the
GoalSource of the first user message (replay/eval can select rounds).

### 5.3 Drivers, plugins, restore

- builtin/goal_round_driver.py: keep GoalRoundDriverPlugin L77, plugin_id L80,
  inject ["hooks","agents"], _service_config L26/_merged_config L41/
  _driver_enabled L63 unchanged; setup L90 constructs NativeRoundDriver(hooks=...,
  agents=..., goals=ctx.get(GoalService)), attaches on native host creation
  (agents.on_native_host(callback) replacing v4 on_created L99 in
  builtin/agents.py), and ctx.effect(detach) L102.
- builtin/goal.py: GoalPlugin L68/GoalService L77 keep prompt sections L139-140
  and GOAL_POLICY L21-29; _load L97-107 becomes NativeSession.open + fold (no v4
  store); the prompt section renderer reads goal state from the service.
- Restore after restart: rounds_started is in the fold, so the driver knows which
  round is next. Activation is process-local (by design, service.py L217), so a
  restarted session is disarmed until the UI/agent arms the goal; the driver
  never auto-runs on startup. A round whose turn started but never ended is left
  to the normal native resume path (loop.run(resume=True)), not auto-driven.
- No new event kinds and no new session fields: rounds are ordinary
  turn.started/turn.ended/step.* events plus goal.changed snapshots. The
  v4 NEXT_STEP/NEXT_TURN/TURN_END/USER_MESSAGE constants (L65-68) and all inbox
  helpers L142-277 are deleted.

### 5.4 File work list and tests

- goal/round_driver.py: delete entirely (or reduce to render_goal_round_prompt +
  codes if imported); goal/native_driver.py new (RoundState, NativeRoundDriver).
- goal/service.py: native fold reads/writes, admit_round, remove
  _records_of/RecordFn/replay imports, keep activation/state/view/create/edit/
  pause/resume/complete/block/clear/publish.
- goal/replay.py: delete (its fold is schema.fold + last-wins goal.changed).
- goal/types.py: GoalSnapshot.to_dict must emit schema field names
  (snake_case, max_goal_rounds); GoalError codes unchanged.
- agent/loop.py: run(prompt, *, source: MessageSource|None) - one new keyword.
- agent/native_host.py: run_turn/wait/run_state (section 3.3).
- builtin/goal_round_driver.py: construct NativeRoundDriver.
- tests/goal/: delete test_fence.py and test_goal_replay.py; rewrite
  test_round_driver.py/test_driver_integration.py against FakeNativeHost
  (records run_turn prompts, returns scripted RunResults); add
  test_native_driver.py cases: exact round prompt text, rounds_started advances
  via goal.changed only, round-limit block, pause->interrupt, restart reads
  rounds from fold, GoalSource on round messages, budget failure -> block.

---

## 6. Prompt projection and projection registry (native slice)

### 6.1 What is useful vs what can be dropped

prompt/projection.py is a pure fold that keeps the system prompt and the runtime
context snapshot stable in a session. That capability is still required: the
native loop currently never writes EventKind.SYSTEM_MESSAGE, so native runs have
view.system = None and no system prompt at all. The useful parts are:

- SystemPromptProjection.project (L155): render the prompt, compare with the
  session's current system message, write only on change.
- RuntimeContextProjection (L194): one runtime-context message that is refreshed
  in place (retained L213, refresh L217, project L239).
- The message constructors create_system_message (L71) and
  create_runtime_context_message (L85); RetainedSnapshot (L61) as the "what is
  currently in the transcript" view.

Dropped with v4: the seq-based replace list in ProjectionUpdate.replace
(L41: list[(seq, text)]), fold_surface/system_nodes (L151), the
in_history/starts_series replay heuristics in project (L155-191), and every
core.wire import (L25). Native has no surfaceOps: a system message is
last-wins in the fold (fold.py L238-240) and a user message is updated with
EventKind.MESSAGE_REPLACED {message_id, message} (schema.py L715-721), where
fold._replace_message (fold.py L346-372) only allows content/is_error to change
while role, created_at, source and tool_call_id stay identical.

### 6.2 prompt/projection.py rewrite

    # native-only, imports core.messages + session.fold only
    SOURCE = "runtime-context"                    # PluginSource(plugin=SOURCE)
    @dataclass(frozen=True)
    class ProjectionUpdate:                       # replaces L41
        kind: Literal["append", "replace"]
        message: Message
        replaced_id: str | None = None            # message.replaced target
    def system_message(text: str) -> Message      # SystemSource(); empty -> content=[]
    def runtime_context_message(text: str, *, sections) -> Message
        # PluginSource(SOURCE), TextPart(text), sections also kept as
        # plugin event? No: plugin events are not message content.
    def project_system(rendered: str, view: SessionView) -> Message | None
        # None when the last system.message text equals rendered; otherwise a
        # new Message ready to append. Appending is the update: the fold keeps
        # the latest system.message, so no replacement is needed and history of
        # prompt changes stays auditable.
    def project_runtime_context(rendered: str, view: SessionView,
                                *, sections=None) -> ProjectionUpdate | None
        # current = last user.message whose source is PluginSource(SOURCE)
        # None -> append; equal text -> None; else replace with the same id,
        # created_at, source and role (fold guarantees legality).
    def runtime_context_sections(view) -> list[dict]  # re-export adapter

The v4 SystemPromptProjection/RuntimeContextProjection classes become these four
pure functions (no records; the loop owns the writes). The
create_runtime_context_message payload stays a single TextPart; structured
sections are rendered into text by prompt/runtime_context.py
(runtime_context_sections L28) before projection, exactly as v4 did
(agent/session_prompt.py L83-91 render_runtime_context), which keeps the
transcript human-readable and replayable.

### 6.3 Loop and host wiring

AgentLoop gains two optional keyword renderers (Pythonic callables, not plugin
classes, because they are per-run policy):

    prompt_renderer: Callable[[Sequence[ToolSpec]], str] | None
    runtime_context_renderer: Callable[[], Sequence[dict]] | None

In _drive (loop.py L428-497), after step.started and before _build_plan
(L455): if prompt_renderer is set, rendered = prompt_renderer(plan.tools); if
project_system(rendered, current_view) returns a message, append
SYSTEM_MESSAGE {"message": message.to_dict()} (no envelope, schema.py L1052-1102
allows it at session level) and re-fold. If runtime_context_renderer is set,
apply project_runtime_context: append USER_MESSAGE {"message": ...} or append
MESSAGE_REPLACED {"message_id", "message"} (same envelope rules), then re-fold.
Order: system first, then runtime context, then the model request. This is the
only place in the native runtime that writes a system message, so there is a
single writer and a single reader (sections 1.4 and 6.4). The renderer
implementation lives in a new prompt/context.py:

    def choose_model(...) / build_prompt_context(view, *, cwd, model, permission,
        preset, tool_names) -> PromptContext
    def render_system(prompt_service, tools) -> str
        # PromptPlugin.build(tools: Sequence[ToolSpec], ctx, scope, revision,
        # include) - prompt/prompt.py SectionRenderer L27 and
        # build_system_prompt L211 change from ToolSchema to ToolSpec
        # (core.llm.ToolSpec, context_plan._tool_spec L44 already produces it).

NativeAgentHost gets the two renderers (or a ready-made PromptRenderer) as
constructor kwargs and forwards them to every AgentLoop it builds; headless.py
_run_native_async (L1001) and eval/live.py LiveAgentRunner (L331) build the same
renderer from the prompt plugin. TUI: NativeAgentHost is constructed in the TUI
wave with the plugin registry, so the TUI needs no separate prompt path.

### 6.4 Projection registry native slice

projection/registry.py stays (third-party plugins may register projections; "all
facts are plugin events"), retyped at the boundary only. The working tree already
carries a blocker note (projection/registry.py L22-48): notify() requires a v4
wire LogRecord and _advance reads session.records; the resolution here is to
retype the boundary to Event/NativeSession rather than write a lossy adapter
(no invented surfaceOp/startSeq ranges).

- ProjectionDefinition (L53): init(header: session.schema.SessionHeader,
  inherited_event_count: int) -> S; apply(state, event: session.schema.Event) ->
  S; wire/summary unchanged.
- ProjectionHandle.state_of(session: NativeSession), ProjectionRegistry.notify
  (session: NativeSession, event) L273, snapshot (L224) and checkpoint (L315)
  unchanged in behaviour: advance cells by seq, self-heal by folding events with
  seq > last, identity gate suppress, listeners.
- Session observation: NativeSession has no observer hook today. Add
  NativeSession.on_append(callback) -> disposer (session/schema.py, after the
  successful append/fsync in append L505-557); every append path (loop, host,
  goal service, team service, task tools) becomes visible to the registry with
  no per-caller wiring. ProjectionsService.attach (builtin/projections.py
  L256) uses it instead of the v4 session.add_append_observer; detach L267 and
  save L273 stay. checkpoint.py is already session.path-based (sidecar
  .projections.json, SIDECAR_VERSION 1 L33; native NativeSession.path L418) so
  it only loses its v4 imports.
- State versions: bump ProjectionDefinition.state_version for every definition
  whose payload changes (todos/agents/jobs); old v4 sidecars are then ignored
  and rebuilt, which is the documented cache semantics (sidecars are deletable
  caches).

Definitions (builtin/projections.py L186-217) after the port:

- todos: read SessionView.state.todos (already core facts via EventKind.TODO_
  WRITTEN, fold.py). Keep the projection only for its wire view; no v4
  'todo/write' branch (_todos_apply L66).
- plan mode: SessionView.state.plan_mode; delete _plan_apply L178.
- agents: fed by the new plugin event namespace only -
  plugin.team.member.changed, plugin.subagent.started/finished plus child
  session lifecycle; delete the v4 'team/member','subagent/descriptor',
  'subagent/catalog' branches (_agents_apply L121).
- jobs: plugin.jobs.* events (the jobs capability owns them; rename its v4
  'job/registered'/'job/settled' writes in the jobs wave, _jobs_apply L78).
- Delete builtin/activity.py's duplicate record scan (refresh L145-250): TUI
  panels read ProjectionsService.snapshot() plus fold state. TUI touch points:
  tui/runtime.py L94/100/116/122, tui/runtime_session.py L107/111,
  tui/app_status.py L75-88, screens team.py L27 / plan.py L21 / jobs.py.

### 6.5 Files and tests

- prompt/projection.py: rewrite as 6.2; delete core.wire/fold_surface imports.
- prompt/context.py: new PromptContext builder + render_system (replaces v4
  agent/session_prompt.py L23-45 and L53-116).
- prompt/prompt.py: SectionRenderer/PromptContext (L27/L31) and
  build_system_prompt L211 take Sequence[ToolSpec]; prompt_service.build
  (L92-124) signature follows.
- agent/loop.py: two renderer kwargs, fact writes in _drive, run(source=).
- agent/native_host.py: renderer kwargs forwarded.
- projection/registry.py, projection/checkpoint.py: retype boundaries.
- builtin/projections.py: native definitions; builtin/activity.py: delete.
- tests/prompt/test_projection.py and tests/projection/* (test_registry,
  test_checkpoint, test_activity_parity, activity_defs, test_fork) rewritten on
  native sessions; new assertions: exactly one live system.message after N
  steps with unchanged prompt, a new system.message only when the rendered prompt
  changes, runtime context replaced in place (same message id, same
  created_at/source), projection sidecar rebuild when state_version changes.

---

## 7. Task tools: remove the last v4 branches

builtin/task_tools.py is already dual-format (native events + v4 ignorable
records) and is the model for the other ports. Once v4 is gone, delete every v4
branch, keeping exactly one payload shape: the schema-1 shapes validated by
schema.py L819-825.

Delete:
- _v4_task_payload L211, _v4_handoff_payload L215, _v4_handoff_result_payload
  L238, _record_payload L250 (its v4 branch is the only reason it exists; the
  native branch is identity).
- _facts_of L259: keep the native branch only (iterate
  session.events, map via NATIVE_TASK_EVENTS L65/_NATIVE_RECORD_TYPES L70);
  delete the session.records branch L272-281.
- TaskService._append L565: keep session.append(NATIVE_TASK_EVENTS[record_type],
  _native_payload(...)), delete the v4 append(..., ignorable=True) branch L572.
- _agent_and_session L596: keep ctx.services["session"] / native path, delete
  the v4 ctx.agent.session fallback L597-598.
- _task_owner L625: drop the session_id fallback L630-635; a task always has an
  explicit owner or the native session id from the session key.
- _usage_of L380 and _session_usage L348: read NativeSessionHandle.usage()
  (native_host.py L598) or RunUsage (observability/usage.py), never v4 usage
  dicts; _budget_violation L365 keeps max_tokens/max_steps/max_seconds checks
  (max_tokens compares RunUsage.total_tokens, max_seconds latency_ms/1000).
- _context_summary L323 already has a native fold branch: keep it, delete the
  derive_messages branch; _handoff_turn L638 reads run/turn ids from the native
  envelope (run_id/turn_id from the session's last turn or the enclosing
  ToolContext extension, section 1.3).
- Docstring/comment references to "v4 session" and the
  TASK_RECORD_VERSION L? ignorable extension note.

Move the payload builders out of the tool module so other capabilities can
write the same facts without importing a tool implementation:
agent/task.py (which already owns TaskStatus L43, Transitions L59, TaskBudget
L155, Task L191, Handoff L320) gains:

    def task_event_data(task: Task) -> dict          # from _native_task_payload L130
    def handoff_event_data(handoff: Handoff) -> dict # from L141
    def handoff_result_event_data(...) -> dict       # from L181
    def task_from_fact(fact) / handoff_from_fact(fact)
    TASK_EVENTS: Mapping[str, EventKind] = { ... }  # NATIVE_TASK_EVENTS L65

builtin/task_tools.py then imports those, and team/manager.py (section 3.1)
uses the same builders for its board. _native_handoff_payload deliberately
drops the v4 usage snapshot (L141-180 comment: "token facts out of the log
schema 8.5") - keep that: usage stays in trace.

Tests: tests/native/test_task_events.py and test_task_handoff_native.py stay
green with v4 fixtures removed; tests/team/test_task_board.py moves to
task.state facts; add one assertion that no tool module writes any v4 record
type after phase P9 (grep-style unit check over builtin/task_tools.py).

---

## 8. Ordered phases, files, tests, dependencies

P0 - provider wave (landed, uncommitted in the working tree; dependency for
everything): core/llm.py (StopReason L40, Usage L51, ToolSpec L68, ModelRequest
L90, stream events L109-158, NativeToolCall L169, ModelResponse L182,
LlmProvider L196), agent/model_call.py (complete_model_call L55), llm/
native_provider.py, llm/native_messages.py, llm/attachments.py, llm/http.py;
agent/loop.py already on the native protocol; agent/context_plan.py already has
the one token counter/estimator and ToolSpec converter (L20-56, L99). Gate:
native loop imports no core.wire; tests/native/test_loop.py, test_context_plan.py,
test_budget.py, test_checkpoint_resume.py, test_headless_native.py,
test_differential.py pass without running the full suite.

P1 - native session/agent plumbing (critical path; waves S and T land together):

Wave-label map used by sections 1-7: S = the session/agent plumbing bullets
below; T = the native tool-contract bullets below; H = host/UI wiring, whose
runtime part is in P1 and whose TUI switch is in P9.
- session/schema.py: NativeSession.on_append(callback)->disposer (append L505);
- agent/native_host.py: run_turn/wait/run_state, on_status listeners,
  prompt_renderer/runtime_context_renderer kwargs;
- agent/children.py: create_child_session/create_fork_child,
  child_permission, child_tools (sections 1.5, 3.2);
- session/native_fork.py (landed untracked): add through_seq prefix support and
  fork_boundary reuse from children.create_fork_child (4.1); session/
  native_query.py already covers native listing/reading;
- builtin/agents.py: open_native_child, on_native_host registration,
  live_hosts; keep preset resolve/filtering semantics from create L119-184;
- builtin/sessions.py: native_root/create_native/open_native/list_native over
  paths.sessions_dir (removes the v4 SessionStore dependency);
- goal/service.py: native fold reads/writes and admit_round (5.1);
- projection/registry.py + checkpoint.py: Event/SessionHeader typing (6.4);
- wave T (native tool contract, section 1.3): new tools/contract.py with Tool/
  ToolContext/ToolResult over core.messages parts and a ToolContext carrying
  session/run/turn/step ids, tools/{pipeline,registry,permissions}.py retyped,
  ToolResult.parts -> ImagePart conversion in agent/turn_messages.py,
  builtin tools that use ToolResult.parts (if any remain) migrated; ToolContext
  gains services["session"] (the NativeSession) so compress/task/team tools stop
  resolving the session by id.
Depends on P0. Tests: tests/native/test_native_host_parity.py new,
extend tests/session/test_native_fork.py (untracked) with through_seq/depth/
inherited-count cases, test_checkpoint_resume.py.

P2 - prompt projection + host wiring (depends P1): prompt/projection.py
rewrite, prompt/context.py new, prompt/prompt.py + builtin/prompt_service.py
ToolSpec signatures, agent/loop.py renderer kwargs and run(source=),
agent/native_host.py forwarding, headless.py _run_native_async L1001 and
eval/live.py L331 build the renderer. Tests: tests/prompt/* rewritten;
differential prompt check: for recorded fixtures the assembled system text and
runtime-context text equal the v4 rendering (v4 session_prompt L23-116 is the
reference until deleted).

P3 - compaction (depends P0+P1+P2 for system prompt/summary framing):
section 2 of this document; new agent/turn_messages.py converts assistant/
tool/thinking/image messages; compact/* rewritten on fold(SessionView) and
context_plan token helpers; builtin/compress.py exposes the NativeSession to the
tool through the native ToolContext. Tests: tests/compact/* rewritten
(test_refs, test_blocks, test_block_tools, test_compress_tool) plus a native
differential row compaction (tests/native/synthetic.py) - session has exactly
one summary message with CompactionSource; no token fields in the log.

P4 - goal round driver + goal service native (depends P1; uses P2 renderer
only for the system prompt): section 5; goal/native_driver.py new,
goal/replay.py and goal/round_driver.py deleted, builtin/goal.py load native,
builtin/goal_round_driver.py wiring, loop run(source=). Tests: tests/goal/*
rewritten (delete test_fence.py, test_goal_replay.py).

P5 - team native (depends P1 children/host, observability/usage.py, and P6 for
the agents projection if the team panel is tested): section 3. Tests:
tests/team/* rewritten; test_cold_resume kills and reopens the parent/child
sessions; test_usage uses a fake TraceStore and asserts zero usage records in
the session log.

P6 - subagent native + supervisor (depends P1, observability/usage.py; shares
children.py with P5): section 4. Supervisor change is one import alias. Tests:
tests/subagent/* rewritten; tests/subagent/test_fork.py asserts renumbered seq /
preserved message ids.

P7 - task tools cleanup (depends P1; handoff path depends P6): section 7.
Tests: tests/native/test_task_events.py, test_task_handoff_native.py;
tests/team/test_task_board.py.

P8 - eval/replay provider swap (depends P0 only, parallel to P2-P7):
tests/native/replay.py imports core.llm (Finish/ModelRequest/StreamEvent/
TextDelta/ToolCallArgsDelta/ToolCallStart/Usage) instead of core.protocols at
L21-29; ReplayProvider L286 implements core.llm.LlmProvider; Finish.kind maps
from StopReason; eval/live.py _default_provider_factory L496 returns the native
provider; recorded fixture bytes unchanged. Tests: tests/eval/test_replay.py,
test_live.py (skipped without --live), tests/native/test_differential*.py.

P9 - v4 deletion and TUI native switch (depends P0-P8): delete core/wire.py,
the v4 session stack (session/{derive,events,fork,layout,session,writer,store,
reader,metadata,image_projection}.py), v4 llm (llm/messages*.py,
openai_compat.py, retry.py v4 types), agent/{session_turn,session_stream,
session_state,session_prompt,session_support,session_tools,native_provider,
inbox,context_plan v4 leftovers}.py, compact/tokens.py/surface.py if anything
remains, goal/replay.py, builtin/activity.py, prompt v4 classes; empty the
tests/integration/test_wire_boundary.py allowlist (L26-55) and the AGENT_WIRE_
ALLOWLIST seam assertion (L111-121) so the footprint is zero; remove
legacy-v4 flags (headless.native_loop_enabled L129, DEEPPYRO_TUI_NATIVE);
freeze dpyro migrate (migrate.py L1216, session/migrate_cli.py L29) as the only
v4 reader, moved under a legacy/ package with an explicit wire exception, or
documented as removed in 0.3.0 with KI-002; de-brand the DSH names (KI-032
list). Gates: wire allowlist empty, no core.protocols imports outside the
native tool contract wave, targeted test dirs green, docs/release notes
updated.

Dependency summary: P0 -> P1 -> {P2, P4, P5, P6, P7} with P2 -> P3, P5/P6 -> P7,
P8 independent of P2-P7, P9 last. P1 is the only true blocking wave; P2 and P8
can run in parallel with P5/P6.

---

## 9. Behavior changes that must be documented as 0.3

Nothing here is a new dependency or a wire change inside the provider payload;
all of it is a change in what the native runtime records and how it drives
itself. Each item goes to docs/release/0.3.0 notes and to the native schema
compat table.

### 9.1 Compaction

- Token estimates use context_plan's estimator (4 chars/token, image 256,
  message overhead 4, tool overhead 8; context_plan.py L20-56) instead of
  compact/tokens.py CHARS_PER_TOKEN=3.5. Auto-compaction may trigger at a
  slightly different point; keep_recent_tokens semantics are preserved but
  measured with the new estimator. Record the new baseline.
- No compaction/start, compaction/end, rawOutput, surfaceOps, shadowedTokenCount,
  provider/model/usage in the session log. One EventKind.COMPACTION_DONE fact
  with {compaction_id, covers, message}; the summary message has
  CompactionSource(compaction_id) and carries no token counters. Tokens, latency,
  retries and cost of the summarization call live only in the trace.
- A manual compress block may start at message 1 (system prompt is a separate
  system.message, not a surface head), so the v4 COMPACT_SYSTEM_PROMPT refusal
  disappears; adjacent/overlapping/covered message rules remain (schema
  compaction validation + fold).
- Partial manual compression: no error-chain v4 record; blocks whose checkpoint
  appended stay committed and the tool result reports committed blocks (v4
  COMPACT_PARTIAL_WRITE semantics kept, but reconstruction is by replaying the
  compaction.done facts).
- Refs are message-id based: AddressableRef.message_id, sidecar .refs.jsonl
  format changes; 0.2 refs files are ignored (deletable caches).

### 9.2 Goal rounds

- rounds_started advances only via goal.changed snapshots and only through
  GoalService.admit_round; goal/replay.py's user-message admission fold is gone.
- There is no pre-step fence: a goal change (pause/edit/complete/block) during a
  running round interrupts the run instead of rejecting the queued prompt; the
  prompt-rejected block code is no longer produced (queue-failed and round-limit
  remain).
- Round messages carry GoalSource(goal_id, revision, round); the initial user
  prompt keeps UserSource. Round 0 is the user's own prompt and is never a
  synthetic goal round.
- After a restart the driver is disarmed (activation is process-local by design);
  an unfinished round is left to explicit native resume, never auto-driven.
- Each round has its own BudgetLimits; rounds do not share budgets. max_goal_
  rounds is the only cross-round cap. A budget failure ends the round and blocks
  the goal with the BudgetExceeded reason code.

### 9.3 Team

- No team/usage records and no parent-side member stream mirror; usage comes
  from the child run traces (RunUsage) and the UsageSink. TUI team panel reads
  the team projection instead of intercepted events.
- Team member and task facts are plugin.team.* events plus core task.state;
  task ids are task_<16 hex> (was task-<12 hex>). Team tasks and task-tool tasks
  now share one board in the parent session, so each is visible to the other's
  list tool.
- Restore rebuilds the roster/queues/tasks from plugin.team.* events; MemberRun
  history is reconstructed from child run facts + trace joins, not stored.
- Member child sessions are native sessions with origin=subagent, parent_id and
  delegation_depth; they are visible to native session listing (native_index.py)
  which v4 child sessions were not.

### 9.4 Usage and trace

- Token, latency, retry and cost data appear only in trace records
  (ModelCallRecord/ToolCallRecord) and in UsageSink.TurnUsage with
  source="native" | "team:<id>" | "subagent:<id>"; no session event contains
  them (schema section 8.5). Compaction/subagent/team usage records are deleted.
- Retries stay in ModelResponse.retries -> ModelCallRecord.retry_count; the
  native loop records a real retry count (v4 hardcoded 0).

### 9.5 Subagent

- v4 subagent/catalog + subagent/descriptor records become
  plugin.subagent.started/finished; the parent notice is queued on the parent
  handle and delivered at the next parent run (no mid-turn inbox admission).
- Usage is trace-backed (RunUsage alias); the ad-hoc subagent UsageSink Protocol
  and usage record are gone.
- Fork copies native events with renumbered seq, preserved message ids and
  parent_id; inherited_event_count is the copied count. The v4 build_fork_seed/
  session/fork.py path is gone.
- Supervisor semantics are unchanged (worker budgets, convergence, stall, round
  limit).

### 9.6 Prompt projection

- The system prompt is written as an append-only system.message fact (last-wins
  in the fold); a change creates a new system.message instead of replacing a seq
  range. Prompt history is therefore visible in the session.
- Runtime context remains one user message, refreshed in place with
  message.replaced (same id/created_at/source).
- Prompt section renderers receive Sequence[ToolSpec] (native) not
  list[ToolSchema] (wire); prompt_hash therefore changes for identical text
  inputs, and recorded prompt hashes from 0.2 are not comparable. Recorded
  provider request bodies stay valid for replay (llm/native_messages.py emits
  the same chat payload shape).

### 9.7 Projections

- Todos and plan mode are read from fold state; the agents projection is fed by
  plugin.team.* / plugin.subagent.* events; jobs by plugin.jobs.*. ActivityService
  is deleted (one writer of activity state).
- Projection sidecars keep format version 1 but changed definitions bump
  state_version, so old .projections.json files are ignored and rebuilt.

### 9.8 Task tools and compatibility

- Only native task.state/task.handoff/task.handoff.result events are written; the
  v4 ignorable extension records disappear, so a 0.3 session is not readable by
  the 0.2 v4 TUI (0.3 reads only schema 1; v4 logs are read only by the frozen
  dpyro migrate one-off, or not at all if 9.9 is chosen).
- The v4 wire contract, v4 logs, V4Codec, dual write and migration-on-open were
  already removed by ADR 0001 D2; 0.3 completes it by deleting the code. v4
  session files remain on disk untouched and are never opened by the runtime.
- Unrepresentable v4 state that 0.2 migration already documented as dropped
  stays dropped: per-record token accounting inside the session, compaction
  rawOutput/usage envelopes, team member stream envelopes, pre-step fence
  decisions, and the v4 event bus payloads (inbox inserted/claimed/discarded,
  next-step/next-turn/turn-end records, goal/change records). None of these has a
  native equivalent by design; replay/checkpoint/eval rely on native events plus
  trace joins instead.
- No new runtime dependencies are introduced by any phase.

---

## 10. Definition of done checklist

- Wire boundary: tests/integration/test_wire_boundary.py allowlist empty; no new
  core.wire/core.protocols import in any module listed in sections 2-7.
- Session logs from the capabilities contain only schema-1 facts; grep-style
  tests assert no inputTokens/totalTokens/latency fields in recorded session
  JSONL for compaction/team/subagent/goal flows.
- Every capability has one writer and one reader per fact set: compaction
  (section 2), team (3), subagent (4), goal (5), prompt (6), tasks (7).
- Targeted test directories per phase pass; the full suite is only run by CI.
- The 0.3 release notes contain section 9 verbatim as the behavior-change list.

