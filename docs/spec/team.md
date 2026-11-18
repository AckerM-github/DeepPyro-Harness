# Agent team layer (v4 records and native sessions)

Status: implemented. This document is the contract for the lead-owned team
service: up to eight child members, member messaging, cold restore and the
shared task board. The same public API runs on two backends: the legacy v4
recorder path (session.records based) and the native session path. A session
type decides which backend a bound service uses; the final 0.3 deletion wave
removes the v4 path.

## 1. Design rules

* Everything is injected. The service only uses agents, sessions and sandbox
  services; it never imports the kernel composition or a provider. Unit tests
  run it with doubles over a real temporary native root.
* Members are real child agents with their own session log. A member id is a
  plugin-local identifier; it is not the child session id (v4 reused the child
  session id, native keeps the two independent so a member can be renamed or
  removed without touching the child log).
* The lead session owns every team fact. v4 writes team records; native writes
  plugin.team.* events. Members never write team state into the lead log
  themselves.
* Roster, pending prompt queue and task board must survive a crash and be
  replayable; they are session facts. Token usage is NOT a session fact: it is
  reported to the UsageSink and recorded in traces only.
* Child stream events are not mirrored to the parent. Team members do not
  notify the parent agent (unchanged from v4); completion is visible through
  the roster and the member.changed facts.

## 2. Service API

Module: src/deeppyro/team/manager.py. Export: deeppyro.team.TeamService.
Native backend: src/deeppyro/team/native_manager.py, export
deeppyro.team.NativeTeamManager (exported from deeppyro.team as well).

    TeamService(*, agents=None, sessions=None, sandbox=None,
                team_id='', max_members=8)

    configure(*, cwd, provider, registry, permission, model, context_window,
              preset=DEFAULT_PRESET_ID, preset_revision=None, services=None,
              ask=None, usage_sink=None)
    bind_session(session)            v4 Session OR native session/handle
    bind_parent(session)             alias for bind_session
    set_ask(ask)
    spawn(name, description, prompt, *, provider=None, model=None,
          preset=None) -> MemberInfo
    members() -> list[MemberInfo]
    get_member(member_id) -> MemberInfo | None
    send(member_id, message) -> 'queued' | 'started'
    await wait(member_id, timeout_s=None) -> MemberInfo
    await cancel(member_id) -> bool
    await close() -> None
    await restore() -> tuple[MemberInfo, ...]
    tasks() / get_task(task_id)
    create_task(subject, description='', *, blocked_by=(), write_scopes=())
    update_task(task_id, action, *, subject=None, description=None,
                blocked_by=None, write_scopes=None, owner=None)
    runs(member_id=None) / run(run_id) / run_usage(run_id) / usage(member_id=None)
    properties: team_id, parent_session_id, max_members

configure() attaches the live lead context used to build children. The native
backend ignores registry: the native agent factory resolves and filters the
child tool registry from the preset scope. bind_session() selects the backend:

* a v4 Session (has records) keeps the recorder path unchanged;
* a native session, or its NativeSessionHandle, routes every operation to
  NativeTeamManager. bind_parent() is a compatibility alias.

## 3. v4 recorder path (preserved)

The v4 path writes v4 records through the lead session append API:

    team/member              {version, teamId, member}
    team/message/queued      {version, teamId, memberId, message.text}
    team/message/delivered   {version, teamId, memberId, message.text}
    team/task                {version, teamId, task}
    team/usage               {version, teamId, runId, memberId, usage, ...} ignorable

Child sessions are created with a v4 SessionHeader (delegationDepth 1, origin
subagent) and driven by the v4 agents.create() API. restore() folds these
records back, pairs queued/delivered messages, rebuilds the roster/board and
resumes members with a queued message. This path and its test suite stay
unchanged until the 0.3 deletion wave.

## 4. Native path

### 4.1 Child sessions and hosts

spawn() creates the child through the sessions service:

    NewSession(cwd=<lead cwd>, preset=<member preset>, origin='subagent',
               parent_id=<lead session id>,
               delegation_depth=<lead depth + 1>)

and builds the agent through agents.host(child_native_session, AgentBuild(...))
with hooks=None (the child stream stays out of the UI), the resolved provider
name, preset and preset revision, the child permission object, the configured
context window and the plugin service map. The host owns the child loop; one
host per member, recreated lazily after a cold restore.

### 4.2 Parent session facts

Every team fact is a plugin event on the lead session; the core EventKind set
is unchanged.

    plugin.team.member.changed    {team_id, member_id, name, description,
                                   provider, model, preset, session_id,
                                   phase, created_at, last_reply?, error?}
    plugin.team.message.queued    {team_id, member_id, text, created_at}
    plugin.team.message.delivered {team_id, member_id, text, created_at}
    plugin.team.task              {version, team_id, task}
    task = {id, revision, subject, description, status, blocked_by,
            write_scopes, owner, created_at}

phase is a TeamPhase member (spawning, idle, running, completed, failed,
cancelled). Task ids keep the v4 shape task-<12 hex> for API stability.

### 4.3 Lifecycle

* spawn records member.changed phase=spawning, then starts the first run in a
  background task.
* A run sets phase=running, records the delivered fact for every turn after the
  first, awaits host.send(text), maps the RunResult: first success sets
  completed, later successes set idle; last_reply is the run final text. A
  cancelled run settles as cancelled; a raised error settles as failed with
  member.error = 'ExcType: message'.
* send() while spawning/running appends plugin.team.message.queued and appends
  to the in-memory queue; an idle member starts a new run. Queued messages are
  drained one per turn after the current run.
* cancel() cancels the task, clears the queue, records phase=cancelled and
  calls host.interrupt().
* close() cancels every task, awaits them, closes each host and releases the
  child native sessions (the lead session is owned by the runtime).
* Team members never notify the parent agent.

### 4.4 Restore after restart

restore() folds the parent plugin events (native session events, or
handle.fold() when the service was bound to a handle):

1. member.changed facts are grouped per member_id, last wins, in first-seen
   order; queued texts are collected and removed by a later delivered fact with
   the same text (same rule as the v4 fold);
2. tasks are folded per task id, newest revision wins;
3. spawning/running members restore as idle; failed and cancelled stay; any
   other phase restores as idle;
4. the child log is opened with sessions.native_open(session_id). When that
   fails, the member becomes failed with
   'cannot restore team member session: <error>' (no synthetic state);
5. members with a queued message are resumed: phase=running, a host is created
   lazily, host.restore() repairs an interrupted open turn in the child log
   (the native loop writes the aborted turn; no assistant content is invented),
   and the queued turn runs normally.

Malformed member facts are logged and skipped; a fact append failure is logged
rather than silently swallowed. A service without native_create/native_open
raises NativeTeamError (a TeamError) instead of falling back to v4.

### 4.5 Usage and trace

The native path writes no usage fact and no team/usage record. Each child host
receives the team UsageSink wrapped by a small adapter, so every finished child
turn reaches the ledger as a TurnUsage with source='subagent' and
session_id=<child session id>; token, latency and retry data stay in the trace
records. runs()/run_usage()/usage() keep the v4 API shape for the live process,
measured from host.usage() deltas per turn (including partial usage of failed or
cancelled turns). After a restart those in-memory runs are empty and usage() is
zero until new turns run: reconstructing historical usage is the job of traces
and child run facts, not of this service.

## 5. Behavior changes and known limitations (0.3)

* No team/usage records and no parent-side stream mirror on the native path.
* Team tasks are plugin.team.task facts, not core task.state; the board is
  service-owned and TaskInfo keeps the v4 id shape. The design note that
  suggested core task.state / task_<16 hex> is intentionally not followed so
  the tool and TUI API stay stable.
* Native member ids are member-<12 hex>; they can differ from child session ids
  (the MemberInfo carries both id and session_id).
* Child tool scoping: AgentBuild has no registry override, so the v4
  CHILD_DISABLED_TOOLS removal cannot be reproduced here. Native children are
  scoped by the native agent factory from their preset/tool configuration. A
  member built with the team preset can therefore still see team tools; closing
  that gap needs an agents-plugin API extension (tracked outside this
  component).
* Sandbox/approval services do not yet have native-session branches. A child
  gate is requested from the sandbox service when it accepts one; otherwise the
  lead permission object is reused and per-child policy inheritance is not
  applied.
* The v4 subagent/catalog and subagent/descriptor records are not written by
  the native path. Native team children are visible through plugin.team.* facts
  and native session listing (origin=subagent).
* Parent notifications are unchanged: none.

## 6. Tests

tests/team/test_native_manager.py covers spawn facts and child headers, the
native handle binding, roster/board/task facts, queue and cancellation,
usage-sink attribution, restore (roster, queue replay, board, missing child
session), error paths and the v4-import boundary of native_manager.py. The v4
suites (test_team_service, test_cold_resume, test_task_board, test_team_tools,
test_team_plugin, test_usage) remain the regression contract for the recorder
path.
