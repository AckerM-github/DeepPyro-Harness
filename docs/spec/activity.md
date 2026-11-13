# Activity, jobs and team surfaces

Status: M1, M2 and M3 implemented. This document is the contract shared by the
kernel services, the agent loop and the terminal UI. It covers plan/todo,
background jobs and the agent team; the same projection populates the activity
bar above the conversation.

## 1. Design rules

* The durable session log is the single source of truth. The activity service
  never owns new state: it folds records such as todo/write, job/registered,
  job/settled, team/member, subagent/descriptor, subagent/catalog and plan/mode.
* Every event is appended before it is emitted (existing loop rule). The UI may
  refresh state on any event and on turn boundaries.
* Reasoning is already persisted inside assistant/message content parts. The
  UI reads it back; there is no second thinking store.
* Missing or malformed records degrade to an empty segment. A bad plugin or
  malformed todo payload must never break the conversation.

## 2. Activity service (M1)

Module: src/deeppyro/builtin/activity.py; plugin id: activity; service name:
activity. It is a core bundle row and has no dependencies.

Data exposed to the TUI:

    TodoItem(content: str, status: str)          status in pending/in_progress/completed
    JobInfo(id, kind, label, status, owner, started_at, finished_at, call_id)
    AgentInfo(id, name, description, phase, provider, model, parent)
    ActivityState(todos, jobs, agents, plan_mode, revision)
        has_items -> bool
    ActivityService.state -> ActivityState
    ActivityService.refresh(session) -> ActivityState
    ActivityService.subscribe(listener) -> unsubscribe

Pure helpers: parse_todos(raw), plan_counts(todos), current_todo(todos).

Record projection:

| record type | fields used |
| --- | --- |
| todo/write | data.todos[{content,status}] |
| job/registered | data.jobId, kind, label, owner, startedAt, toolCall.callId |
| job/settled | data.jobId, status, finishedAt; keeps kind/label/owner from register |
| team/member | data.member{id,name,description,phase,provider,model} |
| subagent/descriptor | data.childId, label, mode, agentProvider, agentModel |
| subagent/catalog | data.childId, label, mode |
| plan/mode | data.mode |

Activity revision increments only when the payload changes. Listeners are
notification-only; exceptions in one listener are swallowed.

## 3. Activity bar (M1)

Module: src/deeppyro/tui/widgets/activity_bar.py. It is a pure view; it never
talks to the agent or the log. The app refreshes it from runtime.activity.

Layout: one row, directly below the page Header and above ConversationView.
Three segments, rendered only when non-empty; the whole row is hidden when all
three are empty.

* plan: completed/total plus the current in-progress (or first pending) item.
* jobs: number of running jobs (the detail lives in JobsPanelScreen).
* agents: number of known team members/subagents (the detail lives in
  TeamPanelScreen; the task board has its own screen).

Clicking a segment emits ActivityBar.SegmentClicked(segment) where segment is
one of plan/jobs/agents. The app maps plan to PlanScreen, jobs to
JobsPanelScreen (/jobs) and agents to TeamPanelScreen (/team). Public API:

    ActivityBar.update_state(state: ActivityState) -> None
    ActivityBar.plan_text / jobs_text / agents_text  (properties for tests)

Pure helpers plan_segment(state), jobs_segment(state), agents_segment(state)
are preferred for easy tests. CSS lives in DEFAULT_CSS; do not edit theme.tcss
from this task.

Plan screen: src/deeppyro/tui/screens/plan.py, PlanScreen(ModalScreen[None])
constructed with the current todos, grouped in_progress -> pending -> completed,
Esc closes, empty state says no plan yet.

## 4. Agent process (M1)

History helper: src/deeppyro/tui/history.py.

    ReasoningStep(step: int, text: str)
    TurnReasoning(turn: int, steps: list[ReasoningStep], finished: bool)
    TurnTool(call_id, name, arguments, result_text="", is_error=False, has_result=False)
    ProcessEntry(kind="step" | "tool", step=0, text="", tool: TurnTool | None = None)
    TurnProcess(turn: int, reasoning: TurnReasoning, entries: list[ProcessEntry],
                tools: list[TurnTool], finished: bool)
    reasoning_from_session(session, *, last_turns: int | None = 1) -> list[TurnReasoning]
    reasoning_map(session) -> dict[int, TurnReasoning]
    processes_from_session(session, *, last_turns=None) -> list[TurnProcess]
    process_map(session) -> dict[int, TurnProcess]
    reasoning_text(message) -> str

A user turn owns one AgentProcess widget, not one per assistant step. Reasoning
steps and tool calls are interleaved in the order they happened; they are all
part of the same process. While streaming only one header line is visible: the
latest complete sentence of the current step (one terminal line, 120 chars) or
the tool call that is currently running. The detailed trail body is collapsed by
default and expands when the header is clicked. When the turn ends the process
collapses to a single summary line and is never a multi-line dump of the chain
of thought.

Module: src/deeppyro/tui/widgets/agent_process.py.

    AgentProcess(Vertical, classes="agent-process")
    AgentProcess(turn: int = 0, reasoning: TurnReasoning | None = None)
    begin_step(step: int) -> None
    append_thinking(chunk: str) -> None
    add_tool_call(call_id: str, name: str, arguments: str) -> None
    add_tool_result(call_id: str, is_error: bool, text: str) -> None
    finish() -> None
    toggle() -> None
    ThinkingRequested(Message)   # carries .reasoning; posted by step line clicks

    ToolProcessLine(Vertical, classes="process-tool")
    ToolProcessLine(call_id: str, tool_name: str, arguments: str)
    set_result(text: str, is_error: bool) -> None
    toggle() -> None

Streaming rendering: header line with the latest sentence plus one line per
step and one line per tool call (running "-> name summary", done "ok name
summary . result preview", failed "x name failed"). Finished rendering: one
summary such as "· agent process · 3 steps · 2 tools · 12 sentences
(click to view)"; the body is hidden. Clicking the header toggles the body;
clicking a step line posts ThinkingRequested; clicking a tool line expands its
full arguments and result.

Thinking viewer: src/deeppyro/tui/screens/thinking.py,
ThinkingScreen(ModalScreen[None]) constructed with one or more TurnReasoning
objects. It renders turn headings, step headings and the complete reasoning
text in a scrollable area. Esc closes. /thinking opens the same screen for the
current turn (or the last N turns when given a number); clicking a step line
opens it for that turn. /resume replays the collapsed process, not full text.

Conversation integration (Lead owns conversation.py and app.py):

* AssistantBlock only renders the label and the Markdown answer.
* ConversationView keeps one optional AgentProcess per user turn and exposes
  begin_step(turn, step), stream_thinking(chunk), finish_turn(final_text),
  add_history_process(turn_process), current_reasoning, process.
* add_tool_call/add_tool_result are forwarded into the current AgentProcess;
  they only finish the assistant text block, never the process.
* add_user starts a new user turn, finishes the previous process and mounts a
  fresh one.

## 5. Jobs (M2)

The jobs service owns background processes; the session log stores job
lifecycle records. Module: src/deeppyro/jobs/ (types.py, manager.py), plugin
src/deeppyro/builtin/jobs.py (service name jobs, plugin id jobs).

    JobsService.start(kind, label, runner, *, owner="", call_id="", record=None,
                      timeout_ms=None) -> JobHandle
    JobsService.get(job_id) -> JobHandle | None
    JobsService.list() -> list[JobHandle]
    JobsService.output(job_id) -> str
    JobsService.wait(job_id, timeout=None) -> JobResult
    JobsService.kill(job_id, reason=None) -> bool
    JobsService.cancel_owner(owner) -> int
    JobsService.cancel_all() -> int

Job id format is <kind>-<16 hex>. job/registered is appended before the
producer starts; job/settled is appended exactly once with status in
running/completed/failed/cancelled plus cause, detail and producerReported.

Tools: job_output, job_kill, job_list; shell run_in_background returns a job id
immediately. ActivityBar.jobs shows running jobs and JobsPanelScreen (/jobs,
clicking the jobs segment) lists every job with status and time. Headless
run_once calls cancel_owner(session_id) before returning, so one-shot runs never
leave a background process behind.

## 6. Agent team (M3)

Team service: members (up to 8), shared task board, messages, child sessions,
wait/cancel and approval forwarding. Module: src/deeppyro/team/ (types.py,
manager.py), plugin src/deeppyro/builtin/team.py (service name team, plugin id
team, inject agents/sessions/sandbox/tools).

    TeamService.configure(*, cwd, provider, registry, permission, model,
                          context_window, preset="default", preset_revision=None,
                          services=None, ask=None)
    TeamService.bind_session(session)
    TeamService.spawn(name, description, prompt, *, provider=None, model=None,
                      preset=None) -> MemberInfo
    TeamService.send(member_id, message) -> "started" or "queued"
    TeamService.wait(member_id, timeout_s=None) -> MemberInfo
    TeamService.cancel(member_id) -> bool
    TeamService.members() / get_member(id) / close()
    TeamService.create_task(subject, description="", *, blocked_by=(),
                            write_scopes=()) -> TaskInfo
    TeamService.update_task(task_id, action, *, ...) -> TaskInfo
    TeamService.tasks() / get_task(id)

Actions: claim, start, complete, reopen, edit. Records mirror DSH: team/member,
team/task, team/message/queued, team/message/delivered, subagent/descriptor,
subagent/catalog plus the existing subagent lifecycle. Each member has its own
child session (session header parentSession/delegationDepth=1/isSeeded) and its
own AgentSession; the Lead remains the only approver. Nine tools (team_spawn,
team_send, team_wait, team_cancel / team_task_create, team_task_update,
team_task_list, team_members, team_tasks) are registered by the team plugin.
TeamPanelScreen (/team, clicking the agents segment) shows members and tasks;
TaskBoardScreen shows the full board. The TUI runtime reconfigures and
rebinds the service on every attach, and closes it during shutdown.

## 7. Test expectations

* Activity service: todo/job/team record projection, malformed payloads,
  revision stability, subscribe/unsubscribe, plan_counts/current_todo.
* ActivityBar: empty state hides the row, each segment, click message.
* PlanScreen: grouped rows, empty state, Esc.
* AgentProcess/ToolProcessLine: latest-sentence streaming, begin_step, finish
  summary, click message, tool line summary/result, expand detail, empty hidden.
* ThinkingScreen: full text per step, multi-turn headings, Esc.
* Conversation/app: one process per user turn, thought and tool lines in order,
  tool calls do not finish it, final event collapses it, /thinking /plan /jobs
  /team commands, history replay.
* Agent loop: step event emitted after step/start and before the model stream.
* Jobs: service lifecycle records, job tools, background shell, headless
  cancellation (tests/jobs, tests/tools/test_shell_jobs.py,
  tests/tui/test_jobs_screen.py, tests/tui/test_headless.py).
* Team: spawn/send/wait/cancel, task board transitions, record shapes, nine
  tools, TUI binding and shutdown (tests/team, tests/tui/test_team_screen.py).
