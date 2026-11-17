# ADR 0001 — Three-track native agent: pluginization, Pythonization, AI-nativeness

- **Status:** Accepted; M0 and M1 deliverables complete (2026-11-17); M2 loop/contract scope complete (2026-11-17, §6.2); decisions locked in §7.
- **Date:** 2026-11-17
- **Owners:** Lead (integration), lane P/Y/A owners as assigned in §6
- **Supersedes:** the implicit "DSH-compatible port" direction recorded in `docs/spec/INTERFACES.md` §0
- **Related:** `D:/dshworkspace/deeppyro-native-python-agent-research.md` (three-track revision), `docs/native/schema.md` (native schema 1, M0)

## 1. Context

DeepPyro started as a Python port of DeepSeek Harness (DSH). The plugin kernel is already native: the vendored cordis port is gone, `src/deeppyro/kernel/` runs a 986-line `RuntimeContext`/`Fiber`/`ServiceContainer`/`mount` implementation, and 3352 tests pass. What remains DSH-shaped is the **domain contract**:

- `src/deeppyro/core/wire.py` defines DSH v4 message and record models with camelCase fields.
- `src/deeppyro/session/events.py` validates 62 DSH record types; `surfaceOp` and `sourceEventSeqs` encode interval-based surface rewriting.
- `src/deeppyro/core/events.py` declares 115 event names, 91 of which are reserved and never produced; `core/event_catalog.py` mirrors 94 DSH bus events and asar call sites.
- 37 source files import `deeppyro.core.wire` (M2 adds exactly one documented provider-adapter importer, §6.2); 27 call sites still use string service lookup; the config model is camelCase with `extra="allow"`.
- The agent turn/step loop in `src/deeppyro/agent/session_turn.py` still infers phases from record sequences and uses a collaborative `asyncio.Event` for cancellation.

Those contracts are not just naming. They force every new feature to answer "what does DSH do?", make replay and trace correlation expensive, and keep TypeScript runtime concepts in a Python product. At the same time, DSH contains design assets worth keeping: everything-is-a-plugin, declarative bundles, service contracts with lifecycle, durable-log-first, capability contribution, adapter boundaries, least privilege, and replayability.

The product goal is therefore three things at once, not three phases:

| Track | Question | Deliverable |
|---|---|---|
| **P — Pluginization** | Who provides a capability, how is it composed, how is it governed? | API v3, one mount shape, typed `inject`, bundle schema, `CapabilityManifest` |
| **Y — Pythonization** | How are data and control flow expressed in Python? | native `Message`, session schema 1, `EventKind`, `AgentLoop`, `Protocol`, `dataclass`, `StrEnum`, `TaskGroup` |
| **A — AI-nativeness** | How is a run observed, resumed, evaluated and improved? | trace/replay, `ContextPlan`, `RunState`/checkpoint, eval, memory, multi-agent |

The failure mode this ADR prevents is treating them sequentially: change the plugin API, then change the session format, then discover trace fields are missing and migrate every record again.

## 2. Decision

### D1 — Adopt the three-track parallel break (report option B)

Use the existing kernel, tools, permissions, TUI/headless, MCP/PTC/team/subagent, goal/memory/skills/compact/schedule, and the test suite as assets. Replace the DSH domain contract in one deliberate break. Reject option A ("de-brand only") because it keeps DSH's loop and wire semantics; reject option C ("rewrite everything") because it discards working Python assets.

### D2 — Zero compatibility for the wire contract

The product reads and writes **only native session schema 1**. It does not ship a v4 reader, a `V4Codec`, a dual-write path, a `dpyro migrate` command, or a legacy config reader. Old data, if it must be preserved, is exported by a one-off script outside the repository; that script is not a product feature and is not covered by CI.

Zero compatibility applies to:

- `session.v4.jsonl` and `session.v4.jsonl.zstd`;
- camelCase `config.toml` keys and `extra="allow"` acceptance;
- external plugin API v1/v2 (hard cut to v3, with a migration note);
- DSH event catalog and TypeScript fixtures;
- `surfaceOp`, `sourceEventSeqs`, `ignorable`, interval-based surface folding.

It does **not** apply to DSH design ideas or components; see D3.

### D3 — Keep DSH design assets; remove DSH compatibility layers

Retained and adapted:

1. **Everything is a plugin.** The core bundle remains a list of plugin rows; no privileged domain module.
2. **Declarative composition.** `bundle.toml` keeps `before`/`after`/`critical`/`group`/`include`/`requires`, typed by pydantic rather than bare dicts.
3. **Service contracts and lifecycle.** `Service`, typed lookup, inject-wait, effect disposal and restart semantics remain.
4. **Durable log first.** Append to disk before publishing UI/live events.
5. **Capability contribution.** Tools, hooks, TUI slots, MCP servers and subagents contribute through plugins.
6. **Adapter boundary.** Providers, MCP, PTC and subagents translate at the edge; the loop uses native types.
7. **Least privilege and replayability.** Permission/sandbox gates stay; a run must be replayable from session log + recorded model/tool I/O.

Removed or archived:

- DSH v4 record vocabulary and interval surface;
- DSH's 62-type session record vocabulary, 94-name bus catalog and asar-derived event names;
- camelCase wire/config fields;
- cordis runtime vocabulary and `.py` path loading;
- TypeScript/Node fixtures and the Node fixture generator;
- legacy reader, `V4Codec`, `migrate` CLI.

The test is simple: **if a decision only exists to read or write DSH data, remove it; if it makes plugin composition, logging or permission safer, keep it.**

### D4 — One contract across all three tracks

There is exactly one session schema, one event-kind set, and one plugin contract. No track may define a second message/event model. Ownership of the seams is fixed:

| Seam | Owner | Rule |
|---|---|---|
| session schema | Y | A may add correlation fields; P does not touch record shapes |
| `EventKind` | Y | P's custom events carry a plugin namespace; A's trace stays out of the session log |
| `AgentLoop` | Y | A injects `ContextPlan`/`TraceSink`/`Budget`; P exposes the loop as a service |
| `api/plugin.py` | P | Y keeps types correct; A adds optional hooks without breaking v3 |
| `CapabilityManifest` | P | Y provides the dataclass; A consumes risk/cost |
| `ContextPlan` | A | Y renders it; P does not inspect context internals |

### D5 — Milestones and hard gates

| Milestone | Scope | Exit gate |
|---|---|---|
| **M0 — freeze and baseline** | this ADR, `docs/native/schema.md`, 10 recorded LLM/tool cases, decision review | existing four gates green; **no product code changes** |
| **M1 — native contract + observability skeleton** | `core/messages.py`, `session/schema.py`, `NativeCodec`, fold; `observability/trace.py`, `ModelCall` | native session round-trip stable; v4 explicitly rejected; tool-schema snapshot byte-identical; trace smoke |
| **M2 — native loop + ContextPlan + checkpoint** | `agent/loop.py`, typed hooks, `RunState`/checkpoint, budgets, cancellation; headless first, TUI second | 10-case differential replay matches; kill/resume test; budget denial enforced; pyright baseline does not grow |
| **M3 — API v3 + capability governance** | one mount, `setup(self)`, typed `inject`, entry points, `CapabilityManifest`, service typing, snake_case config skeleton | only one mount shape; zero string `.service(` calls; preset v3 boot test; built-in tool manifests complete |
| **M4 — eval/memory/multi-agent foundation** | SQLite FTS memory, `Task`/`Handoff`, 20 golden evals, record/replay CI (non-blocking), supervisor + 2 workers | eval repeatable; memory has provenance; supervisor respects budget/convergence |
| **M5 — config/docs/tests/release** | snake_case config, native fixtures, docs rewrite, 0.2.0 | `rg -i dsh src` near zero; no skips; CI has no Node; release note lists breaking changes |

M1/M2/M3 are **mandatory integration points**. A lane may not stay unmerged for more than three working days.

### D6 — Parallel lanes, one integrator, short branches

- **Lane P:** `api/`, `kernel/`, `builtin/presets.py`, `plugins/`, bundle schema, `CapabilityManifest`.
- **Lane Y:** `core/`, `session/`, `agent/`, `prompt/`, `compact/`, `config.py`.
- **Lane A:** new `observability/`, `context/`, `eval/`, memory backend; may propose schema fields but not own the schema.
- **Integrator:** one owner (Lead) merges every 2–3 days, runs the four gates, and resolves seam disputes. Lanes do not get three separate final say.

Worktrees are allowed; a separate long-lived fork is not. A branch older than three days must be rebased or merged.

### D7 — AI-native P0 is a gate, not a later phase

Trace/replay, `ContextPlan`, and run checkpoint are not postponed until "after the rewrite". M0 reserves `run_id/turn_id/step_id/trace_id`; M1 adds the trace skeleton; M2 adds checkpoint/resume; M3 adds capability risk/cost. A milestone that ships without its AI-native gate is not done.

### D8 — M0 changes no product code

M0 may add documentation and baseline data only. It may not edit `src/`, `tests/`, `pyproject.toml`, or runtime config. This keeps the baseline unconfounded and gives M1 a clean starting commit.

## 3. Consequences

**Positive**

- One native contract removes dual semantics, hidden migrations and "which format is this?" bugs.
- Trace/run correlation is designed in from the first schema, not retrofitted.
- Python types (`dataclass`, `Protocol`, `StrEnum`, `TaskGroup`) replace JS runtime simulation.
- The plugin kernel, permissions, tools, TUI and tests remain; the rewrite is bounded to domain contracts.

**Negative**

- Old sessions and configs become unreadable by the product. This is deliberate; export is an external, one-off operation.
- External v2 plugins break. 0.x release policy allows it; migration notes are required.
- Docs, fixtures and wire-format tests need a coordinated rewrite.
- Estimated effort is 35–45 person-days with integration buffer; 2–3 people for 4–6 calendar weeks.

**Risk controls**

| Risk | Control |
|---|---|
| Lanes drift for weeks | 2–3 day merge cadence; M1/M2/M3 hard gates; one integrator |
| "Remove DSH" turns into removing good design | This ADR's D3 retain/remove lists; design review checks both columns |
| AI-native work is deferred again | Trace/ContextPlan/checkpoint are exit gates, not backlog items |
| 3352 tests churn | Behavior snapshots first; only wire-format assertions are rewritten; tool schema snapshot byte-stable |
| Hidden coupling to `core.wire` | Import gate from M1; count only decreases; no dual-write bridge |
| Performance regression from pydantic on hot path | `dataclass` for messages/events; pydantic only at I/O and tool-argument boundaries |

## 4. Compliance rules

1. No new code imports `deeppyro.core.wire` after M1 starts; the existing 37 importers are removed, not wrapped.
2. No dual write of v4 and native records. No "temporary" v4 reader behind a flag.
3. No `ignorable` compatibility switch; unknown plugin events are skipped by namespace, not by a global flag.
4. No `surfaceOp` under a new name; message edits use stable message ids and `message.replaced`.
5. No second message/event model in a lane; schema changes go through the Y owner.
6. Tool schemas remain byte-stable while the transport is replaced; snapshot tests are the guard.
7. Permission/sandbox checks consume the same tool arguments as execution; typing must not change authorization semantics.
8. Existing four gates — `ruff check`, `ruff format --check`, `scripts/typecheck.py`, `pytest` — stay green at every milestone. The pyright baseline may only shrink.
9. M0 records provenance (model, provider, date, harness version) for every baseline case; no synthetic replay data labeled as recorded.

## 5. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| **A. De-brand only** | Renames fields but keeps v4 interval fold, DSH loop shape and string events; does not meet Pythonization or AI-nativeness. |
| **C. Full rewrite** | Discards a working plugin kernel, tool pipeline, sandbox, TUI and 3352 tests; 8–12 weeks and a second ecosystem. |
| **Sequential P → Y → A** | Three big-bang migrations; each later track reopens the previous contract. |
| **Keep a v4 reader "just in case"** | Permanent dual semantics, two test surfaces, and every new record needs a compatibility decision. Rejected by D2. |
| **Keep string service lookup for plugin compatibility** | Type checking cannot see services; API v3 hard cut is cheaper than a permanent string bridge. |

## 6. M0 deliverables and exit checklist

- [x] ADR (this file).
- [x] `docs/native/schema.md`: native session schema 1 + trace/run correlation; 449 lines, 24 core `EventKind`s, decisions D-1..D-15 in §12.
- [x] `docs/native/README.md`: provenance, case matrix, validation summary, replay checks and known limits.
- [x] 10 recorded cases under `fixtures/native/` (text, multi-turn, truncation, single/parallel/sequential/error/denied tool calls) with manifest and checksums: 17 requests, 7 tool calls, 298,506 bytes, 18/18 required validation checks, 10/10 `status=ok`.
- [x] Decision review for §7: all eight locked below; schema-level D-1..D-15 are M1 defaults recorded in schema §12.
- [x] Four gates green; `git diff --stat` shows no `src/` or `tests/` changes.
- [x] Report to the user with links, counts and anomalies.

M0 footprint: `docs/adr/`, `docs/native/`, `fixtures/native/`, the `fixtures/native/` allow-list in `.gitignore`, `.gitattributes` marking `fixtures/native/**` as `-text` so checksums survive checkout, and frozen banners in `docs/spec/`. No product code.

M0 does **not** create `session/schema.py`, `NativeCodec`, `agent/loop.py`, or any placeholder module. Those start in M1.

## 6.1 M1 deliverables and exit checklist

Status: **complete (2026-11-17)**. M1 is additive: the v4 runtime remains the product path until M2 wires the native loop. No v4 module was edited and no dual-write path was added.

- [x] `src/deeppyro/core/messages.py`: frozen slots `Message`, four content parts (text/reasoning/tool_call/image), eight discriminated sources, strict role/part matrix, JSON round-trip, replacement and missing-tool-result helpers; 19 tests.
- [x] `src/deeppyro/session/errors.py`: the 15 schema §9 exception classes, rooted at `SessionError`.
- [x] `src/deeppyro/session/schema.py`: `SCHEMA_VERSION = 1`, 24-value `EventKind`, closed `SessionHeader`/`Event`, `NativeCodec` read/encode, `NativeSession` atomic header, `O_EXCL` writer lock, fsync append and truncate rollback; v4 header/zstd rejected with `SessionV4Error`.
- [x] `src/deeppyro/session/fold.py`: pure `fold(header, events) -> SessionView`; message replacement, compaction, system block, goal/preset/todo/deliverable state, approvals, plugin pass-through, completed-turn tool pairing and missing-result repair.
- [x] `src/deeppyro/observability/trace.py`: canonical JSON + `sha256:` hashes, run/model-call/tool-call records, `TraceStore` under `DEEPPYRO_TRACES_DIR` or `~/.dpro/traces`, strict reader, and `join_trace` against session events.
- [x] `src/deeppyro/core/ids.py`: `new_run_id`, `new_turn_id`, `new_step_id`, `new_span_id`, `new_trace_id`.
- [x] `tests/fixtures/native/golden-session.jsonl`: one complete tool turn; decode → fold → encode is byte-identical (header `cwd` is platform-normalized in the test).
- [x] Existing 43-tool schema snapshot remains byte-identical (`tests/tools/test_tool_schemas.py`).
- [x] Four gates green: `ruff check`, `ruff format --check`, `scripts/typecheck.py` (79 errors, 0 new), full `pytest` (3465 passed, 0 failed, 0 skipped).

M1 footprint: `src/deeppyro/core/messages.py`, `src/deeppyro/core/ids.py` (additive helpers), `src/deeppyro/session/errors.py`, `src/deeppyro/session/schema.py`, `src/deeppyro/session/fold.py`, `src/deeppyro/observability/{__init__,trace}.py`; tests under `tests/core`, `tests/session`, `tests/observability`; and the golden fixture above.

M1 deliberately does **not** wire these modules into `agent/`, the CLI, the TUI, compaction or provider adapters. That is M2 work. The trace skeleton exists and its join is tested against a real native session, but the agent does not emit trace records yet; no model/tool payload is stored in the trace.

## 6.2 M2 deliverables and exit checklist

Status: **complete for the loop/contract scope (2026-11-17)**. M2 wires the native `AgentLoop`, `ContextPlan`, `RunState`/checkpoint, budget/cancellation and trace emission. The v4 runtime stays the default product path; headless exposes an explicit `DEEPPYRO_NATIVE_LOOP=1` / `--native-loop` switch. TUI migration and the v4 provider port remain M3+ work.

- [x] `src/deeppyro/agent/loop.py` (1030 lines): one native conversation per `run()`. Input is native `Message`; each step builds a `ContextPlan`, converts through the adapter boundary, streams the existing `LlmProvider`, writes one native `assistant.message`, dispatches tool calls through the existing `ToolRegistry`/`Tool` protocol (parallel-safe batches in `asyncio.TaskGroup`), appends native `tool.result` messages and terminates with `turn.ended` + `run.finished`. Only `NativeSession` is written; no v4 session and no dual write.
- [x] `src/deeppyro/agent/context_plan.py`: frozen slots `ContextPlan(system, messages, tools, estimated_tokens, source_message_ids)`, pure deterministic `build_context_plan()`, deep-copied tool schemas; every provider request is built from a plan.
- [x] `RunState` + checkpoint (`agent/run_state.py`, `agent/checkpoint.py`): `RunStatus`, `ToolCallCheckpoint`, `RunState.checkpoint_data()` emitting the schema §5.5 shape; atomic JSON sidecar (temp file + fsync + `os.replace`, directory fsync on POSIX) under `DEEPPYRO_CHECKPOINTS_DIR` or `~/.dpro/checkpoints/`; strict reader. A simulated in-process crash leaves an open step/turn; `NativeSession.open` + `loop.run(resume=True)` repairs it with `aborted`/`disposed`, starts a new run, restores cumulative usage from the last matching RUNNING checkpoint and never replays a completed tool call.
- [x] Budget + cancellation (`agent/budget.py`): `BudgetLimits` (`max_steps`, `max_tokens`, `max_wall_seconds`, optional `max_cost`; `None` = unlimited), cumulative `BudgetUsage`/`BudgetTracker` and typed `BudgetExceeded.turn_reason()`; overruns write the typed `turn.ended`/`run.finished` reason and a FAILED checkpoint. Cancellation uses `asyncio.Event`, a cancel-aware wait and `TaskGroup`, so no task leaks (tests assert `asyncio.all_tasks` after cancel).
- [x] Typed hooks (`agent/events.py`): frozen event dataclasses + `AgentHooks` `Protocol` + `NullAgentHooks`; the loop never uses the string hook bus (`hooks/types.py`).
- [x] Native `run.*` events in `session/schema.py`: `EventKind` now has 27 members; closed validation for `run.started`/`run.checkpoint`/`run.finished`; association requires `run_id`+`trace_id` and forbids `turn_id`/`step_id`; `fold()` skips them. Two pre-existing M1 bugs in `_validate_turn_reason` were fixed: the optional fields documented in schema §5.3 (`aborted.message`, `error.code`, `max_tokens.limit`) were rejected as unknown fields.
- [x] Trace: one `ModelCallRecord` per model call (prompt hash, token counts, latency, finish reason, `context_message_ids`) and one `ToolCallRecord` per tool call (args hash, result size, error flag, latency, idempotency key); no prompt/argument text is stored, and `join_trace` passes against the session log.
- [x] Headless switch: `DEEPPYRO_NATIVE_LOOP` (`1/true/yes/on`) or `--native-loop` routes one `run_once` through the native loop (native session file, `run.<id>.jsonl` trace, `<session_id>.json` checkpoint); the default path is untouched. The native path intentionally skips the v4 session-service bindings and only maps `--max-steps` onto a budget.
- [x] Differential replay: `tests/native/test_differential.py` drives all 10 recorded cases from `fixtures/native/` through a test-only replay provider (SSE parsed from the recordings; fixture bytes unchanged) and compares final assistant text/finish reasons, tool call names + JSON arguments, tool result text/error flags and request counts with the recordings.
- [x] Tests: 63 under `tests/native/` (differential, context plan, budget, loop, kill/resume checkpoint, headless switch) + 11 native run-event tests; schema tests updated for the 27-member enum.
- [x] Gates: `ruff check src tests` 0; `ruff format --check src tests` 0; `scripts/typecheck.py` 79 errors, 0 new; `python -m pytest -q` 3542 passed (M1 baseline 3465 + 77 new M2 tests), 0 failed, 0 skipped.

Known deviation from §4 rule 1: `src/deeppyro/agent/native_provider.py` is the only new `deeppyro.core.wire` importer (37 pre-existing → 38 files). M2 must call the M1 `LlmProvider`; this is the provider-adapter boundary already permitted by schema §6.5 and is documented in the module docstring and schema §6.5. It disappears with the M3 provider port, after which rule 1 becomes enforceable by a monotone importer-count check. No other M2 module imports `core.wire` (verified: 38 importer files total, one of them this adapter).

Not done in M2 (deliberately):

- TUI migration: the switch exists only in `run_once`/headless; the TUI keeps the v4 agent.
- v4 provider port and removal of `core.wire`/`llm/` (M3); `agent/native_provider.py` is the temporary seam.
- Default flip: the native loop is opt-in. Plugin/kernel services are forwarded only as an optional mapping in tool contexts (typed inject arrives with API v3/M3); the native loop performs no string service lookup, so the v3 typed-inject cut needs no v2 compatibility entry from M2.
- Schema doc was extended additively (§5.5) and stays schema 1; no frozen contract was changed.

M2 footprint: `src/deeppyro/agent/{errors,budget,run_state,events,context_plan,checkpoint,native_provider,loop}.py`; `src/deeppyro/session/schema.py` (3 run events + optional-field bugfixes); `src/deeppyro/headless.py` and `src/deeppyro/tui/main.py` (switch); tests under `tests/native/`, `tests/session/test_native_run_events.py` and the updated `tests/session/test_native_schema.py`; `docs/native/schema.md` §5.5 and this checklist.

## 7. Decisions locked at M0

All eight decisions below are closed; the recommendation is the decision. Details and edge cases live in `docs/native/schema.md`; schema-specific refinements D-1..D-15 are M1 defaults recorded in its §12. A later change requires an M1 PR that updates both documents.

| # | Decision | Locked at M0 | Why |
| --- | --- | --- | --- |
| 1 | Event type naming | Dotted lowercase: `tool.call`, `turn.started`, `goal.changed`. | The native schema is a new contract, not a v4 transliteration; dotted names stay valid as Python `StrEnum` members and JSON keys. |
| 2 | Timestamp field | `ts`, UTC epoch **milliseconds**; reserve `time` for durations. | One time unit everywhere; avoids the v4 `time`/`createdAt` ambiguity and leaves `time` free for trace durations. |
| 3 | Run linkage | Header has **no** `run_id`. Events optionally carry `run_id`, `turn_id`, `step_id`, `trace_id`. | One session can host many runs (resume/fork/next turn); a header value would be wrong after the first resume and would force a header rewrite. |
| 4 | Compaction shape | `compaction.done.covers: [message_id]`; no seq ranges. | Stable message identity is the native coordinate; seq ranges are a v4 interval habit and break under fork/resume. |
| 5 | zstd | Not in schema 1; files are always plain `.jsonl`. | Line append + fsync is sufficient; zstd adds frame/torn-tail semantics that need their own benchmark. Revisit only with an append benchmark. |
| 6 | Plugin events | Persisted as `plugin.<id>.<event>`; no `ignorable` flag. | Plugin id is the namespace; unknown events are skipped by namespace, not by a global compatibility switch. |
| 7 | Fork | Copy records, renumber `seq` from 1, keep `ts` and `message.created_at` verbatim, set `parent_id`; no lazy parent references. | The child is self-contained and fold-identical at the fork point; the source can change independently. |
| 8 | API v3 break | Hard cut in 0.2.0 with a migration note; no compatibility shim. Session schema is not part of API v3. | 0.x policy permits the break; a shim would keep the string/service bridge this ADR removes. |

No open decision blocks M1 coding.

## 8. References

- Research report: `D:/dshworkspace/deeppyro-native-python-agent-research.md` (three-track revision, especially §2.6, §5.3, §9.3–§9.5).
- Native schema 1 (M0): `docs/native/schema.md`.
- Current DSH-shaped contracts: `src/deeppyro/core/wire.py`, `src/deeppyro/session/events.py`, `src/deeppyro/session/derive.py`, `src/deeppyro/core/events.py`, `src/deeppyro/config.py`.
- Current loop: `src/deeppyro/agent/session_turn.py`.
- Existing proposal: `docs/spec/INTERFACES.md` §0 (to be superseded by this ADR and the M1 schema).
- Zero-compat scope and milestone table: report §5.3–§5.5.
