# Default subagent layer (spawn and fork child agents)

> **状态**：现行（M4 已扩展） · **层级**：功能规格 · **最后核对**：2026-11-18
> 0.2.0 边界：supervisor + 2 workers 已落地；未接真实 subagent 生命周期（无 token/step usage）。
> 0.3 增补：原生（schema 1）分支已落地，与 v4 并存；由绑定的会话类型选择，v4 路径保持不变。

Status: implemented. This document is the contract for the default subagent
service, its two agent-facing tools and the kernel plugin row. The team layer
remains the multi-agent supervisor; this layer is the small, always-on
delegation primitive the main agent uses to hand a self-contained task to a
child agent.

## 1. Design rules

* Two entry points. `run()` is the original one-shot call: it awaits the child
  to completion and returns its final reply. `start()` creates a background
  activation, returns a handle immediately, and later delivers a settlement
  notice to the parent through the parent agent's follow-up inbox.
* Two modes. `spawn` starts from an empty seeded session. `fork` copies the
  parent transcript prefix through its last completed turn and then runs
  independently; later parent turns are never copied into the child.
* No nesting: the child agent never receives the subagent tool, `subagent_fork`
  or any team_* tool. The orchestration tools are stripped with
  CHILD_DISABLED_TOOLS from deeppyro.api.types.
* No shared conversation: a spawned child starts from an empty seeded session
  and a forked child starts from a frozen prefix snapshot. Neither shares live
  context with the parent, and child stream events are swallowed by a private
  sink.
* Durable parent log: the parent session receives subagent/catalog and
  subagent/descriptor records before the child send, so the activity surface
  can show pending, running and failed children even if the run later fails.
  For a fork the descriptor is also appended to the child session itself (the
  DSH authority position) before the parent copy.
* Injected kernel: the service only uses injected agents, sessions and sandbox
  services. It never imports the app, the kernel composition or a provider, so
  it is unit-testable with fakes.

## 2. Service API

Module: src/deeppyro/subagent/manager.py. Export: deeppyro.subagent.SubagentService.

    SubagentService(*, agents=None, sessions=None, sandbox=None, max_runs=4)

    configure(*, cwd, provider, registry, permission, model, context_window,
              preset='default', preset_revision=None, services=None, ask=None)
    bind_session(session)
    bind_parent(agent)
    set_ask(ask)
    await run(prompt, *, description, name=None, provider=None, model=None,
              preset=None) -> SubagentResult
    await start(prompt, *, description, name=None, provider=None, model=None,
                preset=None, mode='spawn') -> SubagentActivation
    await wait_children(timeout=None) -> None
    await cancel_all() -> int
    await close() -> None
    properties: max_runs, live_runs, parent_session_id

configure() attaches the live parent context: cwd, resolved parent provider
object, parent tool registry, parent permission gate, model name and context
window, default preset and revision, the session services dict handed to child
agents, and the approval callback used by the child gate. bind_session() binds
the parent session whose append() receives the records. bind_parent() binds the
parent agent instance that receives settlement notices via followup().
set_ask() lets the runtime update the approval callback without reconfiguring
everything.

SubagentResult (deeppyro/subagent/types.py) fields:

    child_id: str          durable child session id (also the display id)
    session_id: str        same value as child_id
    label: str             caller name or generated 'subagent-N'
    provider: str          resolved provider name
    model: str             resolved model id
    text: str              child final reply (empty on error)
    is_error: bool         True when the child run failed
    error: str             'ExceptionType: message' when is_error
    mode: str              'spawn' or 'fork'

SubagentActivation fields (returned by start()):

    child_id: str          durable child session id
    session_id: str        same value as child_id
    label: str             caller name or generated 'subagent-N'
    provider: str          resolved provider name
    model: str             resolved model id
    mode: str              'spawn' or 'fork'
    created_at: int        child session header createdAt in ms

MAX_LIVE_RUNS is 4. A run beyond the limit raises SubagentError; the service
never queues. DEFAULT_CONTEXT_WINDOW (32768) is only a fallback default; the
runtime should pass the parent context window in configure().

## 3. Run sequences

Both sequences share steps 1-3:

1. Validate the prompt (non-empty after strip), the mode (`spawn` or `fork`,
   anything else raises `unsupported subagent mode: ...`) and readiness:
   service open, agents and sessions present, configured, parent session bound,
   registry present.
2. Enforce the live-run limit.
3. Resolve the run inputs. A spawned run resolves the provider: None keeps the
   parent provider; a string matching the parent provider name reuses the
   parent object; any other string is resolved through
   services['llm'].provider(name); an object is used as-is. A fork always uses
   the parent provider, model and preset; explicit overrides are rejected.
4. Create the child session (section 4): empty seed for spawn, frozen prefix
   for fork.
5. Allocate the label and child_id (the child session id). An explicit name
   wins; otherwise the service counter produces subagent-1, subagent-2, ... in
   this process.
6. Create the child agent (section 5), then append the parent records
   (section 6, best effort).
7. Start the background activation task and return its activation. `run()`
   awaits that task before returning; `start()` returns immediately.
8. In finally: release the live slot, forget the task and call agent.aclose()
   when that method exists (cleanup errors are swallowed).

A child exception becomes a failed result with text='' and
error='Type: message'; cancellation propagates. For a background start the
failure is also reported as a settlement notice (section 8).

## 4. Child session

Spawned child session (same content as before this document was extended): the
session is created through the sessions service instead of the store's
create() helper:

    store = sessions.store
    path = flat_session_path(Path(store.root), session_id)
    write_new_session(path, header, [])
    Session(path=path, header=header, records=[])

SessionHeader fields: id=new_session_id(), createdAt=now_ms(),
cwd=configured cwd, isSeeded=True, delegationDepth=1, agentPreset=preset id,
parentSession=parent session id, origin='subagent'.

Forked child session: the same header fields except delegationDepth and the
seeded record prefix:

* boundary = the seq of the last `turn/end` record in the parent session, or
  absent when the parent has no completed turn.
* No boundary: seed=[], inheritedEventCount=0, delegationDepth=1.
* Boundary found: seed=build_fork_seed(parent.records[:boundary+1], boundary),
  inheritedEventCount=boundary+1, delegationDepth=parent header delegationDepth
  + 1. build_fork_seed copies the prefix, inserts the inherited
  `session/end-seed` marker and synthesizes closers for an open turn/step (see
  docs/spec/session.md), so the child never starts mid-turn.
* The session is written with SessionStore.create_with_seed(header, seed). A
  store without that method raises
  'sessions store does not support seeded sessions'; a store failure becomes
  'cannot create fork session: ...'.
* The seed is a one-time snapshot: only the pre-fork completion notices are
  present, never the parent's later turns.

The child session is a normal durable session: it is openable through the
session store and its records can be inspected after the run. It is not
announced to the TUI by this layer; the parent records are the UI hook. The
child session id is also the member id used by the activity projection.

## 5. Child wiring

    options = agents.options(model=resolved_model, context_window=context_window)
    agent = agents.create(
        session=child_session,
        provider=resolved_provider,
        registry=filter_child_tools(parent_registry),
        options=options,
        sink=_ChildSink(),
        permission=child_permission,
        cwd=str(configured_cwd),
        services=dict(configured_services),
        preset=preset_id,
        preset_revision=preset_revision,
    )

* filter_child_tools(registry): calls registry.without(CHILD_DISABLED_TOOLS)
  when the registry provides that method, otherwise builds a new ToolRegistry
  excluding every tool whose name is in CHILD_DISABLED_TOOLS. A child can
  therefore never call subagent, subagent_fork or team_* tools.
* _ChildSink: emit() does nothing, so child streaming never reaches the parent
  sink and the tool result is the only child output for a one-shot run; a
  background run reports only its settlement notice.
* child_permission: sandbox.gate(cwd, ask=ask, session=child session) when the
  sandbox exposes gate(); TypeError falls back to gate(cwd, ask=ask) and then
  gate(cwd). The parent's current sandbox mode and approval policy are seeded
  onto the child session through sandbox.policy_service.set_mode() and
  sandbox.approval_service.set_policy() when those services are available;
  a seeding failure is swallowed so it never breaks a spawn.
* preset_revision is preserved only for the configured default preset; an
  override preset resolves its own revision (None) so callers may point at
  another preset.

## 6. Parent records

Both records use existing core RECORD_TYPES and are written before the child
send. Failures to append are swallowed: logging must never break a run.

subagent/catalog (parent session only)

    { version: 1, childId, childCreatedAt: <ms of child header.createdAt>,
      mode: 'spawn' | 'fork', label }

subagent/descriptor

    { version: 1, childId, mode: 'spawn' | 'fork', provider, label,
      agentProvider, agentModel [, inheritedEventCount] }

inheritedEventCount appears only for a fork and equals boundary+1 (the number
of parent records copied into the child). The descriptor is appended to the
child session first (the DSH authority position) and then to the parent session
for backwards-compatible activity projection.

childId is unique and label is the human name, matching the activity
projection (docs/spec/activity.md): the activity bar builds one AgentInfo per
descriptor and shows catalog/descriptor state for the conversation. The same
records also drive the session projections (docs/spec/projections.md).

## 7. Kernel plugin and runtime wiring

Module: src/deeppyro/builtin/subagent.py. SubagentPlugin(Plugin):

    plugin_id = 'subagent'
    inject = ['agents', 'sessions', 'sandbox', 'tools']

setup() constructs SubagentService from the injected services, publishes it
with ctx.provide('subagent', service) and registers one tool contribution whose
factory returns subagent_tools() (both the blocking `subagent` tool and the
background `subagent_fork` tool).

Bundle row (already present in src/deeppyro/bundles/core/bundle.toml after the
team row):

    [[plugins]]
    id = 'subagent'
    plugin = 'deeppyro.builtin.subagent:SubagentPlugin'

Runtime wiring: both hosts resolve the typed contract with
kernel_app.get(SubagentService) (TuiRuntime.__init__, and headless.py right
after boot; the string kernel_app.service('subagent') form no longer exists,
see KI-035). TuiRuntime._configure_subagent then calls configure() with the
live parent context after boot and after every new/open session, calls
bind_session(), calls bind_parent(agent) when the parent agent exists, and
awaits close() during runtime shutdown so in-flight children are cancelled.

## 8. Settlement notices

A background activation (`start()`, used by the fork tool) reports its outcome
to the parent agent through the parent session inbox. The notice text is:

* success: 'Background subagent <child_id> finished and will do no further work
  unless you send it more.'
* failure: 'Background subagent <child_id> failed before it finished.'

followed by either 'Its closing message:' + the child reply, or 'It left no
closing message.' when the reply is empty after strip. The call is
parent_agent.followup(notice, source=source) where source is

    { kind: 'subagent-settled', form: 'notice', summary: <summary line>,
      senderSessionId: child_id }

The notice only reaches a live parent agent: when no parent agent is bound, or
the agent has no followup(), the notice is dropped with a logger warning. An
exception from followup() is caught and logged; it never fails the child run.

## 9. Agent-facing tools

Module: src/deeppyro/tools/builtin/subagent.py. subagent_tools() returns two
tools; both are parallel_safe=False (a child run holds a live slot and must not
race siblings).

`subagent` (one-shot, blocking): parameters prompt (required string),
description (required string; short reason for the catalog), name, provider,
model, preset (optional strings; blank values are treated as omitted). The
service is resolved from ctx.services['subagent']; a missing service raises
ToolError.

Success result:

    text = 'subagent <label> (<session_id>) completed:' + newline + final reply
    meta = { childSessionId, label, provider, model }

Failure: is_error=True, text is result.error (or 'subagent run failed' when
empty), meta still identifies the child. A service exception becomes
'subagent failed: message'. The tool description warns that it starts a child
agent and that installations or other side effects run under the normal human
approval flow; it also states that the child cannot spawn further agents.

`subagent_fork` (background, seeded): parameters prompt (required string),
description (required string), name (optional). It accepts no provider, model
or preset parameter; the child always inherits the parent's configuration and
preset. The tool returns as soon as the activation starts:

    text = '{"kind": "activation", "subagentId": "<child id>"}'
    meta = { childSessionId, label, mode: 'fork' }

The actual outcome arrives later as a settlement notice (section 8). A start
failure (empty prompt, live limit, rejected override, session creation,
agent creation) is returned as is_error=True with
'subagent fork failed: <message>'.

## 10. Errors

SubagentError(DeeppyroError) is raised for caller errors: empty prompt,
unsupported mode, unreadable service state, not configured, no bound parent
session or parent agent, missing registry, live-run limit, unknown llm provider,
fork override attempts, full session creation or agent creation failures,
wait_children timeout, and closed service. Child runtime failures are returned
as SubagentResult(is_error=True) instead of raised.

## 11. Tests

tests/subagent/ uses fakes only (no kernel boot, no network): FakeAgents,
FakeAgent, FakeSandbox, FakeLlm and a real SessionStore rooted in tmp_path.
Coverage:

* test_manager.py: child header fields and parent record shapes, child registry
  exclusions, sink isolation, gate cloning, provider/model/preset resolution,
  limits, cancellation/close, error results.
* test_fork.py: fork seeds only completed turns, empty seed without a completed
  turn, override rejection, spawn activation child id, wait_children timeout
  without cancellation, close cancels a background fork, fork tool activation
  result, start-failure tool errors, empty prompt, tool schema and missing
  service, failure notice text, notice dropped without a parent agent.
* test_plugin.py / test_tool.py: the plugin wiring and the blocking tool
  contract.

tests/tools/helpers.make_context builds ToolContext doubles.

## 12. Deviations and known gaps

* description is forwarded by the tools but not consumed by the service,
  except for appearing in tool-facing messages; the records carry label only.
* label numbering is per-process and monotonic; colliding names are allowed.
* A one-shot run has no live progress stream or partial reply: the tool blocks
  until the child finishes or fails. A background activation has only the
  settlement notice, no token-level stream.
* Fork copies the parent prefix only up to its last completed turn; a parent
  turn that is still running when the fork starts is not represented and the
  child starts from an empty seed if the parent has no completed turn yet.
* Fork excludes explicit provider/model/preset overrides by design (DSH
  behaviour). Spawn still accepts them.
* Child session records are not summarized or linked beyond the parent
  subagent records; the runtime may list them later.
* If agents.create() or the child session write fails, no parent records are
  written (the failure surfaces as SubagentError), so the activity bar shows
  nothing for that attempt.
* Settlement notices are only delivered while a parent agent is bound; a
  headless run that closes the parent before the child settles loses the notice
  (the run's own records are still complete).

## 13. Native runtime branch (schema 1 sessions)

The native branch is additive: the same `SubagentService` routes to it when the
bound parent session is a native session, and every v4 path above is unchanged.
The implementation lives in `src/deeppyro/subagent/native_manager.py`
(`NativeSubagentService`) and is exported from `deeppyro.subagent` together
with `NativeSubagentServiceWorker`. It imports no v4 session, wire or agent-loop
module; children are native sessions and child agents are native hosts.

Selection and wiring:

* `configure()` forwards its arguments (plus the optional `prompt_facts`) to the
  native service; `bind_session()` accepts a plain `NativeSession`, a
  `NativeAgentHost` or a `NativeSessionHandle` (anything exposing a
  `native_session`). `bind_parent()` records the parent host so settlement
  notices can be queued through it.
* When a native parent is bound, `run()`, `start()`, `usage()`,
  `wait_children()` and `cancel_all()` delegate to the native service, and the
  new `await restore()` delegates to its restore scan. `close()` closes both
  branches; the v4 branch is never created or touched for a native parent.

Child sessions and fork prefixes:

* spawn: `sessions.native_create(NewSession(cwd, preset=preset,
  origin='subagent', parent_id=parent.header.id,
  delegation_depth=parent.header.delegation_depth + 1))`. The child header
  therefore carries the native parent id, origin `subagent` and depth +1.
* fork: `session.native_fork.fork_native_session(parent_id, through_seq=bound)`
  where `bound` is the seq of the parent's last `turn.ended` event (the
  session tail when the parent has no completed turn). The fork copies only
  events with `seq <= bound`; it renumbers seq, rebases timestamps and sets
  origin `fork`, parent id and depth +1 by the same crash-safe write path as a
  full fork. It never fabricates a turn or step closer and never rewrites a
  payload. `inherited_event_count` is `forked_event_count(child)`, i.e. the
  number of copied events, and the child starts from that frozen prefix.
* Fork keeps the v4 override contract: provider, model and preset overrides are
  rejected with `SubagentError`. NativeForkError codes are mapped to
  `SubagentError` ('cannot fork subagent session: ...').

Parent lifecycle facts (native extension events, not v4 records): every fact is
appended to the parent native session as a raw `plugin.subagent.*` event type,
which the schema-1 validator accepts (plugin payload validation is owned by the
plugin). Payloads are snake_case and carry no token accounting, so the session
log stays a pure fact record (schema §8.5); usage never enters the log.

* `plugin.subagent.started`: `{version: 1, child_session_id, label, provider,
  model, mode: 'spawn'|'fork', inherited_event_count, created_at}`.
* `plugin.subagent.finished`: `{version: 1, child_session_id, status:
  'ok'|'error'|'cancelled', is_error, usage_known}` plus `error` when set and
  `text` when the child left a closing message.
* `plugin.subagent.notice`: `{version: 1, child_session_id, state:
  'delivered'|'queued'|'dropped', summary}` recording where the settlement
  notice went.

Settlement notices:

* The child is closed and the finished fact is written before the notice.
  The notice text is the v4 wording ('Background subagent <id> finished and
  will do no further work unless you send it more.' / '...failed before it
  finished.' plus 'Its closing message:' or 'It left no closing message.').
* Delivery uses the parent native host: `deliver(notice,
  source=PluginSource('subagent'))`. When the parent is idle this appends a
  user-message fact and reports `delivered`; while the parent is busy the
  notice is queued in the bounded native notice deque (report `queued`) and the
  parent host drains it into a user-message fact on its next run; a full queue
  or a missing/unusable parent host reports `dropped` with a logger warning.
  The native runtime has no mid-turn inbox, so a queued notice waits for the
  parent's next prompt by design.
* No settlement notice is emitted for `run()` (v4 semantics); `start()` emits
  exactly one.

Usage and cancellation:

* Usage comes from the child host: `host.usage()` counters (input, output,
  cached), the `RunResult.steps` and a wall-clock latency; retries stay in the
  trace side channel. It is returned as `SubagentUsage`, aggregated by
  `usage()` and reported once through the configured UsageSink; it is never
  written to a session. A cancelled or failed run still settles with the usage
  it spent.
* `cancel_all()` cancels the background tasks, settles each child as
  `status: 'cancelled'` (zero usage when the run never started), and closes the
  child hosts and sessions.

Restore after a restart:

* `await restore()` scans the bound parent session for `plugin.subagent.started`
  ids without a matching `plugin.subagent.finished`, reopens each child with
  `sessions.native_open`, rebuilds a host from the recorded provider/model and
  the child preset, calls `host.restore()` (which repairs a dangling open turn)
  and keeps the host for later `close()`. It returns the restored child ids,
  skips children that are already finished or live, logs and skips failures,
  and returns `()` when no native parent is bound. Already finished children are
  never re-notified.

Supervisor:

* `NativeSubagentServiceWorker(service)` mirrors `SubagentServiceWorker`:
  `Worker` contract and `WorkerOutcome` mapping (summary, steps, tokens, done =
  not is_error, failed = is_error, usage) are identical, so `Supervisor` runs
  against either service. The guard raises SupervisorError('native subagent
  worker requires a NativeSubagentService').

Known gap (deferred to the v4 deletion wave):

* Child-tool filtering is not yet applied on the native branch: `AgentsPlugin.host`
  builds the preset-scoped registry and `agent/native_factory.py` has no
  child-registry hook yet (the planned `agent/children.py` `child_tools()`
  helper is not landed). Until then a native child host could still see the
  delegation tools. The native service strips the `subagent` service from the
  child service map, so a nested `subagent` tool call fails cleanly with
  ToolError ('subagent service is not available in this session').

## 14. Native branch tests

* `tests/subagent/test_native_manager.py` (real native sessions on a tmp root,
  duck-typed agents/host/usage fakes, no kernel boot or network) covers: spawn
  child header fields and the started fact, AgentBuild fields including the
  stripped `subagent` service, measured usage plus UsageSink record, finished
  fact without usage keys, error results, unknown-provider and readiness errors,
  notice delivery/queueing and wording, fork prefix (only completed-turn
  prefix, no synthetic closer, inherited count), fork override rejection and
  tail fallback, cancellation settlement, restore (unfinished only, finished
  skipped), manager native routing, the native supervisor worker and its guard,
  and an AST guard proving the native module imports no v4 module.
* `tests/session/test_native_fork.py` gained prefix cases for
  `through_seq`/`forked_event_count` (consistent prefix, zero/full/beyond-tail
  bounds, invalid bounds) while every full-fork case stays green.
* All v4 subagent tests are unchanged.

## 15. Behaviour changes (native branch only)

* Parent lifecycle facts are `plugin.subagent.*` events instead of
  subagent/catalog and subagent/descriptor records; there are no child-session
  descriptor records on the native branch.
* Usage is not journaled to any session; the live counters come from the child
  host and the sink/trace, so after a restart the finished fact may report
  `usage_known: false` and usage must be recomputed from the trace.
* A settlement notice raised while the parent is busy waits for the parent's
  next run (bounded queue, explicit `queued`/`dropped` fact) instead of being
  delivered immediately.
* Fork children copy a native event prefix (seq-renumbered, timestamp-rebased)
  rather than v4 seed records; the boundary is the last completed parent turn.
* Restored children are re-hosted but not re-run and not re-notified; the child
  session itself remains the authority for its transcript.

## 16. Ownership note

The native branch required one additive change outside `subagent/`: 
`src/deeppyro/session/native_fork.py` gained the optional `through_seq` keyword
(default `None` preserves every existing caller and byte behaviour) and the
`forked_event_count()` accessor, reusing the existing temp+fsync+os.replace
write path. Prefix tests were appended to `tests/session/test_native_fork.py`.
This extension was approved by the lead (message m00142) and is the only file
outside the subagent ownership touched.
