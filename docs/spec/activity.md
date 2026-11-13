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

## 4. Turn progress and thinking viewer (M1)

Reasoning history helper: src/deeppyro/tui/history.py.

    ReasoningStep(step: int, text: str)
    TurnReasoning(turn: int, steps: list[ReasoningStep], finished: bool)
    reasoning_from_session(session, *, last_turns: int | None = 1) -> list[TurnReasoning]
    reasoning_map(session) -> dict[int, TurnReasoning]
    reasoning_text(message) -> str

A user turn owns one TurnProgress widget, not one per assistant step. The
widget is a single line while streaming and a single summary line when the turn
ends; it is never a multi-line dump of the whole chain of thought.

Module: src/deeppyro/tui/widgets/turn_progress.py.

    TurnProgress(Static, classes="turn-progress")
    TurnProgress(turn: int = 0, reasoning: TurnReasoning | None = None)
    begin_step(step: int) -> None
    append(chunk: str) -> None
    finish() -> None
    reasoning -> TurnReasoning
    ThinkingRequested(Message)   # carries .reasoning

Streaming rendering: prefix + latest complete sentence from the current step,
trimmed to one terminal line (120 chars). Finished rendering: one summary such
as "· thinking · 3 steps · 12 sentences  (click to view)" and the full text is
available through the message. Clicking the line posts ThinkingRequested.

Thinking viewer: src/deeppyro/tui/screens/thinking.py,
ThinkingScreen(ModalScreen[None]) constructed with one or more TurnReasoning
objects. It renders turn headings, step headings and the complete reasoning
text in a scrollable area. Esc closes. /thinking opens the same screen for the
current turn (or the last N turns when given a number); clicking a summary opens
it for that turn. /resume replays collapsed summaries, not full text.

Conversation integration (Lead owns conversation.py and app.py):

* AssistantBlock loses its embedded ThinkingBlock; it keeps the label and the
  Markdown answer.
* ConversationView keeps one optional TurnProgress per user turn and exposes
  begin_step(turn, step), stream_thinking(chunk), finish_turn(final_text),
  add_history_reasoning(turn_reasoning), current_reasoning.
* add_tool_call/add_tool_result only finish the assistant text block; they do
  not end the turn progress.
* add_user starts a new user turn and resets the progress widget.

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
* TurnProgress: latest-sentence streaming, begin_step, finish summary, click
  message, load from TurnReasoning, empty hidden.
* ThinkingScreen: full text per step, multi-turn headings, Esc.
* Conversation/app: one progress per user turn, tool calls do not finish it,
  final event closes it, /thinking /plan /jobs /team commands, history replay.
* Agent loop: step event emitted after step/start and before the model stream.
* Jobs: service lifecycle records, job tools, background shell, headless
  cancellation (tests/jobs, tests/tools/test_shell_jobs.py,
  tests/tui/test_jobs_screen.py, tests/tui/test_headless.py).
* Team: spawn/send/wait/cancel, task board transitions, record shapes, nine
  tools, TUI binding and shutdown (tests/team, tests/tui/test_team_screen.py).
