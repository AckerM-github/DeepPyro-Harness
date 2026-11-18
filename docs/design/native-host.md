# Native Agent Host

Status: **implemented (0.3.0)**. This note started as phase 1 of KI-001 (TUI native
migration); the bridge is now the production path — TUI, headless and the Web UI all
drive `AgentLoop` over `NativeAgentHost`, and the v4 read/append surface it once
mirrored was removed with the v4 stack. The text below is the design record; where it
describes v4-shaped seams, read those as plan-time context, not current API.

`src/deeppyro/agent/native_host.py` is the reusable seam between a
long-lived interactive session and the one-shot native agent loop. It owns one
`NativeSession` and drives repeated `AgentLoop.run` calls on it, while
exposing the v4-shaped read/append surface that the TUI services already call.

## Layers

    TuiRuntime
      -> NativeAgentHost        one session, many turns, streaming, usage
         -> NativeSessionHandle  v4-shaped adapter over schema 1
         -> AgentLoop            fresh run per send(); folds the session each run

The host imports neither `core.wire` nor `deeppyro.tui` and keeps no
module-level mutable state.

## NativeAgentHost

| Member | Contract |
| --- | --- |
| `send(text)` | Starts a **fresh** `AgentLoop.run(text)` on the same open session. Streams through `AgentHooks`; returns the loop's `RunResult`. Raises if the session is closed, the prompt is empty, or a send is already in flight. Drains queued notices before the run starts. |
| `interrupt()` | Cancels the in-flight run task; the loop persists `turn.ended`/`run.finished` with reason `{"kind": "aborted", "by": "user"}`. Returns whether a run was actually interrupted. Idempotent. |
| `aclose()` | Cancels any in-flight run, closes the handle and releases the session lock. Idempotent; reads still work, writes do not. |
| `usage()` | `{input, output, cached}` totals accumulated across every run, including aborted ones (from the loop's `TurnUsage` service reports). |
| `context_tokens()` | Live context size: input tokens of the most recent model call, not a cumulative total. |
| `messages()` / `derive_messages()` | Transcript rebuilt from `fold(header, events)`; `derive_messages(strict=True)` raises if the fold reported issues. |
| `handle` | The `NativeSessionHandle`; `native_session`, `session_id`, `busy`, `closed` are also exposed. |

Multi-turn context reuse is implicit: `AgentLoop.run` rebuilds its plan from the
folded session on every step, so the second `send` sees the first turn without any
host-side conversation cache.

## NativeSessionHandle

A thin, documented adapter, not a second session implementation. It never
invents schema vocabulary; everything it writes is a known `EventKind` with
schema-validated data.

| Member | Contract |
| --- | --- |
| `session_id` | `str`. |
| `header` | A cached `NativeHeaderView` with stable identity: `id`, `created_at`, `cwd`, `preset`, `parent_id`, `origin`, `delegation_depth`, plus read-only v4 aliases (`createdAt`, `isSeeded`, `delegationDepth`, `agentPreset`, `parentSession`, `model`, `system`). The identity guarantee exists because the TUI asserts that header objects are not rewritten. |
| `append(record_type, data)` | Maps a v4 record type to a native `EventKind` through an explicit table and returns the new sequence number. Unknown types raise `UnsupportedRecordTypeError` (a `ValueError`) before anything is written. |
| `inject(text, source=...)` | Queues a notice for the next run's context in a bounded deque (`notice_limit`, default 64) and returns whether it was queued. Overflow increments `dropped_count` and is visible via `pending_notices`/counters; a notice is never silently discarded. |
| `usage()` | `{input, output, cached}` int totals. |
| `context_tokens()` | Live prompt size (`int`). |
| `derive_messages()` / `messages()` | Folded transcript as native `Message` objects. |
| `aclose()` | Closes the underlying `NativeSession`. |

### Record mapping table

| v4 record type | Native event | Notes |
| --- | --- | --- |
| `agent-preset/selected` | `preset.selected` | Payload already native-shaped. |
| `model/selection` | `model.selected` | `{provider, model}`. |
| `sandbox/mode` | `sandbox.mode` | `{mode}`. |
| `approval/policy` | `permission.policy` | `{policy}`. |
| `working-directory/change` | `workspace.cwd_changed` | `{cwd}`, absolute. |
| `session/title` | `session.title` | v4 `source` object flattened to `"user"`/`"llm"`. |
| `todo/write` | `todo.written` | `{todos}`. |

Deliberately unmapped: v4 message/streaming/turn records (the native loop owns
those), `permission/preset` (v4 preset names have no schema-1 equivalent; the two
knobs are recorded as `permission.policy`/`sandbox.mode`), and goal/plan/
deliverable records (see below). Extending the table means adding a schema
event type, which is owned by the schema file, not this module.

## What the host deliberately does not cover

- **Team mode and subagent child sessions.** `preset` is stored on the header, but
  team routing, child-session creation, and per-member transcripts stay in the v4
  layer until their own migration phase. The host drives exactly one session.
- **Projections.** Compaction, summaries, and message replacement are loop/session
  concerns; the handle only folds what is already recorded.
- **Goal, plan, todo orchestration.** `todo/write` can be recorded for parity, but
  the goal/plan state machines and their prompts are not owned here.
- **Wiring.** No TUI service is imported or modified; the TUI switch is phase 2
  behind the `DEEPPYRO_TUI_NATIVE=1` flag.
- **Persistence policy.** The host takes an open `NativeSession`; creating or
  discovering session files stays with the session store/index.

## How the TUI will use it (phase 2)

1. `new_session` creates a `NativeSession` and a `NativeAgentHost`; the runtime keeps
   `runtime.agent = host` and `runtime.session = host.handle`.
2. `TurnLayer` calls `await host.send(text)` and `await host.interrupt()`; streaming
   events reach the TUI through an `AgentHooks` adapter, as headless already does.
3. Status widgets read `host.usage()`, `host.context_tokens()`, and
   `host.derive_messages()`; history replay uses the same fold.
4. Services that still call `session.append(record_type, data)` or
   `session.inject(text, source=...)` (for example the working-directory service)
   keep working through the handle, with unknown types failing loudly rather
   than being written as ad-hoc records.

Switching sessions closes the old host (`handle.aclose()`), which releases the
session lock; no dual-write to the v4 log is performed.
