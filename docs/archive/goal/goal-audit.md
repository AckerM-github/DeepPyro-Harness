# Goal upgrade: audit report (M6)

> **历史文档（非契约）**。本文是 0.2.0 之前的研究/计划记录，保留用于追溯设计依据；
> 不再维护，字段、路径与结论可能已过期。当前入口见 [docs/README.md](../../README.md)。

Milestones M1 through M6 of `docs/plan/goal-upgrade.md` are implemented. This is
the closing audit: what shipped, what deliberately differs from DSH, and how the
result was gated.

## 1. What shipped

| card | commit | evidence |
| --- | --- | --- |
| M1 snapshot fold + CAS | `2e85324` | `goal/types.py`, `goal/replay.py`, `goal/service.py`, `tests/goal/test_goal_state.py`, `tests/goal/test_goal_replay.py` |
| M3a session subscription + inbox bus | `6afb786` | `Session.subscribe`, `agent/inbox/inserted\|claimed\|discarded`, `tests/session/test_session_subscribe.py`, `tests/agent/test_inbox_events.py` |
| M2b turn conclusion + policy section | `5997c58` | `ToolContext.conclude_turn`, `AgentSession._request_turn_conclusion`, `GOAL_POLICY`, `tests/agent/test_turn_conclude.py`, `tests/goal/test_goal_policy.py` |
| M2a three tools + authority | `ee2c536` | `tools/builtin/goal.py`, `tests/tools/test_goal_tools.py`, `tests/goal/test_goal_tools.py` |
| M3b round driver + fence | `9b77a21` | `goal/round_driver.py`, `builtin/goal_round_driver.py`, `tests/goal/test_round_driver.py`, `tests/goal/test_fence.py` |
| M4 `/goal` command + TUI | `ee2c536` | `commands_builtin.goal`, `tui/app.py` `goal_command`, `tui/session_view.py`, `tests/tui/test_goal_command.py` |
| M5 docs + payload freeze | this commit | `docs/spec/events.md` section 11, `docs/spec/loop.md` section 13, README "Goals and automatic rounds" |
| M6 integration | this commit | `tests/goal/test_driver_integration.py`, this report |

## 2. Behaviour that matches DSH

- Snapshot and tombstone records with revision CAS, `seen_ids` preventing an id
  from coming back after `clear`, and `updatedAt` monotonicity.
- The verb table (`create`/`edit`/`pause`/`resume`/`complete`/`block`/`clear`)
  with the nine `GOAL_*` error codes.
- Process-local activation: every new agent starts disarmed, `create`/`resume`
  arm, the closing verbs disarm.
- Round messages are `user/message` records whose source is
  `{kind: "goal", goalId, revision, round}`, and only the fold advances
  `roundsStarted`.
- The pre-step fence: a stale reservation restores the other claimed messages,
  rejects the step, and re-drives.
- Tool authority codes (`GOAL_TOOL_AGENT_REQUIRED`, `GOAL_TOOL_DRIVER_REQUIRED`,
  `GOAL_TOOL_AUTHORITY_REQUIRED`, `GOAL_TOOL_RESUME_PAUSED`,
  `GOAL_TOOL_BLOCK_THRESHOLD`, `GOAL_TOOL_INVALID_UPDATE`).

## 3. Documented divergences

1. `goal/changed` carries one extra `session` key. DSH publishes per agent; our
   bus is global, so the frame names its session. Everything inside `change` is
   byte-for-byte the DSH object.
2. `goal/activation-changed` is emitted by the service instead of carrying a
   live agent handle, and it fires only on a real transition.
3. There is no `currentInitiator`/`roots()`: the driver only interrupts the
   agent it drives, and the goal tools decide "direct human turn" from the open
   turn's records plus the session header (`origin == "subagent"` means a
   subagent).
4. `complete`/`blocked` close the turn through `ToolContext.conclude_turn`
   instead of DSH's `deferContext` wrap-up message; the wrap-up text is
   therefore not injected as a user message.
5. No `subagents.waitForChildren` gate, no `sessions.flush` (appends are already
   durable), and no `agent/created|disposed` producers - the driver uses
   `AgentsPlugin.on_created`/`live_agents`.
6. Inbox events are mirrored onto the bus asynchronously; the fence therefore
   accepts a matching `queued` attempt and promotes it to `claimed` in place.
7. The round driver is opt-in (`[goal] driver`), and detaching interrupts a
   running round with `clear_inbox=False` without waiting for idle.
8. `/goal` does not live-refresh from `goal/changed`; the TUI re-reads the view
   when the command runs.

## 4. Compatibility

- With `[goal] driver` unset the driver plugin registers no listener and
  publishes nothing; `tests/goal/test_driver_integration.py` asserts a session
  with an armed goal stays untouched.
- Legacy `{"goal": "<string>"}` records fold into an `id="legacy"` active
  snapshot without rewriting the log, and `{"goal": null}` folds to "no goal".
- The old model-facing `goal` tool still resolves and returns a deprecation
  message that points at `get_goal`/`create_goal`/`update_goal`.

## 5. Gates

- `pytest -o addopts= -q`: 3255 passed.
- `ruff check src tests` and `ruff format --check src tests`: clean.
- `node scripts/extract_dsh_events.mjs --check`: catalogue up to date.
- `tests/core/test_events.py` pins the bus table: 24 implemented, 91 reserved,
  115 total, 10 `internal/*`.

## 6. Open risks

- A goal round is a full turn: a long goal costs one model request per round,
  plus its tool traffic.
- `resume` re-arms only in the process that issued it; a restarted process
  always starts disarmed, by design.
- The driver's checkpoint is a no-op, so a crash between "round queued" and
  "round admitted" is settled by the fence on the next drive instead of a flush.
