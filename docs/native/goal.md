# Native goal rounds (schema 1)

Status: **current (0.3.0, native-only).** The native round driver is the only
goal-round implementation: it advances goal rounds over a schema 1
`NativeSession` and a `NativeAgentHost`, and the core bundle row
`goal-round-driver` mounts it. The v4 driver was deleted with the v4 stack; the
comparison table below is 0.2.0 context. No schema change was needed: every round
is one ordinary `run.*`/`turn.*` sequence plus the existing `goal.changed`
fact.

Contract of record: [docs/spec/goal.md](../spec/goal.md) (user-visible
behaviour, frozen at M0). Design intent: [native-capabilities.md](../design/native-capabilities.md)
section 5. Session facts: [schema.md](schema.md).

## What changed relative to the v4 driver (0.2.0 comparison, historical)

| Concern | v4 round driver | native round driver |
| --- | --- | --- |
| Round admission | user message queued in the agent inbox; `goal/change` record admitted by `replay` | `GoalService.admit_round` appends one `goal.changed` fact with `rounds_started`; it is the only writer that advances rounds |
| Round counter | process memory (`GoalState.rounds_started`) | folded from `goal.changed` facts; the driver holds no counter |
| Wake up | inbox events, pre-step fence, hook listeners | `GOAL_BUS_EVENT` listener plus an explicit `request_drive`; one coalesced asyncio task per host |
| Round prompt | `render_goal_round_prompt(goal, round)` | byte identical, sent by `NativeAgentHost.send(prompt, source=GoalSource(...))` |
| Round identity | source kind `goal` on the queued v4 message | `GoalSource(goal_id, revision, round)` on the `user.message` fact, so rounds join the transcript and trace |
| Pause / stop | interrupt the busy agent, hint only arms once idle | `GOAL_BUS_EVENT` with a non-active phase interrupts the running host; the goal snapshot is already the durable truth |
| Budget | one `AgentLoop.run` per round, per-round `BudgetLimits` | unchanged: each round is a fresh `AgentLoop.run` with the host budget; `max_goal_rounds` only caps rounds |
| Failures | `queue-failed`, `prompt-rejected` blocked reasons | `round-limit` and budget-derived reasons only; there is no inbox or prompt admission in the native loop |
| Restart | armed state could resume from records | activation is process local and starts `disarmed`; the driver never auto-runs on startup, and rounds resume only after an explicit arm plus drive |

## Durable facts

The driver writes no facts itself. `GoalService` owns all writes:

- `goal.changed` is exactly `{goal, rounds_started}` (schema 1). `goal` is
  `null` after `clear`, otherwise the snake_case snapshot
  `{id, revision, objective, phase, max_goal_rounds, blocked_reason?}`.
  A round start appends the same goal snapshot with `rounds_started = round`.
  Revision does not move on a round; event order is the round order.
- A round body is the ordinary native turn: `run.started`, `turn.started`,
  `step.*`, `assistant.message`/`tool.call`/`tool.result`, `turn.ended`,
  `run.finished`, and the `user.message` whose `source.kind` is `goal`.
- Pause, resume, complete, block, edit, create and clear keep using the same
  `goal.changed` fact through the existing goal verbs.

Token, cost, latency and retry detail stays in the trace store; no such
detail is written to the session log.

## Driver loop

`NativeRoundDriver.drive(host)` runs one round at a time and re-reads state
before every round, so a crash or a restart can never skip or replay a round:

1. If the host is busy, return: a user turn always wins.
2. If the log ends with an open `run.started`, return: resuming is a user
   decision, not a driver decision.
3. Read `GoalService.state(session)` and `activation(session)`. Return unless
   the goal is active and armed.
4. If `rounds_started >= max_goal_rounds`, block the goal with reason
   `round-limit` and the v4 message `Goal reached its configured limit of N rounds.`
5. `round = rounds_started + 1`; `admit_round` appends the durable fact; the
   returned pre-round snapshot renders the prompt.
6. `host.send(prompt, source=GoalSource(...))` and await the run result.
7. Map the outcome: `completed` loops for the next round; `cancelled` after a
   goal change stops without blocking (the goal verb already owns the
   outcome); `cancelled` from a user abort disarms; a budget failure blocks
   the goal with a budget-derived reason; any other failure disarms.

Blocked reasons produced by this path:

| Code | Trigger | Message |
| --- | --- | --- |
| `round-limit` | `rounds_started >= max_goal_rounds` | `Goal reached its configured limit of N rounds.` |
| `max-steps` | per-round step budget | `Goal round N exceeded its max_steps budget (limit X).` |
| `max-tokens` | per-round token budget | `Goal round N exceeded its max_tokens budget (limit X).` |
| `max-wall-seconds` | per-round wall-clock budget | `Goal round N exceeded its max_wall_seconds budget (limit X).` |
| `max-cost` | per-round cost budget | `Goal round N exceeded its max_cost budget (limit X).` |

`queue-failed` and `prompt-rejected` stay reserved for the v4 path: the native
driver has no queue or admission step that could fail that way.

## Activation, adoption and restart

- Activation remains process local (`armed`/`disarmed`). `adopt(host)` always
  disarms, so attaching a driver or loading a plugin never starts a round.
- A restarted session therefore comes up disarmed. `rounds_started` is in the
  folded state, so the next explicit drive starts at `rounds_started + 1`.
- One host per session id is adopted; a second driver cannot drive the same
  session concurrently. `release(host)` and `detach()` disarm and interrupt.

## API surface

Native additions live beside the v4 code; nothing v4 was removed or changed:

- `deeppyro.goal.service.GoalService`
  - `state(session)` folds schema 1 `goal.changed` facts for a native session
    (the v4 record path is unchanged for v4 sessions).
  - `admit_round(session, ref, round) -> GoalSnapshot` is the only writer that
    advances `rounds_started`; it verifies active phase, same id/revision and
    `round == rounds_started + 1`, then appends the fact and returns the
    pre-round snapshot.
- `deeppyro.goal.native_driver`
  - `NativeRoundDriver(*, hooks, goals=None)` with `attach()` (returns the
    disposer), `detach()`, `adopt(host)`, `release(host)`, `state_for(id)`,
    `states()`, `request_drive(host) -> Task | None` and `drive(host)`.
  - `NativeRoundHost` and `GoalFacts` protocols; `is_native_host(obj)` duck
    check used by plugin wiring.
- `deeppyro.builtin.goal_round_driver.NativeGoalRoundDriverPlugin`
  - `plugin_id = goal-round-driver` (service `goal-round-driver-native`), injects
    `hooks` and `agents`, gated by the `[goal] driver` boolean.
  - Publishes the driver as service `goal-round-driver-native`; adopts native
    hosts from `agents.live_agents()` and `agents.on_created`.

## Wiring and remaining limits

- The core bundle row `goal-round-driver` (`bundles/core/bundle.toml`) mounts
  `deeppyro.builtin.goal_round_driver:NativeGoalRoundDriverPlugin`; there is no
  separate v4 row or v4 driver left.
- The driver never starts a round by itself after load: a caller must adopt
  the host, arm the goal and request a drive (the `GOAL_BUS_EVENT` listener
  only reacts to goal changes).
- There is no legacy inbox fence (the v4 inbox was deleted). A user turn that is
  already running wins; the driver waits for the next request instead of
  queueing beside it.
- Activation is not persisted; this is intentional and matches the historical
  process-local contract, so restarts are always disarmed.
- Only `goal.changed` carries round state. Per-round token, latency and retry
  detail stays in the trace store and is not reconstructible from the session.
- TUI, headless and the Web UI all drive the native host, and this driver is the
  only goal-round path. The 0.2.0 `--legacy-v4`/v4 driver alternative was removed
  in 0.3.0.

## Tests

[`tests/goal/test_native_driver.py`](../../tests/goal/test_native_driver.py)
covers multi-round progress from admitted facts, byte-identical prompts and
`GoalSource` rounds, round-limit blocking, budget blocking per budget kind,
restart/disarm/resume, adoption, busy-host deference, pause interrupt,
request coalescing, detach interrupt, fact replay into a fresh service, no
synthetic facts (goal facts are the only events the driver path writes), duck
typing, the plugin gate, and one end-to-end two-round run on the real
`NativeAgentHost` over a scripted provider.

