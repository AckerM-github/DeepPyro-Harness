# Default subagent layer (one-shot child agents)

Status: implemented. This document is the contract for the default
subagent service, the agent-facing tool and the kernel plugin row. The team
layer remains the multi-agent supervisor; this layer is the small, always-on
delegation primitive the main agent uses to hand one self-contained task to a
fresh child agent and receive its final reply.

## 1. Design rules

* One-shot: a run is a single await that returns the child's final reply. There
  is no background member, no mailbox and no follow-up send; a run cannot be
  resumed. The caller receives a SubagentResult handle only after the child
  finished (or failed).
* No nesting: the child agent never receives the subagent tool or any team_*
  tool. The orchestration tools are stripped with CHILD_DISABLED_TOOLS from
  deeppyro.api.types.
* No shared conversation: the child starts from an empty seeded session; the
  parent transcript is not copied into it and child stream events are
  swallowed by a private sink.
* Durable parent log: the parent session receives subagent/catalog and
  subagent/descriptor records before the child send, so the activity surface
  can show pending and failed children even if the run later fails.
* Injected kernel: the service only uses injected agents, sessions and sandbox
  services. It never imports the app, the kernel composition or a provider, so
  it is unit-testable with fakes.

## 2. Service API

Module: src/deeppyro/subagent/manager.py. Export: deeppyro.subagent.SubagentService.

    SubagentService(*, agents=None, sessions=None, sandbox=None, max_runs=4)

    configure(*, cwd, provider, registry, permission, model, context_window,
              preset='default', preset_revision=None, services=None, ask=None)
    bind_session(session)
    set_ask(ask)
    await run(prompt, *, description, name=None, provider=None, model=None,
              preset=None) -> SubagentResult
    await cancel_all() -> int
    await close() -> None
    properties: max_runs, live_runs, parent_session_id

configure() attaches the live parent context: cwd, resolved parent provider
object, parent tool registry, parent permission gate, model name and context
window, default preset and revision, the session services dict handed to child
agents, and the approval callback used by the child gate. bind_session() binds
the parent session whose append() receives the records. set_ask() lets the
runtime update the approval callback without reconfiguring everything.

SubagentResult (deeppyro/subagent/types.py) fields:

    child_id: str          'subagent-' + 12 hex chars, unique per run
    session_id: str        durable child session id
    label: str             caller name or generated 'subagent-N'
    provider: str          resolved provider name
    model: str             resolved model id
    text: str              child final reply (empty on error)
    is_error: bool         True when the child run failed
    error: str             'ExceptionType: message' when is_error

MAX_LIVE_RUNS is 4. A run beyond the limit raises SubagentError; the service
never queues. DEFAULT_CONTEXT_WINDOW (32768) is only a fallback default; the
runtime should pass the parent context window in configure().

## 3. Run sequence

1. Validate the prompt (non-empty after strip) and readiness: service open,
   agents and sessions present, configured, parent session bound, registry
   present.
2. Enforce the live-run limit.
3. Resolve the provider: None keeps the parent provider; a string matching the
   parent provider name reuses the parent object; any other string is resolved
   through services['llm'].provider(name); an object is used as-is.
4. Allocate child_id and label. An explicit name wins; otherwise the service
   counter produces subagent-1, subagent-2, ... in this process.
5. Create the child session (section 4), then the child agent (section 5).
6. Append subagent/catalog and subagent/descriptor to the parent (best effort).
7. await agent.send(prompt). A child exception becomes a failed result with
   text='' and error='Type: message'; cancellation propagates.
8. In finally: release the live slot, forget the task and call agent.aclose()
   when that method exists (cleanup errors are swallowed).

## 4. Child session

Mirrors the team manager conventions. The session is created through the
sessions service instead of the store's create() helper:

    store = sessions.store
    path = flat_session_path(Path(store.root), session_id)
    write_new_session(path, header, [])
    Session(path=path, header=header, records=[])

SessionHeader fields: id=new_session_id(), createdAt=now_ms(),
cwd=configured cwd, isSeeded=True, delegationDepth=1, agentPreset=preset id,
parentSession=parent session id, origin='subagent'.

The child session is a normal durable session: it is openable through the
session store and its records can be inspected after the run. It is not
announced to the TUI by this layer; the parent records are the UI hook.

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
  therefore never call subagent or team_* tools.
* _ChildSink: emit() does nothing, so child streaming never reaches the parent
  sink and the tool result stays the only child output.
* child_permission: sandbox.gate(cwd, ask=ask) when the sandbox exposes gate();
  TypeError falls back to gate(cwd). The parent gate preset and approval are
  copied over when the new gate supports set_preset/set_approval.
* preset_revision is preserved only for the configured default preset; an
  override preset resolves its own revision (None) so callers may point at
  another preset.

## 6. Parent records

Both records use existing core RECORD_TYPES and are written before agent.send().
Failures to append are swallowed: logging must never break a run.

subagent/catalog

    { version: 1, childId, childCreatedAt: <ms of child header.createdAt>,
      mode: 'spawn', label }

subagent/descriptor

    { version: 1, childId, mode: 'spawn', provider, label,
      agentProvider, agentModel }

childId is unique and label is the human name, matching the activity
projection (docs/spec/activity.md): the activity bar builds one AgentInfo per
descriptor and shows catalog/descriptor state for the conversation.

## 7. Kernel plugin

Module: src/deeppyro/builtin/subagent.py. SubagentPlugin(Plugin):

    plugin_id = 'subagent'
    inject = ['agents', 'sessions', 'sandbox', 'tools']

setup() constructs SubagentService from the injected services, publishes it
with ctx.provide('subagent', service) and registers one tool contribution whose
factory returns subagent_tools().

Bundle row (already present in src/deeppyro/bundles/core/bundle.toml after the
team row):

    [[plugins]]
    id = 'subagent'
    plugin = 'deeppyro.builtin.subagent:SubagentPlugin'

Runtime wiring (implemented in TuiRuntime._configure_subagent and
HeadlessRuntime._configure_subagent): resolve kernel_app.service('subagent'),
call configure() with the live parent context after boot and after every
new/open session, call bind_session(session), and await close() during runtime
shutdown so in-flight children are cancelled.

## 8. Agent-facing tool

Module: src/deeppyro/tools/builtin/subagent.py. Tool name: subagent;
parallel_safe=False (a child run holds a live slot and must not race siblings).

Parameters: prompt (required string), description (required string; short
reason for the catalog), name, provider, model, preset (optional strings; blank
values are treated as omitted). The service is resolved from
ctx.services['subagent']; a missing service raises ToolError.

Success result:

    text = 'subagent <label> (<session_id>) completed:' + newline + final reply
    meta = { childSessionId, label, provider, model }

Failure: is_error=True, text is result.error (or 'subagent run failed' when
empty), meta still identifies the child. A service exception becomes
'subagent failed: message'. The tool description warns that it starts a child
agent and that installations or other side effects run under the normal human
approval flow; it also states that the child cannot spawn further agents.

## 9. Errors

SubagentError(DeeppyroError) is raised for caller errors: empty prompt,
unreadable service state, not configured, no bound parent session, missing
registry, live-run limit, unknown llm provider, full session creation or agent
creation failures, and closed service. Child runtime failures are returned as
SubagentResult(is_error=True) instead of raised.

## 10. Tests

tests/subagent/ uses fakes only (no kernel boot, no network): FakeAgents,
FakeAgent, FakeSandbox, FakeLlm and a real SessionStore rooted in tmp_path.
Coverage: child header fields and parent record shapes, child registry
exclusions, sink isolation, gate cloning, provider/model/preset resolution,
limits, cancellation/close, error results, the tool contract and the plugin
wiring. tests/tools/helpers.make_context builds ToolContext doubles.

## 11. Deviations and known gaps

* description is forwarded by the tool but not consumed by the service, except
  for appearing in tool-facing messages; the records carry label only.
* label numbering is per-process and monotonic; colliding names are allowed.
* There is no live progress stream or partial reply: the tool blocks until the
  child finishes or fails.
* Child session records are not summarized or linked beyond the parent
  subagent records; the runtime may list them later.
* If agents.create() or the child session write fails, no parent records are
  written (the failure surfaces as SubagentError), so the activity bar shows
  nothing for that attempt.
