# v4 Deletion Plan — native-only hard cut for deeppyro 0.3.0

**Status:** design proposal, analysis only. No source file was modified, moved, or deleted to produce it.

**Baseline:** repo `D:/code/deeppyro`, HEAD `7d03ef6` ("feat: close the remaining 0.2.0 known-issue items") plus the uncommitted wave-1 work (46 modified + 21 untracked files, 1 deleted file). `pyproject.toml` says version `0.2.0`; this plan targets `0.3.0` as the release that removes the v4 stack outright.

**Companion design docs (already in the tree):**

- `docs/design/native-capabilities.md` — capability port phases P0–P9, definition of done (§10), behavior changes (§9).
- `docs/design/tui-native-runtime.md` — TUI phases P0–P6, deletion list (P6 §10), test plan (§11), UX regressions (§12).
- `docs/design/native-provider-port.md` — provider seam, what deleting `core/wire.py` additionally requires.
- `docs/adr/0001-three-track-native-agent.md` — D2 "zero compatibility": 0.3 ships no v4 reader, no `V4Codec`, no dual-write, no `dpyro migrate`.
- `docs/README.md` §6 (L80–90) — authoritative documentation deletion list for 0.3.
- `docs/release/0.2.0-known-issues.md` — KI-001 (TUI still v4), KI-002 (migration), KI-010 (core.wire remains), KI-032 (de-brand).

This document turns those design intents into an evidence-backed, executable deletion order. It deliberately duplicates no design content: it inventories what is left, classifies every reference, sequences the cuts, and ends with a checklist plus the final "no v4 imports" AST guard.

### Table of contents

- [0. Executive summary](#0-executive-summary)
- [1. Inventory of v4-exclusive modules](#1-inventory-of-v4-exclusive-modules)
- [   1.3 Full inventory table](#13-full-inventory)
- [   1.4 Kept look-alikes](#14-modules-that-look-v4-but-are-kept-and-why)
- [2. Classification of remaining src references](#2-classification-of-every-remaining-src-reference-to-the-v4-modules)
- [3. Deletion order in waves](#3-deletion-order-in-waves)
- [   3.2 Migration waves M0–M8](#32-migration-waves-m0m8-prerequisites-no-deletion)
- [   3.3 Deletion waves W1–W6](#33-deletion-waves)
- [4. Data/behavior that becomes unreadable](#4-data-and-behavior-that-becomes-unreadable-or-unavailable)
- [5. Test modules: delete vs rewrite, delta](#5-test-modules-disappear-vs-rewrite-and-the-final-test-count-delta)
- [6. Hidden coupling traps](#6-hidden-coupling-traps)
- [7. Cleanup checklist](#7-cleanup-checklist-execute-top-to-bottom)
- [7.2 Final no-v4 AST guard (code)](#72-the-final-no-v4-imports-ast-guard)
- [7.3 Commands that prove the final state](#73-commands-that-prove-the-final-state)
- [Appendix A — Evidence & reproducibility](#appendix-a--evidence-artifacts-reproducibility)
- [Appendix B — Not verified](#appendix-b--what-the-analysis-did-not-verify)

---

## 0. Executive summary

| Item | Number | Evidence |
|---|---|---|
| v4-exclusive source modules to delete | **59 modules / 16,850 lines** | §1; current-tree line counts |
| Non-v4 src modules with at least one v4 reference | **74 modules** (78 rows counted per-reference before package re-exports) | §2 |
| Test modules that touch a v4-exclusive module | **137 modules / 1,447 static test functions** (of 352 modules / 3,656 fns) | §5 |
| Test modules to delete | **62 modules / 663 fns** (hard-cut recommendation) | §5.2 |
| Test modules to rewrite against native subjects | **43 modules / 459 fns** | §5.2 |
| Test modules kept, imports retyped only | **31 modules / 316 fns** (only because they import `core.protocols` for tool types) | §5.2 |
| Test modules kept as-is | **1 module / 9 fns** (`tests/core/test_events.py`) | §5.2 |
| Expected static test-function delta | **≈ −530 to −575**, before new native/guard suites (design docs list 12 new files, est. +90) | §5.4 |
| Deletion waves (after migration waves M0–M7) | **W1–W6** | §3 |
| Files/behaviors that become unreadable | **v4 session logs, `dpyro migrate`, zstandard, Anthropic transport, v4 TUI history** | §4 |

**Hard blockers that must be decided before W1:**

1. **Anthropic native transport does not exist.** `llm/registry.py:create_native_provider` (L96–116) supports only `_OPENAI_COMPAT_APIS` and raises `ConfigError` for anything else; `headless._resolve_native_provider` (L695–716) turns that into a fallback to the v4 loop. After the cut an Anthropic-configured user has no runtime. Either port the transport (Anthropic-native `LlmProvider`) before W5 or ship 0.3.0 with a documented, tested hard error (D3).
2. **`dpyro migrate` cannot survive a total deletion.** `migrate.py` needs `session/{layout,reader,writer,derive,events}` and `core/wire.py`; it is a v4 reader by construction. ADR 0001 already says the product ships no migrator, but 0.2.0 docs/KI-002 ship one. The hard cut means "migrate on 0.2.x first" (D2). Anything else keeps ~2.3k lines of v4 alive behind an exception.
3. **The projection package is claimed by both design docs.** `native-capabilities.md` P1 retypes `projection/{registry,checkpoint}` to native `Event`/`SessionHeader` and keeps it; `tui-native-runtime.md` P6 deletes the package along with `builtin/projections.py` and `builtin/activity.py`. Both cannot be true. Recommendation: delete it with the TUI switch (D1); decide before starting P1.
4. **The TUI is still a v4 client.** `tui/runtime.py` loads `builtin.sessions.SessionStore`, `tui/history.py` reads v4 logs, `tui/app_sessions.py` writes v4 metadata, `--legacy-v4` still parses. Delete nothing user-visible until the TUI native switch (M7) has landed and shipped in a build.
5. **The existing boundary guard is already stale.** `tests/integration/test_wire_boundary.py` still lists `llm/native_provider.py` in `NATIVE_FOOTPRINT` (L28–47) and `NATIVE_PROTOCOL_MODULES` (L52–59), but that file was deleted in the working tree. Fix (or replace) the guard in W0 before it can be trusted as a deletion gate.

**Decisions this plan recommends (details and alternatives in §3/§4):**

| ID | Decision | Alternative rejected because |
|---|---|---|
| D1 | Delete `projection/` + `builtin/projections.py` + `builtin/activity.py` with the TUI switch | retyping projection to native keeps 1,300+ lines of sidecar machinery for a feature only the old activity feed consumed |
| D2 | Delete `migrate.py` + `session/migrate_{cli,service}.py`; users migrate on 0.2.x | quarantine keeps `wire`+`derive`+`events`+`layout`+`reader`+`writer` + zstandard forever, contradicting ADR 0001 and the empty-allowlist definition of done |
| D3 | Land a native Anthropic provider or ship a documented hard error | silently dropping configured providers is a silent data-path regression |
| D4 | Remove `zstandard` from dependencies | only v4 log compression uses it |
| D5 | Keep `core/event_catalog.py` as a frozen data table; delete its generation chain | `core/events.py` (native bus catalog) imports `BUS_EVENTS`; deleting the table is a separate, larger de-brand change |
| D6 | Remove `--legacy-v4`, `--native-loop`, `DEEPPYRO_NATIVE_LOOP`, `headless.native_loop_enabled` and the TUI "still v4" note | a flag that selects a deleted code path is a lie |

---

## 1. Inventory of v4-exclusive modules

### 1.1 Method

The inventory was produced from a static AST graph of all 717 Python files under the repo (365 source modules, 352 test modules) built at the analysis baseline:

- every `Import`, `ImportFrom` (including relative) statement, with line and lazy (function-body) flag;
- top-level `class`/`def` names, `__all__`, and line counts;
- package re-export resolution: a test that does `from deeppyro.session import Session` is attributed to `session/__init__.py` *and* to the symbol's defining module, so package hubs cannot hide v4 coupling;
- "direct src importer" = module has an import statement whose resolved target is exactly the module (submodule imports count for the submodule, not for the package `__init__`).

Known limits: import counts describe the working tree, not HEAD alone; 46 modified files may have changed (mostly `builtin/{inspect,session_title}.py`, `core/{protocols,wire}.py`, `llm/*`, `projection/registry.py`, `prompt/projection.py` gained docstrings/native helpers, not new importers). Line counts and symbol lists below were recomputed from the current files on disk; importer counts are AST-snapshot values. Re-run the inventory (checklist step C1) before executing W1.

### 1.2 Cluster summary

| Cluster | Files | Lines | What it is |
|---|---|---|---|
| core contracts | 3 | 735 | `core/wire.py` DSH v4 record models, `core/protocols.py` v4 provider/tool protocol, `core/payloads.py` v4 hook payload TypedDicts |
| session v4 log | 11 | 2,280 | v4 `session.v4.jsonl` codec, fold/derive, fork, layout, metadata, image projection; package hub |
| session migration | 3 | 1,611 | `migrate.py` v4↔native converter, CLI entry, kernel service plugin |
| session query | 5 | 967 | v4 log read-only query engine + its builtin plugin |
| agent v4 loop | 14 | 3,741 | `AgentSession` TurnLayer/StreamLayer/ToolLayer/RecordLayer/PromptLayer stack, inbox, retry policy, image request planning |
| prompt + projections | 5 | 1,357 | `prompt/projection.py` v4 projection updates; projection registry/checkpoint + builtin plugin |
| compaction | 6 | 1,921 | v4 surface fold + compaction records + compress/refs/block tools |
| goal round driver | 2 | 1,361 | v4 goal replay fold + automatic round driver over the v4 agent |
| llm v4 providers | 5 | 1,764 | v4 message converters, Anthropic/OpenAI-compatible providers, image budget |
| builtin v4 services | 3 | 598 | hook journal, retry-policy plugin, activity feed (activity has no v4 import; its consumers are v4-only) |
| hooks journal | 1 | 161 | `hook/invoked`/`hook/result` record builders written into v4 sessions |
| TUI history | 1 | 354 | v4-session-derived reasoning/process reconstruction for the TUI |
| **Total** | **59** | **16,850** | |

Two modules that look like they belong here are **not** in the deletion set but need edits: `builtin/sessions.py` (now dual v4/native in the working tree, 116 L) and `builtin/{inspect,session_title,compress}.py` (already dual/native branches). They are classified in §2.3.

### 1.3 Full inventory

Line counts and symbol lists are from the current files; importer lists are direct import statements.

### core contracts

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `core/wire.py` | 233 | WireModel, TextPart, ReasoningPart, ToolCallPart, ImagePart, ToolAdditionPart, ToolRemovalPart, UnknownPart, Message, SessionHeader …(+2) | agent/images.py, agent/inbox.py, agent/request.py, agent/session_records.py, agent/session_state.py, agent/session_stream.py, agent/session_support.py, agent/session_tools.py, builtin/inspect.py, builtin/session_title.py, compact/compactor.py, compact/compress_tool.py, compact/refs.py, compact/surface.py, compact/tokens.py, core/__init__.py, core/protocols.py, goal/round_driver.py, llm/image_budget.py, llm/messages.py, llm/messages_anthropic.py, migrate.py, projection/registry.py, prompt/projection.py, session/derive.py, session/events.py, session/fork.py, session/image_projection.py, session/metadata.py, session/reader.py, session/session.py, session/store.py, session/writer.py, session_query/filters.py, session_query/query.py, session_query/types.py, subagent/manager.py, team/manager.py | 28 (agent/fakes.py, agent/test_agent_images.py, agent/test_compact.py, agent/test_images.py, agent/test_inbox.py, …) |
| `core/protocols.py` | 196 | ToolSchema, LlmRequest, BlockStart, BlockEnd, TextDelta, ReasoningDelta, ToolCallStart, ToolCallArgsDelta, Finish, LlmProvider …(+5) | agent/budget.py, agent/context_plan.py, agent/events.py, agent/loop.py, agent/native_host.py, agent/session_state.py, agent/session_stream.py, agent/session_support.py, agent/session_tools.py, agent/session_turn.py, builtin/ptc.py, builtin/session_title.py, builtin/task_tools.py, compact/block_tools.py, compact/compactor.py, compact/compress_tool.py, compact/surface.py, core/__init__.py, core/payloads.py, eval/live.py, llm/__init__.py, llm/anthropic.py, llm/messages.py, llm/messages_anthropic.py, llm/openai_compat.py, llm/registry.py, mcp/bridge.py, mcp/resources.py, memory/tools.py, prompt/prompt.py, ptc/executor.py, ptc/runtime.py, ptc/sdk.py, ptc/tool.py, subagent/manager.py, team/manager.py, tools/builtin/__init__.py, tools/builtin/ask_user.py, tools/builtin/fs.py, tools/builtin/goal.py, tools/builtin/jobs.py, tools/builtin/memory.py, tools/builtin/plugin_tools.py, tools/builtin/present.py, tools/builtin/schedule.py, tools/builtin/search.py, tools/builtin/shell.py, tools/builtin/skill.py, tools/builtin/subagent.py, tools/builtin/team.py, tools/builtin/todo.py, tools/builtin/web.py, tools/builtin/working_directory.py, tools/permissions.py, tools/pipeline.py, tools/registry.py | 64 (agent/fakes.py, agent/test_agent_images.py, agent/test_agent_loop.py, agent/test_agent_p0.py, agent/test_compact.py, …) |
| `core/payloads.py` | 306 | ToolCallPayload, HookScope, ToolHookContext, ToolHookPayload, PreExecuteHookPayload, ExecuteHookPayload, PostExecuteHookPayload, PreExecuteDecision, ExecuteDecision, PostExecuteDecision …(+11) | agent/session_records.py, builtin/approval_policy.py, builtin/hook_journal.py, builtin/session_title.py, hooks/records.py, session/events.py, session/writer.py, tools/pipeline.py | 1 (core/test_payloads.py) |

### session v4 log

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `session/__init__.py` | 105 | — | builtin/sessions.py, session/fork.py, session/session.py, session/store.py, session_query/filters.py, session_query/query.py | 25 (agent/test_hook_records.py, agent/test_inbox.py, agent/test_session_write_fidelity.py, compact/support.py, compact/test_block_tools.py, …) |
| `session/session.py` | 218 | Session | projection/checkpoint.py, projection/registry.py, session/__init__.py, session/fork.py, session/store.py, subagent/manager.py, team/manager.py | 8 (agent/test_task_service.py, builtin/test_projections.py, projection/support.py, subagent/helpers.py, subagent/test_handoff.py, …) |
| `session/store.py` | 223 | SessionInfo, SessionStore | session/__init__.py, session/fork.py | 14 (agent/test_task_service.py, builtin/test_projections.py, projection/support.py, projection/test_checkpoint.py, projection/test_fork.py, …) |
| `session/writer.py` | 140 | encode_header_line, event_to_dict, encode_event_line, write_new_session, append_events | migrate.py, session/__init__.py, subagent/manager.py, team/manager.py | 1 (core/test_payloads.py) |
| `session/reader.py` | 159 | SessionRead, decompress_zstd_frames, read_session_file, read_header | migrate.py, session/__init__.py, session/store.py | 3 (native/synthetic.py, session/test_migrate.py, session_query/test_summaries.py) |
| `session/events.py` | 385 | is_event_seq, is_surface_type, decode_source_event_seqs, encode_source_event_seqs, validate_header, is_absolute_path, header_line_dict, normalize_event, assert_source_event_references | migrate.py, session/__init__.py, session/derive.py, session/reader.py, session/session.py, session/writer.py | 5 (agent/fakes.py, core/test_payloads.py, ptc/test_records.py, session/support.py, session/test_invariants.py) |
| `session/derive.py` | 313 | fold_surface, plan_surface_transition, pending_tool_calls, repair_pending_tool_calls, derive_messages, event_message | agent/images.py, agent/session_records.py, agent/session_stream.py, compact/compress_tool.py, compact/refs.py, migrate.py, prompt/projection.py, session/__init__.py, session/session.py | 7 (agent/fakes.py, integration/test_golden.py, native/synthetic.py, session/test_derive.py, session/test_derive_repair.py, …) |
| `session/fork.py` | 287 | SessionForkError, fork_session, build_fork_seed, open_turn_closers | session/__init__.py, subagent/manager.py, team/manager.py | 2 (projection/test_fork.py, session/test_fork.py) |
| `session/layout.py` | 129 | encode_segment, project_key, log_filename, session_file_path, flat_session_path, is_log_file, discover_session_files | compact/blocks.py, compact/refs.py, migrate.py, session/__init__.py, session/store.py, subagent/manager.py, team/manager.py | 1 (session/test_migrate.py) |
| `session/metadata.py` | 176 | collapse, strip_command, first_sentence, derive_title, preview_text, first_user_message, first_user_text, existing_title, title_source, record_title …(+2) | builtin/session_title.py, session/store.py, tui/app_sessions.py, tui/session_view.py | 5 (builtin/test_session_title.py, core/test_payloads.py, session/test_metadata.py, session/test_store_info.py, test_session_title.py) |
| `session/image_projection.py` | 145 | image_identity, offloaded_image_text, apply_image_offloads, project_image_offloads | agent/images.py, agent/session_stream.py, session/derive.py | 3 (agent/test_agent_images.py, agent/test_images.py, session/test_image_projection.py) |

### session migration

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `migrate.py` | 1443 | MigrationError, SessionFormat, MigrationStatus, NativeDocument, V4Document, MigrationEntry, MigrationReport, map_session_id, v4_to_native, native_to_v4 …(+2) | session/migrate_cli.py, session/migrate_service.py | 1 (session/test_migrate.py) |
| `session/migrate_cli.py` | 116 | build_parser, main | tui/main.py | 0 |
| `session/migrate_service.py` | 52 | SessionMigratePlugin | — | 1 (session/test_migrate.py) |

### session query

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `session_query/__init__.py` | 61 | — | builtin/session_query.py | 9 (agent/test_hook_records.py, session_query/conftest.py, session_query/support.py, session_query/test_events.py, session_query/test_hook_trace.py, …) |
| `session_query/query.py` | 505 | SessionQuery, scan_tail_title | session_query/__init__.py | 2 (session_query/test_hook_trace.py, session_query/test_summaries.py) |
| `session_query/filters.py` | 173 | EventFilter, normalize_text, event_matches, event_text, extract_text | session_query/__init__.py, session_query/query.py | 0 |
| `session_query/types.py` | 104 | SessionQueryError, SessionSummary, EventWindow, SessionTrace | session_query/__init__.py, session_query/filters.py, session_query/query.py | 0 |
| `builtin/session_query.py` | 124 | SessionQueryConfig, SessionQueryPlugin | tui/app_panels.py | 1 (session_query/test_plugin.py) |

### agent v4 loop

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `agent/session.py` | 60 | AgentSession | agent/__init__.py | 1 (agent/test_session_write_fidelity.py) |
| `agent/session_state.py` | 268 | — | agent/session_records.py | 0 |
| `agent/session_turn.py` | 469 | TurnLayer | agent/session.py | 0 |
| `agent/session_tools.py` | 377 | ToolLayer | agent/session_stream.py | 0 |
| `agent/session_records.py` | 104 | RecordLayer | agent/session_prompt.py | 0 |
| `agent/session_support.py` | 191 | ToolCallDraft, StreamAccumulator, SessionHandle | agent/session.py, agent/session_prompt.py, agent/session_state.py, agent/session_stream.py, agent/session_tools.py, agent/session_turn.py | 0 |
| `agent/session_stream.py` | 358 | StreamLayer | agent/session_turn.py | 0 |
| `agent/session_prompt.py` | 140 | PromptLayer | agent/session_tools.py | 0 |
| `agent/request.py` | 245 | canonical_tools, HeaderPlan, ToolChange, latest_header, latest_header_seq, build_header, plan_header, tool_changes, build_tool_change_message, tool_change_data …(+2) | agent/session_tools.py | 2 (agent/test_request.py, agent/test_session_write_fidelity.py) |
| `agent/inbox.py` | 360 | Inbox | agent/session_state.py, agent/session_turn.py, goal/round_driver.py | 4 (agent/test_inbox.py, agent/test_inbox_events.py, goal/conftest.py, goal/test_round_driver.py) |
| `agent/retry.py` | 491 | RetryDecision, decide_retry | agent/session_stream.py, builtin/llm_retry.py | 2 (agent/test_retry.py, builtin/test_llm_retry.py) |
| `agent/stream.py` | 302 | AssistantStreamAccumulator, expand_stream | agent/session_state.py, agent/session_stream.py | 2 (agent/test_agent_p0.py, agent/test_stream.py) |
| `agent/images.py` | 348 | ImageRequestPlan, surface_messages_with_seq, expand_image_attachments, plan_request_images | agent/session_stream.py | 1 (agent/test_images.py) |
| `agent/options.py` | 28 | AgentOptions | agent/__init__.py, agent/session_state.py | 22 (agent/test_agent_images.py, agent/test_agent_loop.py, agent/test_agent_p0.py, agent/test_hook_records.py, agent/test_inbox_events.py, …) |

### prompt + projections

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `prompt/projection.py` | 263 | ProjectionUpdate, RetainedSnapshot, create_system_message, create_runtime_context_message, SystemPromptProjection, RuntimeContextProjection | agent/session_prompt.py, prompt/__init__.py | 1 (prompt/test_projection.py) |
| `projection/__init__.py` | 57 | — | builtin/projections.py | 5 (projection/activity_defs.py, projection/test_activity_parity.py, projection/test_checkpoint.py, projection/test_fork.py, projection/test_registry.py) |
| `projection/checkpoint.py` | 233 | sidecar_path, encode_checkpoint, decode_checkpoint, read_checkpoint, write_checkpoint, save_checkpoint, load_checkpoint, ProjectionCheckpoints | projection/__init__.py | 0 |
| `projection/registry.py` | 501 | ProjectionError, ProjectionDefinition, ProjectionHandle, ProjectionRegistry, inherited_event_count | projection/__init__.py, projection/checkpoint.py | 0 |
| `builtin/projections.py` | 303 | definitions, register_all, ProjectionsService, ProjectionsPlugin | headless.py, tui/runtime.py | 2 (builtin/test_projections.py, projection/activity_defs.py) |

### compaction

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `compact/compactor.py` | 279 | Compactor | agent/session_state.py, compact/__init__.py | 0 |
| `compact/surface.py` | 334 | SurfaceNode, CompactionRegion, EntryState, fold_surface, tool_pairing_cuts, balanced_before, balanced_after, derive_node_message, surface_messages, estimate_node_tokens …(+3) | compact/__init__.py, compact/compactor.py | 0 |
| `compact/tokens.py` | 45 | estimate_text_tokens, estimate_message_tokens, estimate_messages_tokens | compact/__init__.py, compact/compactor.py, compact/compress_tool.py, compact/surface.py | 0 |
| `compact/refs.py` | 342 | canonical_ref, ref_ordinal, RefEntry, ResolvedRef, RefResolution, RefStore, addressable_messages, resolve_refs, ensure_refs | builtin/compress.py, compact/compress_tool.py | 3 (compact/support.py, compact/test_compress_tool.py, compact/test_refs.py) |
| `compact/compress_tool.py` | 616 | CompressBlock, parse_content, requested_refs, format_local, CompressTool, replace_range | builtin/compress.py, compact/block_tools.py | 3 (compact/support.py, compact/test_block_tools.py, compact/test_compress_tool.py) |
| `compact/block_tools.py` | 305 | SearchContextTool, DecompressTool | builtin/compress.py | 1 (compact/test_block_tools.py) |

### goal round driver

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `goal/replay.py` | 429 | GoalReplayError, SnapshotChange, ClearedChange, decode_change, apply_change, FoldRecord, fold, view | goal/__init__.py, goal/service.py | 1 (goal/test_goal_replay.py) |
| `goal/round_driver.py` | 932 | DriverSession, DriverAgent, render_goal_round_prompt, is_goal_round_source, RoundAttempt, DriverState, RoundDriver | builtin/goal_round_driver.py | 4 (goal/conftest.py, goal/test_driver_integration.py, goal/test_fence.py, goal/test_round_driver.py) |

### llm v4 providers

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `llm/messages.py` | 210 | message_text, message_reasoning, message_tool_calls, normalize_arguments, tool_to_json, message_to_chat, convert_messages, build_chat_payload | agent/images.py, llm/__init__.py, llm/messages_anthropic.py, llm/openai_compat.py, session/metadata.py | 2 (llm/test_fixtures.py, llm/test_messages.py) |
| `llm/messages_anthropic.py` | 279 | tool_to_anthropic, convert_anthropic_messages, build_anthropic_payload | llm/__init__.py, llm/anthropic.py | 1 (llm/test_messages_anthropic.py) |
| `llm/anthropic.py` | 573 | AnthropicProvider | llm/__init__.py, llm/registry.py | 1 (llm/test_anthropic.py) |
| `llm/openai_compat.py` | 441 | OpenAICompatProvider | llm/__init__.py, llm/registry.py | 0 |
| `llm/image_budget.py` | 261 | ImageBudget, base64_length, offloaded_image_prefix_count, required_image_offload | agent/images.py | 3 (agent/test_agent_images.py, agent/test_images.py, llm/test_image_budget.py) |

### builtin v4 services

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `builtin/hook_journal.py` | 77 | HookJournalPlugin | — | 2 (builtin/test_hook_journal.py, integration/test_p2_bus.py) |
| `builtin/llm_retry.py` | 231 | build_retry_policy, LlmRetryPlugin | — | 1 (builtin/test_llm_retry.py) |
| `builtin/activity.py` | 290 | TodoItem, JobInfo, AgentInfo, ActivityState, parse_todos, plan_counts, current_todo, ActivityService, ActivityPlugin | builtin/projections.py, tui/runtime.py, tui/screens/jobs.py, tui/screens/plan.py, tui/screens/team.py, tui/widgets/activity_bar.py | 7 (builtin/test_activity.py, integration/test_plugin_services.py, projection/test_activity_parity.py, tui/test_activity_bar.py, tui/test_jobs_screen.py, …) |

### hooks journal

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `hooks/records.py` | 161 | invoked_record, result_record, agent_from_payload, is_journaled | agent/session_support.py, builtin/hook_journal.py, session_query/query.py | 4 (builtin/test_hook_journal.py, core/test_payloads.py, hooks/test_records.py, integration/test_builtin_hooks_real.py) |

### TUI history

| Module | Lines | Public symbols (top-level) | Direct src importers | Direct test modules (static fns) |
|---|---|---|---|---|
| `tui/history.py` | 354 | ReasoningStep, TurnReasoning, source_kind, is_ptc_mode_source, is_ptc_mode_message, reasoning_text, reasoning_from_session, reasoning_map, TurnTool, ProcessEntry …(+3) | tui/app.py, tui/app_sessions.py, tui/screens/thinking.py, tui/widgets/agent_process.py, tui/widgets/conversation.py | 5 (tui/test_app.py, tui/test_conversation.py, tui/test_history.py, tui/test_ptc_ui.py, tui/test_thinking_screen.py) |



### 1.4 Modules that look v4 but are kept (and why)

| Module | Why it is not deleted |
|---|---|
| `core/events.py` | Native kernel event catalog (94 DSH bus events → dispatch modes). Imports `core/event_catalog.BUS_EVENTS` only. |
| `core/event_catalog.py` | Generated data table, but consumed by the native catalog. Keep the table, delete the generation chain (`scripts/extract_dsh_events.mjs`, `scripts/gen_event_catalog.py`, catalog docs) and the generation wording in the docstring (D5). |
| `llm/retry.py` | Native/shared retry policy (imports only `api.services.bind_service_type` + `core.errors.LlmError`); consumed by `llm/native_provider.py` and `headless.py`. |
| `llm/sse.py` | No deeppyro imports at all; used by the native provider. |
| `llm/catalog.py`, `llm/discovery.py` | Provider/model discovery used by native registry. |
| `session/{schema,fold,native_index,native_query,native_fork,native_control,errors}.py` | The native schema-1 stack the product keeps. |
| `hooks/service.py`, `hooks/types.py` | Native hook facade over the bus. |
| `builtin/sessions.py` | Dual surface in the working tree; W2 removes the v4 half, file stays (rewritten `SessionsPlugin`). |
| `builtin/{inspect,session_title,compress}.py` | Already carry native paths; W1/W2 delete the v4 branch, file stays. |
| `eval/live.py`, `eval/replay.py` | Native eval harness; only need tool-contract retyping. |
| `agent/{loop,native_host,model_call,run_state,budget,events,checkpoint,context_plan,errors,native_factory,native_records,turn_messages}.py` | Native loop and host; `turn_messages` keeps `core.protocols` tool types until W1 retype. |
| `observability/*`, `tools/*` (non-protocol fixes), `kernel/*`, `api/*`, `plugins/*` | Native infrastructure. |

## 2. Classification of every remaining src reference to the v4 modules

Legend for the action column:

- **migrate to native** — the importing module stays; its call site is rewritten to the native replacement before the referenced v4 module is deleted.
- **delete reference (module stays)** — the importing module stays but the v4 feature/code path inside it is removed; the module is reworked to its native path (dual-path modules already in the working tree are the model).
- **delete now** — the importing module itself is deleted in the same wave (used only where the reference cannot be rewritten).
- **keep with reason** — the reference survives the cut.

A module appears once with all of its v4 references; 74 non-v4 src modules are listed across the tables. Package-hub re-exports are attributed to the hub module here and to the defining module in §1.

### 2.1 Tool protocol, hook payloads, and misc runtime types (migrate first — phase T)

The single largest mechanical change. `core/protocols.py` has 56 direct src importers; 45 of them want only the tool types, and can be retyped in one wave once `tools/contract.py` exists.

| Importing module | v4 reference (line) | Action | Native replacement |
|---|---|---|---|
| `agent/context_plan.py` | core.protocols.Tool L18 | migrate to native | tools/contract.Tool; already emits core.llm.ToolSpec |
| `agent/events.py` | core.protocols.ToolResult L16 | migrate to native | tools/contract.ToolResult |
| `agent/loop.py` | core.protocols.Tool/ToolContext/ToolResult L90 | migrate to native | tools/contract (phase T); loop is native and stays |
| `agent/native_host.py` | core.protocols.Tool L50 | migrate to native | tools/contract.Tool |
| `agent/turn_messages.py (new)` | core.protocols.Tool/ToolResult L42 | migrate to native | tools/contract (phase T) before W1 |
| `builtin/approval_policy.py` | core.payloads.ApprovalRequestHookPayload L26 | migrate to native | new wire-free hooks payload module (e.g. hooks/payloads.py) created in M1 |
| `builtin/ptc.py` | core.protocols.ToolSchema L18 | migrate to native | core.llm.ToolSpec / tools/contract.Tool |
| `builtin/task_tools.py` | core.protocols.ToolContext/ToolResult L45 | migrate to native | tools/contract |
| `eval/live.py` | core.protocols.Tool L41 | migrate to native | tools/contract.Tool |
| `mcp/bridge.py` | core.protocols.ToolContext/ToolResult L14 | migrate to native | tools/contract |
| `mcp/resources.py` | core.protocols.ToolContext/ToolResult L11 | migrate to native | tools/contract |
| `memory/tools.py` | core.protocols.ToolContext/ToolResult L19 | migrate to native | tools/contract |
| `prompt/prompt.py` | core.protocols.ToolSchema L21 | migrate to native | core.llm.ToolSpec; renderers take Sequence[ToolSpec] |
| `ptc/executor.py` | core.protocols.ToolContext/... L23 | migrate to native | tools/contract |
| `ptc/runtime.py` | core.protocols.* L29 | migrate to native | tools/contract |
| `ptc/sdk.py` | core.protocols.* L41 | migrate to native | tools/contract |
| `ptc/tool.py` | core.protocols.ToolContext/ToolResult L11 | migrate to native | tools/contract |
| `tools/pipeline.py` | core.payloads hook TypedDicts L42-53; core.protocols.ToolContext/ToolResult L55 | migrate to native | tools/contract + hooks/payloads; JSONL journal goes to hook service/trace |
| `tools/registry.py` | core.protocols.Tool L8; ToolSchema | migrate to native | tools/contract.Tool; schemas()->tuple[core.llm.ToolSpec,...] |
| `tools/permissions.py` | core.protocols.ToolContext L16 | migrate to native | tools/contract |
| `tools/builtin/__init__.py + 18 tool modules` | core.protocols.Tool/ToolSchema/ToolContext/ToolResult | migrate to native | tools/contract (18 files listed in §2.1) |

Other 18-file cluster: `src/deeppyro/tools/builtin/` = ask_user.py, fs.py, goal.py, jobs.py, memory.py, plugin_tools.py, present.py, schedule.py, search.py, shell.py, skill.py, subagent.py, team.py, todo.py, web.py, working_directory.py plus package `__init__.py`; every one imports only `Tool`/`ToolSchema`/`ToolContext`/`ToolResult` from `core.protocols` (no wire records).

### 2.2 Session, agent, team/subagent, and goal references

| Importing module | v4 reference (line) | Action | Native replacement / rule |
|---|---|---|---|
| `builtin/compress.py` | builtin.sessions.SessionsPlugin L21; compact.block_tools L22; compact.blocks L23; compact.compress_tool L24; compact.refs L25 | migrate to native | native session control (session/native_control.py) + native compact modules from M3 |
| `builtin/goal.py` | builtin.sessions.SessionsPlugin L10 | migrate to native | native sessions plugin API (session/native_control) |
| `builtin/subagent.py` | builtin.sessions.SessionsPlugin | migrate to native | same; plus agent/children.py |
| `builtin/team.py` | builtin.sessions.SessionsPlugin | migrate to native | same |
| `builtin/__init__.py` | builtin.sessions.SessionsPlugin | migrate to native | same |
| `builtin/inspect.py` | core.wire.MESSAGE_PROJECTION_TYPES/RECORD_TYPES/SURFACE_EVENT_TYPES L22 | delete reference (module stays) | v4 branch deleted; native report (working tree) uses session.fold/native_index/schema and stays |
| `builtin/session_title.py` | core.payloads.SessionTitleLlmRequestData L39; core.protocols.LlmRequest/TextDelta L40; core.wire.Message/TextPart L41; session.metadata names L45 | delete reference (module stays) | v4 branch deleted; native path name_native_session uses session.schema EventKind + core.llm events; move the LLM-request payload type to hooks/payloads.py |
| `builtin/sessions.py` | session.SessionStore L10 | delete reference (module stays) | v4 half deleted; native_* methods delegate to session/native_control.py (already in tree) |
| `builtin/session_query.py` | session_query.{EventFilter,SessionQuery} L27; builtin.sessions L24 | migrate to native | session/native_query.py from M7; plugin rewritten or deleted as v4-only surface (recommend delete; query is a library/TUI concern) |
| `builtin/projections.py` | builtin.activity L19; projection package L20 | delete reference (module stays) | deleted with D1 in W1 (only headless/tui.runtime import it) |
| `compact/__init__.py` | compact.{compactor,prompts,surface,tokens} re-exports L5-27 | migrate to native | re-export native compaction API from M3; v4 surface/tokens names gone |
| `compact/blocks.py` | session.layout.encode_segment L28 | migrate to native | native addressable refs from fold (M3); session.layout deleted W3 |
| `goal/__init__.py` | goal.replay names L5 | delete reference (module stays) | stop re-exporting replay fold (goal/replay.py deleted W2) |
| `goal/service.py` | goal.replay.{ClearedChange,GoalReplayError,SnapshotChange} L23 | migrate to native | native fold over goal.changed snapshots (M4); no replay module |
| `hooks/records.py` | core.payloads.HookInvokedRecord/HookResultRecord L19 | delete reference (module stays) | whole module deleted W2 (records go to trace store) |
| `llm/image_budget.py` | core.payload.field_of L36; core.wire.ImagePart/Message L37 | delete now (module dies) | v4-only image offload chain (agent/images.py importer); delete in W2 |
| `subagent/manager.py` | core.protocols.LlmProvider L24; core.wire.SessionHeader; session.fork.build_fork_seed; session.layout.flat_session_path; session.session.Session; session.writer.write_new_session | migrate to native | agent/children.py + session/native_fork.py + session/native_control.py (M6) |
| `team/manager.py` | core.protocols.LlmProvider L26; core.wire LogRecord/SessionHeader; session.fork helpers; session.layout.flat_session_path; session.session.Session; session.writer.write_new_session | migrate to native | agent/children.py + native_fork/native_control (M5) |

### 2.3 LLM provider layer and hub re-exports

| Importing module | v4 reference (line) | Action | Native replacement / rule |
|---|---|---|---|
| `agent/__init__.py` | re-exports AgentOptions/AgentSession/StreamAccumulator/ToolCallDraft from agent.options/agent.session | delete reference (module stays) | export only native API; agent/session.py deleted in W2 |
| `agent/budget.py` | core.protocols.Usage L20 | migrate to native | core.llm.Usage (same fields; already imported elsewhere) |
| `headless.py` | builtin.sessions.SessionStore path; builtin.projections.ProjectionsService L61; llm.retry.RetryPolicy L74 (keep); native_loop_enabled L129-146 + NATIVE_LOOP_ENV L102-104 | undefined | drop v4 branch/flags (D6); boot native host; delete projections wiring (D1); keep RetryPolicy binding |
| `core/__init__.py` | re-exports core.protocols (20 names) + core.wire model/report types L16-34 | delete reference (module stays) | keep native re-exports (core.llm/core.messages) only |
| `llm/__init__.py` | core.protocols stream types L16; v4 Anthropic/OpenAICompat providers L28/L58; v4 message builders L51-52 | delete reference (module stays) | re-export core.llm contract + create_native_provider only |
| `llm/registry.py` | core.protocols.LlmProvider L29; v4 providers L30/L34 | delete reference (module stays) | keep create_native_provider; delete create_provider + v4 branches |
| `prompt/__init__.py` | prompt.projection re-exports L5 | delete reference (module stays) | export prompt.prompt + new prompt/context.py only |
| `builtin/llm.py` | documents that only OpenAI-compatible APIs have a native transport; falls back to v4 loop | migrate to native | drop `provider()`/`add_provider()` v4 paths, keep `native_provider()`/`add_native_provider()`; rewrite docstring after Anthropic decision (D3) |

### 2.4 TUI references

The TUI is the last consumer: every row here is resolved by the TUI native switch (M7/P6), after which the v4 modules are only reachable from the v4 agent stack itself.

| Importing module | v4 reference (line) | Action | Native replacement / rule |
|---|---|---|---|
| `tui/app.py` | tui.history.reasoning_from_session L36 | migrate to native | derive from session.fold/SessionView + trace (M7) |
| `tui/app_sessions.py` | session.metadata record_archive/record_title L7; tui.history is_ptc_mode_message/process_map L12 | migrate to native | native_control rename/archive + native fold (M7) |
| `tui/screens/thinking.py` | tui.history.TurnReasoning L25 | migrate to native | fold view + trace (M7) |
| `tui/widgets/agent_process.py` | tui.history.ReasoningStep/TurnReasoning L35 | migrate to native | fold view + trace (M7) |
| `tui/widgets/conversation.py` | tui.history TurnProcess/TurnReasoning/is_ptc_mode_source L27 | migrate to native | fold view + trace (M7) |
| `tui/session_view.py` | session.metadata.EMPTY_TITLE L14 | migrate to native | native title constant/helper (M7) |
| `tui/app_panels.py` | builtin.session_query lazy L139 | migrate or delete | native query library (M7); delete lazy import with plugin |
| `tui/main.py` | session.migrate_cli.main lazy L125; --legacy-v4 L48-53; --native-loop L54-58; backend selection L141-159; v4 note L161-167 | delete reference (module stays) | remove migrate dispatch (D2) and all v4 flags (D6) |
| `tui/runtime.py` | builtin.activity.ActivityService; builtin.projections.ProjectionsService L27; builtin.sessions.SessionStore | migrate to native | native control + fold facts; activity/projections deleted (D1) |
| `tui/screens/jobs.py` | builtin.activity.JobInfo | migrate to native | jobs plugin facts / fold view (M7) |
| `tui/screens/plan.py` | builtin.activity.TodoItem/plan_counts | migrate to native | fold todos (M7) |
| `tui/screens/team.py` | builtin.activity.AgentInfo | migrate to native | plugin.team.* / fold (M7) |
| `tui/widgets/activity_bar.py` | builtin.activity.ActivityState/current_todo/plan_counts | migrate to native | fold todos/jobs (M7) |

### 2.5 Intentionally kept references

| Importing module | v4 target | Why it is kept |
|---|---|---|
| `core/events.py` | `core.event_catalog.BUS_EVENTS` | `core/events.py` is the native kernel event catalog (94 bus events); `core/event_catalog.py` is its generated data table. Delete the generation chain (`scripts/extract_dsh_events.mjs`, `scripts/gen_event_catalog.py`, the two catalog docs, the "generated" wording) but keep the table and this import (D5). Revisit in the separate KI-032 de-brand work. |
| `packaging/hiddenimports.py` | derives hidden imports from `bundle.toml` rows | Not a v4 import; bundle rows for deleted plugins are removed in their deletion wave, which shrinks hidden imports automatically. Only the docstring ("38 plugin modules", transitive zstandard/Pillow/httpx) needs updating in W6. |
| `tests/architecture/test_frozen_hiddenimports.py` | bundle rows | Asserts generic properties (every row yields an import, sorted/unique, datas exist). No edit needed. |

No other remaining src module imports a v4-exclusive module. The evidence for that claim is the final guard test in §7.

## 3. Deletion order in waves

### 3.1 Two invariants

The deletion plan is constrained by two invariants that the current tree violates in the obvious direction, so they are stated first:

1. **No dangling import.** A module may only be deleted when *no remaining module in `src/` imports it* (directly or through a package re-export). Consequence: consumers must move before their dependencies. The literal order "session, then agent, then plugins/TUI" is not executable — `agent/session_*.py` import `session.*`, and TUI/plugins import both — so the wave order below follows the real import direction: leaf capabilities and v4-only plugins/utilities first, then the v4 agent stack, then the v4 session format stack, then the wire boundary and core contracts. Where a desired order and the graph conflict, the plan says so and offers a combined wave.
2. **The suite is green after every wave.** Each wave is a self-contained commit (or short commit series) whose gate is: the final no-v4 AST guard passes with its allowlist shrunk by exactly that wave's modules, targeted tests pass, and the full suite passes before merge.

Gates are the same four commands plus the native differential matrix (CI already runs these; `uv run` in CI, the local venv equivalent below):

```powershell
# from D:/code/deeppyro
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe scripts\typecheck.py
.venv\Scripts\python.exe -m pytest -q tests/native/test_differential.py tests/native/test_differential_synthetic.py
```

Per-wave targeted gates are listed with the wave; the full pytest run is the merge gate. `scripts/typecheck.py` is a pyright ratchet: deleting files with baseline entries automatically removes them from the baseline; run `scripts/typecheck.py --write` in the same wave to re-record it (today 26 of the 79 counted errors live on 11 v4 files, §6.4).

### 3.2 Migration waves M0–M8 (prerequisites, no deletion)

These are the phases from the two design docs, with the evidence for why deletion cannot start earlier. They delete no source module (they rewrite modules and tests in place).

| Wave | Content | Why it must precede deletion | Gate |
|---|---|---|---|
| **M0** | Land the working-tree wave; fix the stale guard (`tests/integration/test_wire_boundary.py` lists deleted `llm/native_provider.py`); add `tests/test_v4_free.py` in allowlist mode; record static test counts; freeze the inventory in this doc's §1 | A guard that lies cannot gate a deletion; measurement before mutation | full pytest, ruff, typecheck, guard |
| **M1** | Phase T: new `tools/contract.py`; retype `tools/*`, `tools/builtin/*` (18), `mcp/*`, `memory/tools.py`, `ptc/*`, `builtin/ptc.py`, `builtin/task_tools.py`, `agent/{context_plan,events,loop,native_host,turn_messages}.py`, `eval/live.py`, `prompt/prompt.py`; create wire-free `hooks/payloads.py` for `tools/pipeline.py` | `core/protocols.py` cannot be deleted while 45 kept modules import its tool types; `core/wire.py` cannot be deleted while `prompt/prompt.py` imports `ToolSchema` | targeted tests + full pytest |
| **M2** | Native-only LLM layer: `llm/__init__.py` + `llm/registry.py` export `core.llm` contract and `create_native_provider` only; `builtin/llm.py` drops v4 provider paths; native headless is the only path (flags removed in M6) | makes `llm/{anthropic,messages,messages_anthropic,openai_compat}.py` unreferenced (W1) | `tests/llm`, `tests/builtin/test_llm.py`, differential |
| **M3** | Session control plane and boot: `builtin/sessions.py` native half only; `session/native_control.py`; `agent/children.py`; `native_fork` through_seq; `goal/service.py` native fold; entry points native | v4 `SessionStore`/`Session` are the number-one hidden dependency of plugins and TUI | `tests/session/native_*`, `tests/integration/test_*bus*`, `tests/tui` |
| **M4** | Prompt projection + runtime context native (`prompt/context.py`, rewritten `prompt/prompt.py` renderers) | makes `prompt/projection.py` unreferenced (W3) | `tests/prompt`, differential prompt check |
| **M5** | Compaction native on `session.fold` + `agent/context_plan` estimators; compress plugin native | makes v4 `compact/{surface,tokens,compactor,refs,compress_tool,block_tools}.py` unreferenced (W3) | `tests/compact` (rewritten), differential compaction |
| **M6** | Goal native driver; delete `goal/{replay,round_driver}.py` imports from `goal/__init__`/`service`; entry-point cut: remove `--legacy-v4`, `--native-loop`, `DEEPPYRO_NATIVE_LOOP`, `native_loop_enabled`, TUI migrate dispatch | makes `goal/replay.py` + `round_driver.py` unreferenced (W3); v4 stack becomes unreachable from the CLI | `tests/goal`, `tests/tui/test_headless.py` rewritten, full pytest |
| **M7** | Team + subagent native children over `agent/children.py`/host, usage trace sink | makes `subagent/manager.py`, `team/manager.py` v4 branches drop their wire/session imports (W2) | `tests/team`, `tests/subagent`, usage trace tests |
| **M8** | TUI native switch: app/runtime/sessions/history/widgets read `session.fold` + `native_control` + trace; `session_query` replaced by `native_query`; task-tool cleanup; eval/replay provider swap | makes `tui/history.py`, `session_query/*`, `projection/*`, `builtin/{projections,activity}.py` unreferenced (W2) | `tests/tui`, `tests/eval`, `tests/native`, full pytest |

Only **M0–M3** are strictly blocking for the first source deletion assuming W1 is ordered after M2. M4–M8 block W2/W3. W4/W5 need every M-wave green.

### 3.3 Deletion waves

#### W1 — v4 LLM provider layer, retry plugin, migrator, hook journal

```
src/deeppyro/llm/anthropic.py            573
src/deeppyro/llm/messages.py             210
src/deeppyro/llm/messages_anthropic.py   279
src/deeppyro/llm/openai_compat.py        441
src/deeppyro/builtin/llm_retry.py        231
src/deeppyro/builtin/hook_journal.py      77
src/deeppyro/migrate.py                 1443
src/deeppyro/session/migrate_cli.py      116
src/deeppyro/session/migrate_service.py   52
```

- **Callsites changed before/with the wave:** `llm/__init__.py` (M2 already), `llm/registry.py` v4 branch (M2), `tui/main.py` migrate dispatch (M6), bundle rows `llm-retry` (L91) and `session-migrate` (L31), `builtin/llm.py` docstring, `docs/guides/session-migration.md` archived.
- **Tests:** delete `tests/llm/{test_anthropic,test_messages,test_messages_anthropic,test_fixtures,helpers}.py`, `tests/builtin/test_llm_retry.py`, `tests/builtin/test_hook_journal.py` (hook-journal half), `tests/session/test_migrate.py`. (Hook journal's integration coverage in `tests/integration/test_p2_bus.py` is rewritten in M-wave/TUI work.)
- **Guard:** remove these nine modules from the allowlist.
- **Gate:** `pytest tests/llm tests/builtin/test_llm.py tests/session/native_*`, full pytest, ruff, typecheck.

#### W2 — v4 session query, projection/activity subsystem, TUI history

Prerequisite: M8 green (TUI native).

```
src/deeppyro/session_query/{__init__,query,filters,types}.py   843
src/deeppyro/builtin/session_query.py                          124
src/deeppyro/projection/{__init__,checkpoint,registry}.py     791
src/deeppyro/builtin/projections.py                            303
src/deeppyro/builtin/activity.py                               290
src/deeppyro/tui/history.py                                    354
```

- **Callsites:** TUI modules already retyped in M8 (`tui/{app,app_sessions,screens/thinking,screens/jobs,screens/plan,screens/team,widgets/agent_process,widgets/conversation,widgets/activity_bar,session_view,runtime}.py`); `tui/app_panels.py` lazy query import; `headless.py` projections wiring; bundle rows `session-query` (L36), `activity` (L206), `projections` (L210).
- **Tests:** delete `tests/session_query/*` (8 modules), `tests/projection/*` (6), `tests/builtin/{test_activity,test_projections}.py`, TUI tests already rewritten in M8 (`test_activity_bar`, `test_history`, `test_jobs_screen`, `test_plan_screen`, `test_team_screen` keep native versions).
- **Decision point:** D1. If the projection package is instead kept (retyped in M3), skip `projection/*`, `builtin/projections.py` and `tests/projection/*` here and keep `builtin/activity.py` only if a native consumer exists.
- **Gate:** TUI tests, `tests/integration`, full pytest.

#### W3 — v4 agent stack and its private capability helpers

Prerequisite: M4–M8 green.

```
src/deeppyro/agent/session.py            60
src/deeppyro/agent/session_state.py     268
src/deeppyro/agent/session_turn.py      469
src/deeppyro/agent/session_tools.py     377
src/deeppyro/agent/session_records.py   104
src/deeppyro/agent/session_support.py   191
src/deeppyro/agent/session_stream.py    358
src/deeppyro/agent/session_prompt.py    140
src/deeppyro/agent/request.py           245
src/deeppyro/agent/inbox.py             360
src/deeppyro/agent/retry.py             491
src/deeppyro/agent/stream.py            302
src/deeppyro/agent/images.py            348
src/deeppyro/agent/options.py            28
src/deeppyro/hooks/records.py           161
src/deeppyro/prompt/projection.py       263
src/deeppyro/compact/compactor.py       279   (v4 portion; may survive as native rewrite in M5)
src/deeppyro/compact/surface.py         334   (delete outright — no native analog)
src/deeppyro/compact/tokens.py           45   (delete outright — estimators moved to context_plan)
src/deeppyro/compact/refs.py            342   (v4 record API; native refs live in fold)
src/deeppyro/compact/compress_tool.py   616   (v4 record API; rewritten in M5)
src/deeppyro/compact/block_tools.py     305   (v4 block records; rewritten in M5)
src/deeppyro/goal/replay.py             429
src/deeppyro/goal/round_driver.py       932
src/deeppyro/llm/image_budget.py        261
```

- **Callsites:** `agent/__init__.py` re-exports; `goal/{__init__,service}.py`; `compact/{__init__,blocks}.py`; `builtin/compress.py`; `prompt/__init__.py`; `builtin/{sessions,session_title,inspect}.py` v4 branches (files stay); `goal/service.py` v4 branch; bundle rows `goal-round-driver` (L52), `compress` (L66) if it pointed at v4 classes, `hook-journal` already gone.
- **Tests:** delete/rewrite the v4 agent, goal, compact, prompt-projection, image-budget and hook-record suites listed in §5.2.
- **Gate:** `tests/native`, `tests/agent`, `tests/goal`, `tests/compact`(native), `tests/prompt`, `tests/hooks`, full pytest + differential.

#### W4 — v4 session format stack

```
src/deeppyro/session/__init__.py        105
src/deeppyro/session/session.py         218
src/deeppyro/session/store.py           223
src/deeppyro/session/writer.py          140
src/deeppyro/session/reader.py          159
src/deeppyro/session/events.py          385
src/deeppyro/session/derive.py          313
src/deeppyro/session/fork.py            287
src/deeppyro/session/layout.py          129
src/deeppyro/session/metadata.py        176
src/deeppyro/session/image_projection.py 145
```

- **Callsites:** by now only v4 modules/tests imported these; `session/__init__.py`'s v4 `__all__` disappears, native modules are imported directly or via `session/native_control.py`. `tests/session/support.py` and the v4 fixtures go with this wave (§5.3).
- **Tests:** delete `tests/session/{support,test_derive,test_derive_repair,test_events,test_fork,test_image_projection,test_invariants,test_metadata,test_reader,test_session_subscribe,test_store,test_store_info}.py`; `tests/session/test_native_schema.py` keeps its `test_read_rejects_v4_json_and_zstd` assertion but it now tests only the native reader's rejection path.
- **Gate:** `tests/session` (remaining native), full pytest, guard.

#### W5 — wire boundary and core contracts

```
src/deeppyro/core/wire.py        233
src/deeppyro/core/payloads.py    306
src/deeppyro/core/protocols.py   196
```

Plus re-export cleanup in `core/__init__.py`, `prompt/__init__.py`, `compact/__init__.py`, `agent/__init__.py`, `llm/__init__.py` and `session/__init__.py` (if any v4 names remain), and `tests/integration/test_wire_boundary.py` replaced by `tests/test_v4_free.py` with an empty allowlist.

- **Tests:** delete `tests/core/{test_wire,test_payloads}.py`, `tests/integration/test_wire_boundary.py` (superseded), any remaining v4 test imports. Keep `tests/core/test_events.py`.
- **Gate:** AST guard with empty allowlist, full pytest, ruff, typecheck.

#### W6 — packaging, config, fixtures, docs, de-brand

No module deletion; this wave makes the cut honest and installable:

- `pyproject.toml`: remove `zstandard>=0.23` (L11); remove the `per-file-ignores` rows for deleted files (`core/wire.py` L88, `session/store.py` L91, `session_query/types.py` L92, `builtin/compress.py` L93, `builtin/session_title.py` L94, `builtin/session_query.py` L95; `tests/agent/test_images.py` L99); bump version to 0.3.0.
- `bundle.toml`: delete rows whose module no longer exists (all listed in W1–W3); verify kernel boot with a missing optional plugin row is impossible.
- `config.py`: remove `sessionDir` mapping (L49), `session_dir` field (L234), sample, `session_dir()` helper (L488) and `__all__` entry (L643) per D2; keep `DEEPPYRO_SESSION_DIR` path env var (`paths.py` L20) because native sessions use it. Remove `[retry]` plugin documentation if `builtin/llm_retry` was its only consumer — verify native provider receives retry config through `headless.py` first.
- Fixtures: delete `tests/fixtures/synthetic/` (3 v4 sessions + 42 by-type records + manifest) and root `fixtures/` workflow; delete `tools/gen_fixtures.mjs`; rewrite the tests that read root fixtures (`tests/jobs/test_schema.py`, `tests/tools/{helpers,test_fixtures}.py`, `tests/user_questions/test_tool.py`, `tests/working_directory/test_tool.py`, `tests/llm/test_retry.py`, `tests/projection/support.py`) to native fixtures or add minimal native samples under `tests/fixtures/native/`.
- Packaging docs: `packaging/hiddenimports.py` docstring ("38 plugin modules", transitive zstandard/Pillow/httpx); `packaging/deeppyro.spec` unchanged.
- pyright: `scripts/typecheck.py --write` after deletions.
- Docs: full list in §4.2.
- Gate: full pytest, ruff, typecheck, native differential, `python -m build`/wheel smoke install (optional), TUI/headless smoke commands.

### 3.4 Why this order (import-graph evidence)

Iterative peeling of the deletion set by remaining src importers produces this partial order (computed from the AST graph): first modules with zero src importers (v4 LLM providers, migrate CLI/service, hook journal, retry plugin, history), then `session_query`, `projection`, `builtin/{projections,activity,sessions}`, then `tui/*`; only after those can the agent stack go, and only after the agent stack can the session format modules and `core/wire.py` go. `core/protocols.py` sits *after* the tool retype (M1) and *after* the whole agent stack; `core/payloads.py` after `tools/pipeline.py`'s hook payloads move (M1) and the v4 session writer/events. A Kahn-style ordering of individual modules gives 10 micro-waves; the six waves above group them at subsystem boundaries so each commit is reviewable.

## 4. Data and behavior that becomes unreadable or unavailable

### 4.1 Artifacts and capabilities lost in the hard cut

| Artifact / behavior | Current evidence | What 0.3.0 users get | Required action/note |
|---|---|---|---|
| v4 session logs: `session.v4.jsonl`, `session.v4.jsonl.zstd` | `session/layout.py:LOG_BASENAME="session.v4"`, `LOG_SUFFIXES=(".jsonl",".jsonl.zstd")`; codec in `session/{reader,writer,store,session}.py` | Permanently unreadable by 0.3.0; native readers reject the format (`SessionV4Error`; `tests/session/test_native_schema.py:test_read_rejects_v4_json_and_zstd`); sessions invisible in /sessions and TUI | Users migrate with **0.2.x** `dpyro migrate --to native --all` before upgrading; 0.3.0 release notes must state this in the first paragraph and the install/upgrade guide must gate the upgrade |
| `dpyro migrate` command | `tui/main.py:122-127` dispatches `session.migrate_cli.main` before argparse; `migrate.py` 1,443 L; bundle row `session-migrate` (bundle.toml L31) | Command removed (D2); invocation is an argparse error. The v4→native converter ships only in 0.2.x | `docs/guides/session-migration.md` archived (not deleted? see 4.2); new `docs/guides/upgrading-to-0.3.md` documents the two-step upgrade; `docs/native/schema.md` claim "no v4 reader/migrator" finally becomes true |
| zstandard-compressed logs and the `zstandard` dependency | `pyproject.toml` L11; `session/reader.py` L17/36-51/67/98-107; `session/writer.py` L19/27-28/80-101/120-126; `session_query/query.py` L36/434-441; `tests/integration/golden.py`; root fixtures | Dependency removed (D4); `.jsonl.zstd` files openable by no shipped code; golden v4 fixture test gone | Remove dependency + transitive hidden-import; delete `tools/gen_fixtures.mjs` and the root-fixture workflow; `docs/ops/telemetry.md` dependency list and `docs/spec/frozen-distribution.md` transitive list updated |
| Anthropic transport | `llm/registry.py:create_native_provider` (L96-116) supports only `_OPENAI_COMPAT_APIS` and raises `ConfigError("... has no native (KI-010) transport; use the legacy v4 loop for this provider")`; `headless._resolve_native_provider` (L695-716) catches it and falls back to v4 | After the cut there is no v4 fallback. An Anthropic-configured install either (a) runs if a native transport landed in M-waves, or (b) must fail at boot with an actionable error, not a silent provider downgrade | D3. If (b): config validation must reject the provider with a message naming the replacement and the last compatible release; release notes list it as a breaking change; `docs/spec/llm.md` provider matrix rewritten |
| v4 TUI history (`/thinking`, `/process`, reasoning progress lines, `/resume` from v4 logs) | `tui/history.py` 354 L rebuilds reasoning from `assistant/message` thinking parts; imported by `tui/{app,app_sessions,screens/thinking,widgets/agent_process,widgets/conversation}.py` | Reconstructed from native `session.fold` `SessionView` + trace store; old v4 logs unreadable so old conversations cannot be resumed at all | M8 rewrites the five consumers; `docs/spec/tui.md` / `tui-widgets.md` describe the trace-backed history |
| Per-session hook journal (`hook/invoked`, `hook/result` session records) | `builtin/hook_journal.py` 77 L writes into the v4 `AgentSession`; `hooks/records.py` 161 L builds records | Hook journal moves to the trace store (design: `tui-native-runtime.md` L1557); the TUI `/hooks` view is trace-backed, not per-session | `docs/spec/hooks.md` and `docs/spec/events.md` tables rewritten; release notes mention the view change |
| Image offload + image budget | `agent/images.py:plan_request_images`; `llm/image_budget.py` (`ImageBudget`, `required_image_offload`); `session/image_projection.py`; `image/offload` event in `core/wire.py`; `agent/session_stream.py` L35/L58-73 | Native providers still send images (native `ImagePart` carries `media_type`, resolved by `llm/attachments.py`), but **offloading large images to host files and per-request image accounting are removed** | Mark `docs/spec/image-budget.md` + `docs/spec/image-offload.md` as archived/removed in 0.3; release note "image offload removed"; no native replacement planned per design (attachments plugin says offload is phase 2 and not implemented natively) |
| v4 projection sidecars (`.projections.json`, version 1) | `projection/checkpoint.py` atomic sidecar next to the session log; `builtin/projections.py` | Deleted subsystem (D1); orphaned sidecar files can be deleted by users; if D1 is reversed, the sidecar format must be redefined against native events and the version bumped so old files are ignored/rebuilt | Release note "projection sidecars removed"; `docs/spec/projection.md` rewritten as native fact derivation or deleted |
| v4 session query CLI/plugin (`builtin/session_query`, `session_query/*`) | plugin row bundle.toml L36; TUI lazy import `tui/app_panels.py:139` | Query capability becomes a native library (`session/native_query.py`) used by the TUI, with no user-facing v4 query command | `docs/spec/session-query.md` rewritten or deleted; no release-note action if it was internal-only |
| `sessionDir` config key and `session_dir()` helper | `config.py` TOML mapping L49, dataclass field L234, sample, helper L488, `__all__` L643; used by v4 store + migrate | Removed (D2/D6). Keep `DEEPPYRO_SESSION_DIR` (`paths.py` L20) — native sessions use the same env-var path. A user config with `sessionDir` must not silently split session locations | Config loader should raise/warn with the removed-key name in 0.3.0; `docs/guides/config-migration.md` updated |
| `--legacy-v4`, `--native-loop`, `DEEPPYRO_NATIVE_LOOP`, `headless.native_loop_enabled` | `tui/main.py` L48-58, L141-159, note L161-167; `headless.py` L12, L102-104, L129-146, L1236 | Removed (D6). `--legacy-v4` becomes an argparse error; the env var is ignored (native is the only loop). No `DEEPPYRO_TUI_NATIVE` exists in src (design docs only) | `docs/guides/native-loop-default.md` archived; `docs/spec/headless.md` flags table trimmed; release notes list removed flags |
| v4 bus payloads / team member stream mirrors / per-record token accounting / inbox next-step events | `core/payloads.py` TypedDicts; `team/manager.py`; `agent/inbox.py` events; design behavior-change section 9.3/9.4/9.8 | No native representation; token/latency/retry/cost live only in trace store (`ModelCallRecord`, `ToolCallRecord`, `UsageSink.TurnUsage`); team member streams are not mirrored into the parent session | Documented as behavior changes in release notes (design §9); no data migration possible for v4 logs (unreadable anyway) |
| 0.2 TUI reading 0.3 sessions | native schema-1 sessions were already readable by 0.2 native loop, but 0.2 TUI is a v4 client | Downgrading 0.3 → 0.2 loses TUI session visibility | Release note: downgrade unsupported; back up before upgrade |

### 4.2 Documentation changes

Authoritative starting point: `docs/README.md` §6 (L80–90). This table completes it with files found by grep during the analysis.

| Document | Required change | Reason |
|---|---|---|
| `docs/README.md` | Complete §6 deletion list; update the §4 status labels and §7 recent changes; remove claims that v4 docs are "current" | Authoritative doc deletion list for 0.3; §6 currently a plan, not fact |
| `docs/spec/INTERFACES.md` | DELETE | v4 wire interfaces (frozen migration reference; 0.3 target has no v4 reader) |
| `docs/spec/session.md` | DELETE | v4 session format spec; replaced by docs/native/schema.md |
| `docs/spec/dsh-event-catalog.md` | DELETE | generated DSH catalog; generation chain removed (D5) |
| `docs/spec/dsh-event-catalog-unresolved.md` | DELETE | same |
| `docs/spec/loop.md` | REWRITE as native loop spec (or delete + point at design/native docs) | transition spec, v4 record flow |
| `docs/spec/events.md` | REWRITE: native typed events + trace store; drop v4 bus payload tables | v4 records/event shapes |
| `docs/spec/hooks.md` | REWRITE: bus hook modes + trace-backed journal; delete hook/invoked+result session records | hook journal now trace-only |
| `docs/spec/projection.md` | DELETE (D1) or rewrite as native fact derivation | projection package deleted |
| `docs/spec/session-query.md` | DELETE or rewrite as native query library | session_query package deleted |
| `docs/spec/compact-blocks.md` | REWRITE as native compaction (fold/context_plan); delete v4 block/compaction records | compaction rewritten in M5 |
| `docs/spec/image-budget.md` | ARCHIVE/remove; note feature removed | image budget deleted with v4 provider layer |
| `docs/spec/image-offload.md` | ARCHIVE/remove; note feature removed | image offload deleted with v4 session stack |
| `docs/spec/llm.md` | REWRITE provider matrix: native providers only; Anthropic status per D3; remove v4 converters | v4 provider layer deleted |
| `docs/spec/tui.md` | REWRITE: native session/boot/history; remove migrate tip (L11) and v4 note; /compact flow | TUI native switch M8 |
| `docs/spec/tui-widgets.md` | Remove tui/history references; describe fold/trace-backed widgets | tui/history deleted |
| `docs/spec/headless.md` | Remove --legacy-v4/--native-loop/DEEPPYRO_NATIVE_LOOP; native is only path | flags removed (D6) |
| `docs/spec/plugin-core.md` | Remove "v4 wire/protocols" plugin assumptions; point at core.llm/tools contract | plugin contract changed (core.protocols gone) |
| `docs/spec/activity.md` | REWRITE without ActivityService/tui/history; facts come from fold + plugin rows | activity service deleted |
| `docs/spec/README.md` | Update status table: 4 frozen docs deleted, 5 transition docs rewritten; fix stale "spec/ = 39 files" counts | doc structure changes |
| `docs/spec/frozen-distribution.md` | Remove zstandard/Pillow/httpx transitive dependency claims tied to v4 plugins | dependency graph changes |
| `docs/adr/0001-three-track-native-agent.md` | Update importer counts / "remaining" statements; ADR decision unchanged | counts stale after cut |
| `docs/guides/session-migration.md` | ARCHIVE; supersede with upgrading-to-0.3 guide (migrate on 0.2.x first) | dpyro migrate removed (D2) |
| `docs/guides/native-loop-default.md` | ARCHIVE; native is the default and only loop | flags removed (D6) |
| `docs/guides/config-migration.md` | Document removed sessionDir key and env-var behavior | config key removed |
| `docs/guides/bundle-signing.md` | Check no deleted plugin ids are used as examples | bundle rows deleted |
| `docs/release/0.2.0.md` | Leave as historical 0.2.0 record; do not retro-edit promises | release history |
| `docs/release/0.2.0-known-issues.md` | Close KI-001, KI-002, KI-010; note KI-032 status; rewrite §0.3 next steps as done | this release resolves them |
| `docs/release/dsh-audit.md` | DELETE or archive after de-branding (per docs/README §6) | DSH audit obsolete |
| `docs/release/0.3.0.md` | NEW: breaking changes (v4 logs, migrate, Anthropic, image offload, flags, sessionDir), upgrade order, known issues | user-facing release contract |
| `docs/native/README.md` | Remove "TUI still v4"; update module list/status | TUI switched |
| `docs/native/schema.md` | Remove statements about v4 compatibility/migrate drift; state v4 rejected | format contract |
| `docs/native/coverage.md` | Update coverage cells for capabilities ported in M-waves | coverage table |
| `docs/ops/telemetry.md` | Remove zstandard from dependency list; update trace-store claims | dependency change |
| `docs/plugins/README.md` | Remove v4 payload/wire plugin examples if any | plugin API changed |
| `docs/design/native-capabilities.md` | Mark P9 done; remove stale counts; align projection decision (D1) | design record |
| `docs/design/tui-native-runtime.md` | Mark P6 done; align projection decision; remove planned-but-not-real file names | design record |
| `docs/design/native-provider-port.md` | Mark KI-010 scope closure; Anthropic status per D3 | design record |
| `docs/design/native-host.md, tui-native-migration.md` | Remove DEEPPYRO_TUI_NATIVE plan references (flag never implemented in src) | stale plan |
| `README.md (repo root)` | Remove v4/zstd/flags mentions; update feature matrix and quickstart | user entry point |
| `fixtures/native/README.md` | Keep; clarify it is the only fixture corpus after root fixtures are removed | fixtures |
| `tools/gen_fixtures.mjs` | DELETE with root-fixture workflow | v4 fixture generator |
| `scripts/extract_dsh_events.mjs` | DELETE (defaults to core/wire.py; part of catalog generation chain) | D5 generation chain |
| `scripts/gen_event_catalog.py` | DELETE (regenerates core/event_catalog.py from the mjs dump) | D5 generation chain |
| `scripts/differential_matrix.py` | Update wording "native/v4 differential" after v4 side is gone; keep matrix as native regression mapping | CI wording |
| `docs/archive/research/*.md` | Leave as historical archive; only fix links that claim v4 is current | archive policy |

### 4.3 Migration note users will need

0.3.0 should ship a short, copy-pastable upgrade sequence: (1) on 0.2.x run `dpyro migrate --to native --all --dry-run` then for real; (2) verify `~/.dpro/sessions/*.jsonl` (or `DEEPPYRO_SESSION_DIR`) contains schema-1 logs; (3) confirm the configured provider has a native transport (OpenAI-compatible today; Anthropic only if D3 option (a) landed); (4) remove `--legacy-v4` / `DEEPPYRO_NATIVE_LOOP` from scripts and `sessionDir` from config; (5) upgrade and check `/sessions`. Keep 0.2.x installed until the v4 logs are migrated because 0.3 cannot read them.

## 5. Test modules: disappear vs rewrite, and the final test-count delta

### 5.1 Measurement method and caveat

Counts are static AST counts over `tests/` (modules = files; "fns" = top-level `def test_*` / `async def test_*`), not pytest-collected counts. Parametrization, fixtures, and `pytest.mark.parametrize` expansion mean collected counts differ; the delta below is structural and should be re-measured on the real suite with `pytest --collect-only -q` in W0 and after W5. Baseline: **352 test modules / 3,656 static test functions**; 137 modules / 1,447 fns touch at least one v4-exclusive module.

### 5.2 Classification (hard-cut recommendation)

| Category | Modules | Static fns | Meaning |
|---|---|---|---|
| DELETE | 62 | 663 | subject is v4-only; native successor suites exist or the behavior is intentionally gone |
| REWRITE | [object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object] | 461 | file stays; subject (session/agent/TUI surface) stays but assertions move to native facts |
| RETYPE | [object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object],[object Object] | 314 | file and assertions stay; only the import of `core.protocols` changes to `tools/contract` |
| KEEP | 1 | 9 | `tests/core/test_events.py` — imports only `core.event_catalog.BUS_EVENTS` (D5 keeps it) |

The hard-cut column moves 10 modules from "rewrite/retype" to DELETE relative to a soft reading, because their subject is deleted and the native successor suite already covers the behavior: `tests/agent/test_agent_loop.py` (17), `tests/agent/test_loop_v4_usage.py` (3), `tests/agent/test_session_hooks_p1.py` (8), `tests/agent/test_session_split_snapshot.py` (3), `tests/agent/test_turn_conclude.py` (3), `tests/agent/test_pre_step_steps.py` (2), `tests/builtin/test_hook_journal.py` (15), `tests/hooks/test_records.py` (17), `tests/llm/test_openai_compat.py` (29), `tests/projection/test_fork.py` (3). If the team prefers to keep parity tests for the native replacement, only `test_openai_compat` and `test_session_hooks_p1` are worth rewriting instead (their subject, streaming/tool-call assembly and hook ordering, survives natively).

### 5.3 Modules that disappear (62 modules / 663 fns)

| Test module | fns |
|---|---|
| `tests/agent/fakes.py` | — |
| `tests/agent/test_agent_images.py` | 5 |
| `tests/agent/test_agent_loop.py` | 17 |
| `tests/agent/test_agent_p0.py` | 11 |
| `tests/agent/test_compact.py` | 12 |
| `tests/agent/test_hook_records.py` | 7 |
| `tests/agent/test_images.py` | 19 |
| `tests/agent/test_inbox_events.py` | 2 |
| `tests/agent/test_inbox.py` | 22 |
| `tests/agent/test_loop_v4_usage.py` | 3 |
| `tests/agent/test_pre_step_steps.py` | 2 |
| `tests/agent/test_request.py` | 17 |
| `tests/agent/test_retry.py` | 32 |
| `tests/agent/test_session_hooks_p1.py` | 8 |
| `tests/agent/test_session_split_snapshot.py` | 3 |
| `tests/agent/test_session_write_fidelity.py` | 5 |
| `tests/agent/test_step_cap.py` | 3 |
| `tests/agent/test_stream.py` | 26 |
| `tests/agent/test_task_service.py` | 10 |
| `tests/agent/test_turn_conclude.py` | 3 |
| `tests/builtin/test_activity.py` | 8 |
| `tests/builtin/test_hook_journal.py` | 15 |
| `tests/builtin/test_llm_retry.py` | 22 |
| `tests/core/test_payloads.py` | 7 |
| `tests/core/test_wire.py` | 5 |
| `tests/goal/test_goal_replay.py` | 14 |
| `tests/hooks/test_records.py` | 17 |
| `tests/integration/test_builtin_hooks_real.py` | 2 |
| `tests/integration/test_golden.py` | 3 |
| `tests/llm/helpers.py` | — |
| `tests/llm/test_anthropic.py` | 30 |
| `tests/llm/test_fixtures.py` | 4 |
| `tests/llm/test_image_budget.py` | 27 |
| `tests/llm/test_messages_anthropic.py` | 26 |
| `tests/llm/test_messages.py` | 32 |
| `tests/llm/test_openai_compat.py` | 29 |
| `tests/projection/activity_defs.py` | — |
| `tests/projection/support.py` | — |
| `tests/projection/test_activity_parity.py` | 2 |
| `tests/projection/test_fork.py` | 3 |
| `tests/ptc/test_records.py` | 2 |
| `tests/session_query/conftest.py` | — |
| `tests/session_query/support.py` | — |
| `tests/session_query/test_events.py` | 13 |
| `tests/session_query/test_hook_trace.py` | 9 |
| `tests/session_query/test_plugin.py` | 6 |
| `tests/session_query/test_session_read.py` | 6 |
| `tests/session_query/test_summaries.py` | 9 |
| `tests/session_query/test_tracing.py` | 8 |
| `tests/session/support.py` | — |
| `tests/session/test_derive_repair.py` | 8 |
| `tests/session/test_derive.py` | 17 |
| `tests/session/test_events.py` | 19 |
| `tests/session/test_fork.py` | 13 |
| `tests/session/test_image_projection.py` | 26 |
| `tests/session/test_invariants.py` | 11 |
| `tests/session/test_metadata.py` | 7 |
| `tests/session/test_migrate.py` | 16 |
| `tests/session/test_reader.py` | 12 |
| `tests/session/test_session_subscribe.py` | 4 |
| `tests/session/test_store_info.py` | 4 |
| `tests/session/test_store.py` | 20 |

7 of these are helper modules with no test functions (they are deleted or rebuilt wire-free): `tests/agent/fakes.py`, `tests/llm/helpers.py`, `tests/projection/activity_defs.py`, `tests/projection/support.py`, `tests/session_query/conftest.py`, `tests/session_query/support.py`, `tests/session/support.py`.

### 5.4 Modules that are rewritten (44 / 461 fns)

| Test module | fns |
|---|---|
| `tests/agent/test_ptc_surface.py` | 6 |
| `tests/builtin/test_agent_preset_filters.py` | 22 |
| `tests/builtin/test_inspect.py` | 15 |
| `tests/builtin/test_preset_revisions.py` | 8 |
| `tests/builtin/test_projections.py` | 5 |
| `tests/builtin/test_session_title.py` | 7 |
| `tests/compact/support.py` | — |
| `tests/compact/test_block_tools.py` | 31 |
| `tests/compact/test_compress_tool.py` | 25 |
| `tests/compact/test_refs.py` | 11 |
| `tests/goal/conftest.py` | — |
| `tests/goal/test_driver_integration.py` | 2 |
| `tests/goal/test_round_driver.py` | 28 |
| `tests/integration/test_agent_presets.py` | 3 |
| `tests/integration/test_e2e_agent.py` | 1 |
| `tests/integration/test_event_harness.py` | 1 |
| `tests/integration/test_kernel_bundle.py` | 4 |
| `tests/integration/test_p0_bus_alignment.py` | 2 |
| `tests/integration/test_p1_bus.py` | 6 |
| `tests/integration/test_p2_bus.py` | 4 |
| `tests/integration/test_plugin_services.py` | 3 |
| `tests/integration/test_preset_modes.py` | 5 |
| `tests/kernel/test_bundle.py` | 5 |
| `tests/llm/test_registry.py` | 34 |
| `tests/native/synthetic.py` | — |
| `tests/projection/test_checkpoint.py` | 14 |
| `tests/projection/test_registry.py` | 13 |
| `tests/prompt/test_projection.py` | 26 |
| `tests/subagent/helpers.py` | — |
| `tests/subagent/test_handoff.py` | 9 |
| `tests/subagent/test_usage.py` | 10 |
| `tests/team/helpers.py` | — |
| `tests/team/test_usage.py` | 6 |
| `tests/test_session_title.py` | 8 |
| `tests/tui/test_activity_bar.py` | 9 |
| `tests/tui/test_app.py` | 35 |
| `tests/tui/test_conversation.py` | 14 |
| `tests/tui/test_history.py` | 8 |
| `tests/tui/test_jobs_screen.py` | 11 |
| `tests/tui/test_plan_screen.py` | 6 |
| `tests/tui/test_ptc_ui.py` | 13 |
| `tests/tui/test_runtime_presets.py` | 22 |
| `tests/tui/test_team_screen.py` | 19 |
| `tests/tui/test_thinking_screen.py` | 10 |

### 5.5 Modules whose imports are only retyped (30 / 314 fns)

| Test module | fns |
|---|---|
| `tests/agent/test_prompt.py` | 7 |
| `tests/builtin/test_hook_observability.py` | 18 |
| `tests/goal/test_goal_policy.py` | 5 |
| `tests/integration/fake_provider.py` | — |
| `tests/integration/test_fake_provider.py` | 5 |
| `tests/mcp/test_alignment.py` | 16 |
| `tests/mcp/test_bridge.py` | 9 |
| `tests/memory/test_long_term_tools.py` | 17 |
| `tests/memory/test_tools.py` | 15 |
| `tests/native/replay.py` | — |
| `tests/native/test_budget.py` | 9 |
| `tests/native/test_checkpoint_resume.py` | 7 |
| `tests/native/test_context_plan.py` | 6 |
| `tests/native/test_loop.py` | 13 |
| `tests/native/test_task_handoff_native.py` | 4 |
| `tests/observability/test_telemetry.py` | 8 |
| `tests/prompt/test_prompt_direct_tools.py` | 4 |
| `tests/ptc/helpers.py` | — |
| `tests/ptc/test_executor.py` | 11 |
| `tests/ptc/test_plugin.py` | 14 |
| `tests/ptc/test_sdk.py` | 32 |
| `tests/ptc/test_tool.py` | 18 |
| `tests/schedule/test_tool.py` | 10 |
| `tests/tools/helpers.py` | — |
| `tests/tools/test_goal_tools.py` | 25 |
| `tests/tools/test_pipeline_hooks.py` | 26 |
| `tests/tools/test_pipeline.py` | 11 |
| `tests/tools/test_registry.py` | 6 |
| `tests/user_questions/test_tool.py` | 8 |
| `tests/working_directory/test_tool.py` | 10 |

### 5.6 New suites required by the design docs

The two design docs list the following new test files; they are part of migration waves, not deletion waves: `tests/tui/{test_native_boot,test_native_turn,test_native_hooks,test_native_system_prompt,test_native_team_restore,test_native_fork_command}.py`, `tests/agent/{test_native_records,test_native_factory}.py`, `tests/compact/{test_native_compactor,test_native_compress}.py`, `tests/session/test_native_control.py`, `tests/integration/test_tui_native_parity.py`, plus this plan's `tests/test_v4_free.py` guard. Estimate: 12 files, +60–120 static test functions.

### 5.7 Expected delta

| Metric | Baseline | After W5 (hard cut) |
|---|---|---|
| Test modules | 352 | 352 − 62 + ~12 = **~302** |
| Static test functions | 3,656 | 3,656 − 663 + ~90 = **~3,083** (range 2,993–3,123) |
| v4-touching test modules | 137 / 1,447 fns | **0** (guard-enforced) |

The upper bound of the range assumes rewritten suites keep their function count; in practice the native rewrites are expected to shrink because v4-only cases disappear (e.g. `tests/projection/*` 42 fns → 0, `tests/session_query/*` 58 → 0) and grow in a few places (native differential rows). Net **−530 to −575 static functions**, before accounting for parametrization.

### 5.8 Test helper modules (hidden coupling)

These helpers import v4 modules and are imported by kept/native suites, so they must be rebuilt wire-free in M1/M0, not deleted silently:

| Helper | v4 imports | Imported by |
|---|---|---|
| `tests/agent/fakes.py` | wire, session.derive, session.events | 14 modules incl `tests/native/test_headless_native.py`, `tests/observability/test_telemetry.py`, `tests/agent/test_loop_v4_usage.py` |
| `tests/session/support.py` | session layout/reader/writer/derive/events/store | `tests/session_query/*` (deleted), v4 session tests (deleted) |
| `tests/llm/helpers.py` | core.wire | `tests/llm/*` incl native `test_native_provider.py` |
| `tests/native/synthetic.py` (1,315 L) | wire, derive, reader | `tests/native/test_differential_synthetic.py` |
| `tests/tools/helpers.py` | root v4 fixtures (JSONL samples) | 16 modules incl approval-policy, core/test_payloads, team/subagent tool tests |
| `tests/team/helpers.py`, `tests/subagent/helpers.py` | AgentSession/SessionStore | team/subagent suites |
| `tests/integration/fake_provider.py` | core.protocols LlmProvider | integration suites |
| `tests/goal/conftest.py` | agent.inbox, core.wire | `tests/goal/*` |
| `tests/compact/support.py` | SessionStore/compaction records | `tests/compact/*` |
| `tests/projection/{support,activity_defs}.py` | SessionStore/projection API | `tests/projection/*` |

## 6. Hidden coupling traps

Each trap below is something a deletion wave will break *without an import error in the file being deleted*. Treat this section as the pre-flight checklist for the cleanup agent and the rationale for the guard design in §7.

### 6.1 Lazy imports and string-based loading

Only two **lazy imports** of v4 modules exist in `src/` (function-body `ImportFrom`), but both are invisible to a naive top-of-file grep:

| Location | Mechanism | Target |
|---|---|---|
| `tui/app_panels.py:139` | lazy `from deeppyro.builtin.session_query import ...` | v4 session-query plugin |
| `tui/main.py:125` | lazy `from deeppyro.session.migrate_cli import main` | v4 migrator |

String-based loading is broader and is the real trap: plugin modules are named in `src/deeppyro/bundles/core/bundle.toml` and imported with `importlib.import_module` — `plugins/manager.py:138`, `kernel/composition.py:496/504`, `kernel/manifest.py:81`, `plugins/cli.py:169`, `tui/ui_package.py:463`. A deleted plugin module whose row is still in `bundle.toml` is a **kernel boot failure**, not an import error at the deletion site. Bundle rows must be deleted in the same commit as the module (W1–W3 list them).

### 6.2 Package re-export hubs (undercount trap)

The v4 stack re-exports symbols through package `__init__.py` files, so "which module does this import" cannot be answered from the module path alone:

| Hub | v4 re-exported symbols |
|---|---|
| `deeppyro.session` | 45 (`Session`, `SessionStore`, `SessionInfo`, `derive_messages`, `fork_session`, `read_session_file`, `LOG_BASENAME`, zstd helpers, event constants…) |
| `deeppyro.core` | 34 (`core.protocols` stream/tool types + `core.wire` model and report types) |
| `deeppyro.llm` | 19 (v4 stream events + `AnthropicProvider`/`OpenAICompatProvider` + native provider) |
| `deeppyro.compact`, `deeppyro.projection`, `deeppyro.prompt`, `deeppyro.goal`, `deeppyro.agent` | 18 / 15 / 9 / 4 / 4 |

Measured impact: module-path matching alone found 102 v4-touching test modules; resolving `from deeppyro.session import Session` to the defining module found **137**. The final guard must resolve symbols through package `__init__` re-exports, or it will pass while tests still import deleted modules.

### 6.3 Entry points, flags, environment variables

- `pyproject.toml` `[project.scripts]`: `dpyro` and `deeppyro` → `deeppyro.tui.main:main` (unchanged by the cut).
- `tui/main.py`: `--legacy-v4` (L48-53), `--native-loop` deprecated alias (L54-58), backend selection (L141-159), "TUI still runs the v4 path" note (L161-167), lazy migrate dispatch (L122-127). All removed in M6/M8.
- `headless.py`: `NATIVE_LOOP_ENV="DEEPPYRO_NATIVE_LOOP"` (L102-104), `native_loop_enabled()` (L129-146, in `__all__` L1236), v4 fallback branch (L851/L1080), docstring (L12). Removed in M6.
- `DEEPPYRO_TUI_NATIVE` appears **only in design docs** (`native-host.md`, `tui-native-migration.md`), never in `src/` — do not write a test for it.
- `DEEPPYRO_SESSION_DIR` / `DEEPPYRO_HOME` (`paths.py` L18/L20) are native and stay; do not delete them with `sessionDir`.

### 6.4 The pyright baseline is a second allowlist

`pyright-baseline.json` accepts 79 counted errors across 40 files; **26 of them are on 11 files this plan deletes**: `agent/inbox.py` (4), `builtin/sessions.py` (1), `compact/compactor.py` (1), `compact/compress_tool.py` (5), `llm/messages.py` (3), `llm/messages_anthropic.py` (1), `projection/checkpoint.py` (4), `projection/registry.py` (4), `session/events.py` (1), `session/fork.py` (1), `session/image_projection.py` (1). `scripts/typecheck.py` fails only on *new* diagnostics; deleting a baselined file leaves a "fixed" entry that is reported but does not fail. Run `scripts/typecheck.py --write` in the same wave so the baseline does not accumulate stale entries, and never treat "typecheck passes" as evidence that a wave is clean.

### 6.5 Bundle rows, kernel boot, and the control-plane wave

`bundle.toml` (227 L) is the string-loaded plugin graph. Rows that die with this plan: `session-migrate` (L31, critical=false), `session-query` (L36), `goal-round-driver` (L52, critical=false), `llm-retry` (L91), `hook-journal` (L101), `activity` (L206), `projections` (L210). Rows that change implementation: `sessions` (L26), `compress` (L66), `session-title` (L108), `agents` (L188), `team` (L192), `subagent` (L196), `task` (L201), `inspect` (L226). Rows that stay untouched: `kernel` (L16), `llm` (L22), `goal` (L48), hooks (L87), jobs (L113), presets (L121), prompt-service (L125), approval-policy (L140), ptc (L146), slots (L212-214).

The uncommitted control-plane wave touches `kernel/{app,bundle_schema,capabilities,composition}.py`, `plugins/settings_store.py` and adds bundle signing/group-patch tests. Two interactions are possible: (a) row deletion changes bundle digests/signatures asserted by the new tests or by `docs/guides/bundle-signing.md` examples; (b) composition may group/cache rows by id. **Sequencing rule:** land the control-plane wave first (W0), rerun `tests/kernel/test_bundle*.py`, then delete rows per wave and rerun the same tests. Deleting rows also automatically shrinks `packaging/hiddenimports.py` output because it derives hidden imports from bundle rows; only its docstring ("38 plugin modules", transitive `httpx`/`Pillow`/`zstandard`) and `docs/spec/frozen-distribution.md` need wording updates (W6).

### 6.6 Config keys and the fixture corpus

- `sessionDir` → `config.py` mapping (L49), dataclass field (L234), helper (L488), `__all__` (L643). Delete with migrate (W6); keep the env var.
- `[retry]` config section: today only `builtin/llm_retry.py` reads it; native retry policy is bound in `headless.py` (`("retryPolicy", RetryPolicy)` from `llm/retry.py` L74/L642). Verify the native provider actually receives the configured retry policy before removing the section/docs; otherwise delete only after a native retry-config test exists.
- Root `fixtures/` is 88 files / ~23 MB, **mostly untracked** (`.gitignore` ignores `fixtures/*` except `fixtures/native/**`); `tests/fixtures/synthetic/` is the tracked v4 corpus (3 sessions + 42 by-type records + manifest). Tests that read root fixtures but are *not* v4-only: `tests/jobs/test_schema.py` (request_header sample), `tests/tools/helpers.py` + `tests/tools/test_fixtures.py`, `tests/user_questions/test_tool.py`, `tests/working_directory/test_tool.py`, `tests/llm/test_retry.py` (`llm_retry.jsonl` — `llm/retry.py` itself is kept), and `tests/projection/support.py`. They need native samples under `tests/fixtures/native/` before `tools/gen_fixtures.mjs` and the root corpus are removed. `tests/session/support.py` prefers the root corpus when present and falls back to `tests/fixtures/synthetic`; both die with the session stack.

### 6.7 pyproject and packaging surfaces

- `zstandard>=0.23` (L11) — remove (D4).
- `per-file-ignores` rows naming deleted files: `src/deeppyro/core/wire.py` (L88), `session/store.py` (L91), `session_query/types.py` (L92), `builtin/compress.py` (L93), `builtin/session_title.py` (L94), `builtin/session_query.py` (L95), `tests/agent/test_images.py` (L99). Ruff does not fail on stale globs, so these silently accumulate — remove them in W5/W6.
- `[project.scripts]` unchanged; `[tool.hatch.build.targets.wheel]` packages `src/deeppyro` only (no file list to prune).
- `packaging/deeppyro.spec` needs no edit; it calls `collect_hiddenimports`.

### 6.8 Event-catalog generation chain

`core/event_catalog.py` is generated data consumed by the native `core/events.py`; the generation chain is `docs/spec/dsh-event-catalog.md` → `scripts/extract_dsh_events.mjs` (its default `records` path is `src/deeppyro/core/wire.py`) → `scripts/gen_event_catalog.py` → `core/event_catalog.py`. Delete the scripts and both catalog docs, keep the committed table, and remove "generated" wording/docstrings (D5). `licenses`/header comments in the table may mention the generator.

### 6.9 Docs examples and stale counts

Deleted-module path strings appear in 13 docs files (ADR 0001, design docs ×2, guides ×2, plugin README, spec ×6) and deleted plugin ids appear in bundle-signing examples. The release-known-issues doc and ADR 0001 carry importer/test counts that this cut invalidates (e.g. "39 importers", "38 files"). `docs/spec/session-query.md` claims a test count; `docs/spec/README.md` has stale spec file counts (39 vs actual). Rewrite in W6 (§4.2) and grep for `session.v4`, `core.wire`, `core.protocols`, `core.payloads`, `dpyro migrate`, `--legacy-v4` as a final documentation gate.

### 6.10 Test-side traps

- `tests/agent/fakes.py` is imported by **14 modules**, including native `tests/native/test_headless_native.py` and `tests/observability/test_telemetry.py`; it imports `core.wire` and session modules. Native suites silently depend on the v4 stack through test fixtures.
- `tests/native/synthetic.py` (1,315 L) builds v4-shaped synthetic sessions for the native differential suite; it must be rebuilt on native events before `session/*` deletion (W4), and the differential fixtures under `tests/fixtures/native/synthetic/` stay (they are native).
- `tests/llm/helpers.py` imports `core.wire` and is used by the **native** `tests/llm/test_native_provider.py`.
- Test-function counts in docs/design docs are stale and differ from statically counted fns; re-measure with `pytest --collect-only -q` in W0 and W5.

### 6.11 Working-tree state

At the analysis baseline the tree has 46 modified and 21 untracked files, including all new native modules (`agent/{model_call,native_factory,native_records,turn_messages}.py`, `core/llm.py`, `llm/{attachments,http,native_messages,native_provider}.py`, `session/{native_fork,native_query,native_control}.py`) and deleted `agent/native_provider.py`. Consequences: (a) commit or stash the wave before starting W1 so every wave is a reviewable diff; (b) the guard test is stale because it still lists the deleted file — fix in W0; (c) this document itself is a new untracked file that must be committed with the plan, not with a deletion wave.

### 6.12 Import-graph blind spots the guard must cover

1. `if TYPE_CHECKING:` imports (e.g. `core/payloads.py` docstring says domain modules import it behind TYPE_CHECKING) — they still break at delete time and must be flagged.
2. `__all__` string re-exports — a symbol exported but not imported is still a public API break; guard should compare `__all__` against module existence.
3. Function-body (lazy) imports — §6.1.
4. String module paths in `bundle.toml` and any `importlib.import_module("deeppyro.…")` literal.
5. `from package import symbol` re-export resolution — §6.2.
6. Docs/scripts that name deleted modules — not code, but `scripts/extract_dsh_events.mjs` defaults to `core/wire.py`, and a stale generator is a lying artifact.

## 7. Cleanup checklist (execute top to bottom)

Work on a branch (e.g. `chore/v4-exit`), one commit per wave, with the gate run before each commit. Never combine two waves in one commit: the guard allowlist diff is the review artifact.

### C0 — Pre-flight (no source deletion)

1. Commit or stash the uncommitted wave; confirm `git status` clean. Record `HEAD` and `pyproject.toml` version.
2. Record baselines: `git rev-parse HEAD > /tmp/0.3-baseline.txt`; `.venv\Scripts\python.exe -m pytest --collect-only -q | tail -n 3` (note collected count); `git hash-object pyright-baseline.json`.
3. Land the control-plane wave and re-run `tests/kernel/test_bundle*.py` (§6.5) before touching bundle rows.
4. Fix `tests/integration/test_wire_boundary.py`: remove `llm/native_provider.py` from `NATIVE_FOOTPRINT` and `NATIVE_PROTOCOL_MODULES` (it is already deleted from the tree), or replace the file now with the §7.2 guard in allowlist mode. Whichever: **the guard must pass on the current tree before any deletion**.
5. Decide D1 (projection), D2 (migrate), D3 (Anthropic), D7 (compact module disposition, below). Record the decision in this doc or the release notes; do not start W1 with these open.
6. Refresh the inventory: run the §A.1 script (or equivalent AST scan) and diff its module list against §1; if a module was added/removed since this analysis, update the wave assignment before proceeding.

### C1 — Wave procedure (repeat per wave W1–W6)

For every wave:

1. `git switch -c` a wave branch (or commit per wave on the deletion branch).
2. Recompute remaining importers of every module the wave will delete:
   `.venv\Scripts\python.exe - << PY` … AST scan from §A.1 filtered to the wave's names … `PY`. The result must be empty except modules deleted in the same wave; if not, move that importer to an earlier migration wave.
3. Grep for lazy and string references: `Select-String -Path src/**/*.py -Pattern "core\.wire|core\.protocols|core\.payloads|session\.migrate|tui\.history|projection"` (the AST scan covers lazy imports; this catches strings/comments that must be cleaned).
4. Delete the files listed for the wave.
5. Edit the call sites listed for the wave (plugin implementations, hub `__init__`, bundle rows, ruff ignores).
6. Delete or rewrite the wave's test modules (§5.3/§5.4 lists) and rebuild the test helper modules that import v4 modules (§5.8) **before** the wave that deletes their target.
7. Shrink `V4_MODULES` in `tests/test_v4_free.py` by exactly the deleted module paths; the guard must fail if the wave forgot a call site.
8. Run the wave gate: targeted pytest dirs, then `.venv\Scripts\python.exe -m pytest -q`; `.venv\Scripts\python.exe -m ruff check src tests`; `-m ruff format --check src tests`; `scripts/typecheck.py`; differential matrix; `tests/test_v4_free.py`. Only then commit.

### C2 — Wave W1: v4 LLM provider layer, retry plugin, migrator, hook journal

- [ ] Delete `src/deeppyro/llm/{anthropic,messages,messages_anthropic,openai_compat}.py` (1,503 L).
- [ ] Verify `llm/__init__.py` and `llm/registry.py` no longer export `AnthropicProvider`, `OpenAICompatProvider`, `create_provider`, v4 message builders (M2 work); `create_native_provider` remains the only factory.
- [ ] Delete `src/deeppyro/builtin/llm_retry.py`, `src/deeppyro/builtin/hook_journal.py`.
- [ ] Delete `src/deeppyro/migrate.py`, `src/deeppyro/session/migrate_cli.py`, `src/deeppyro/session/migrate_service.py`; remove `tui/main.py` L122-127 dispatch (M6).
- [ ] Remove bundle rows `session-migrate` (L31), `llm-retry` (L91) and `hook-journal` (L101).
- [ ] Delete tests: `tests/llm/{test_anthropic,test_messages,test_messages_anthropic,test_fixtures,helpers}.py`, `tests/builtin/{test_llm_retry,test_hook_journal}.py`, `tests/session/test_migrate.py`.
- [ ] Guard: remove `deeppyro.llm.{anthropic,messages,messages_anthropic,openai_compat}`, `deeppyro.builtin.{llm_retry,hook_journal}`, `deeppyro.migrate`, `deeppyro.session.{migrate_cli,migrate_service}` from `V4_MODULES`.
- [ ] Gate: `pytest -q tests/llm tests/builtin/test_llm.py tests/session/native_schema.py`, full pytest, guard.

### C3 — Wave W2: session query, projection/activity subsystem, TUI history

- [ ] Confirm M8 (TUI native switch) is green and `tui/*` no longer imports `tui.history`, `builtin.session_query`, `builtin.activity`, `builtin.projections` (AST scan).
- [ ] Delete `src/deeppyro/session_query/{__init__,query,filters,types}.py`, `src/deeppyro/builtin/session_query.py`.
- [ ] If D1=delete: delete `src/deeppyro/projection/{__init__,checkpoint,registry}.py`, `src/deeppyro/builtin/projections.py`, `src/deeppyro/builtin/activity.py`; remove bundle rows `session-query` (L36), `projections` (L210), `activity` (L206).
- [ ] Delete `src/deeppyro/tui/history.py`; remove remaining TUI imports (should be none).
- [ ] Delete tests: `tests/session_query/*` (8 modules), `tests/projection/*` (6 modules), `tests/builtin/{test_activity,test_projections}.py`; TUI suites already rewritten in M8 (`tests/tui/test_{activity_bar,history,jobs_screen,plan_screen,team_screen}.py` native versions stay).
- [ ] Ruff/pyright: remove stale ignores; `scripts/typecheck.py --write`.
- [ ] Guard: remove the W2 names; `tests/core/test_events.py` untouched.
- [ ] Gate: `pytest -q tests/tui tests/integration`, full pytest, guard, typecheck.

### C4 — Wave W3: v4 agent stack and private capability helpers

- [ ] Prerequisites green: M4 prompt projection, M5 compaction, M6 goal/entry cut, M7 team/subagent.
- [ ] Delete agent files listed in §3.3 W3 (14 modules), `hooks/records.py`, `prompt/projection.py`, `goal/{replay,round_driver}.py`, `llm/image_budget.py`.
- [ ] Compact disposition (D7): `compact/surface.py` and `compact/tokens.py` are deleted unconditionally (no native analog; estimators live in `agent/context_plan.py`). For `compact/{compactor,refs,compress_tool,block_tools}.py`: if M5 rewrote them in place as native modules, keep the paths and **remove them from `V4_MODULES` and from the deletion-footprint count**; if M5 created new modules and left the v4 files, delete them here. Record which choice was taken in the wave commit message.
- [ ] Edit hubs: `agent/__init__.py`, `goal/{__init__,service}.py`, `compact/__init__.py`, `prompt/__init__.py`, `builtin/{sessions,session_title,inspect}.py` v4 branches (files stay), `builtin/compress.py` (native calls only), bundle rows `goal-round-driver` (L52) if the plugin is removed, `compress` (L66) implementation updated.
- [ ] Delete/rewrite tests from §5.3/§5.4 for this subject (agent v4, goal replay/round driver, compact v4, prompt projection, image budget, hook records). Rebuild `tests/agent/fakes.py`, `tests/goal/conftest.py`, `tests/compact/support.py` wire-free.
- [ ] Guard: remove W3 names; run.
- [ ] Gate: `pytest -q tests/agent tests/goal tests/compact tests/prompt tests/hooks tests/native`, full pytest, differential.

### C5 — Wave W4: v4 session format stack

- [ ] Prerequisite: AST scan shows only tests (and each other) import the session modules.
- [ ] Delete `session/{session,store,writer,reader,events,derive,fork,layout,metadata,image_projection}.py`; rewrite `session/__init__.py` to the native API (or leave it exporting only native names).
- [ ] Edit call sites if any remain: `builtin/session_title.py`, `tui/app_sessions.py`, `tui/session_view.py` (should already be native in M8), `compact/blocks.py` (native refs in M5).
- [ ] Delete tests: `tests/session/{support,test_derive,test_derive_repair,test_events,test_fork,test_image_projection,test_invariants,test_metadata,test_reader,test_session_subscribe,test_store,test_store_info}.py`; keep native session suites; adjust `tests/session/test_native_schema.py::test_read_rejects_v4_json_and_zstd` (it now proves the native reader rejects a foreign file, no zstandard involved).
- [ ] Delete `tests/fixtures/synthetic/` and the root `fixtures/` workflow + `tools/gen_fixtures.mjs`; move any still-needed samples to `tests/fixtures/native/` first (§6.6).
- [ ] Guard: remove W4 names.
- [ ] Gate: `pytest -q tests/session tests/native`, full pytest, guard.

### C6 — Wave W5: wire boundary and core contracts

- [ ] Prerequisite: no src import of `core.wire`/`core.payloads`/`core.protocols` outside the three files themselves; `tools/contract.py` retype (M1) is complete.
- [ ] Delete `core/{wire,payloads,protocols}.py` (735 L).
- [ ] Rewrite `core/__init__.py` to export only native names (`core.llm`, `core.messages`, ids, errors). Remove v4 re-exports from `prompt/__init__.py`, `compact/__init__.py`, `agent/__init__.py`, `llm/__init__.py`, `session/__init__.py`.
- [ ] Delete `tests/core/{test_wire,test_payloads}.py` and `tests/integration/test_wire_boundary.py` (superseded by `tests/test_v4_free.py`); keep `tests/core/test_events.py`.
- [ ] `scripts/typecheck.py --write`; remove ruff per-file-ignores for deleted files; remove `zstandard>=0.23` from `pyproject.toml`; bump version to 0.3.0.
- [ ] Empty `V4_MODULES` in the guard (keep the frozen tuple in the test as documentation and assert the list of deleted paths does not exist on disk).
- [ ] Gate: guard, full pytest, ruff, typecheck, differential matrix.

### C7 — Wave W6: packaging, config, fixtures, docs

- [ ] Bundle rows: confirm all deleted-plugin rows gone; kernel boots with `tests/kernel/test_bundle*.py`; update `packaging/hiddenimports.py` docstring and `docs/spec/frozen-distribution.md`.
- [ ] Config: remove `sessionDir` mapping/field/helper/`__all__`; decide warning-vs-error for a config that still sets it; verify native `[retry]` wiring before removing its docs.
- [ ] Fixtures: root corpus + `tools/gen_fixtures.mjs` gone; `tests/fixtures/synthetic` gone; non-v4 tests reading root fixtures rewritten to `tests/fixtures/native/` (list in §6.6).
- [ ] Docs: execute §4.2 table; run the doc grep `Select-String -Recurse docs,README.md -Pattern "session\.v4|core\.wire|core\.protocols|core\.payloads|dpyro migrate|--legacy-v4|DEEPPYRO_NATIVE_LOOP|zstandard"` and justify every remaining hit (historical archives may keep hits only if marked archive).
- [ ] Release notes `docs/release/0.3.0.md` + upgrade guide; close KI-001/KI-002/KI-010; note D3 outcome.
- [ ] Final gate: guard, full pytest, ruff, format, typecheck, differential, `python -m build`/wheel import smoke, `dpyro --headless "hello"` smoke, TUI start smoke.

### C8 — Rules for the cleanup agent

- Do not delete or reformat `docs/design/{native-capabilities,tui-native-runtime,native-provider-port}.md` while executing waves; they are the design record. Mark phases done at the end.
- Do not delete `core/event_catalog.py` (D5), `llm/retry.py`, `llm/sse.py`, `llm/{catalog,discovery}.py`, native session modules, or any untracked native module.
- Do not touch `src/deeppyro/paths.py` env vars; only `config.py` `sessionDir` goes.
- One wave = one commit; the guard allowlist diff must match the wave exactly. If a wave needs an exception, add it explicitly to `V4_MODULES`/allowlist with a comment and a date, never silently.
- If a wave's full pytest is red, fix forward in the same wave branch (do not stack another deletion wave on a red tree).
- Re-measure test counts with `--collect-only` after W5 and reconcile the release notes.

## 7.2 The final "no v4 imports" AST guard

Place this file at `tests/test_v4_free.py`. It uses only the standard library so it keeps working after the stack is gone. During migration waves it runs in allowlist mode; at W5 the `V4_MODULES` tuple stays as the frozen historical list (documentation) while all assertions must pass on an empty allowlist.

```python
"""Guard: no v4 module, symbol re-export, or bundle row may survive in src/."""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "deeppyro"
BUNDLE = SRC / "bundles" / "core" / "bundle.toml"

# Frozen list of modules the 0.3.0 hard cut deletes: 58 module paths (the
# session/__init__.py package file stays and is rewritten, so it is not listed).
# Keep this tuple as the historical record even after the cut; the on-disk
# assertion at the bottom proves the files are gone.
V4_MODULES: tuple[str, ...] = (
    "deeppyro.core.wire",
    "deeppyro.core.protocols",
    "deeppyro.core.payloads",
    "deeppyro.session.session",
    "deeppyro.session.store",
    "deeppyro.session.writer",
    "deeppyro.session.reader",
    "deeppyro.session.events",
    "deeppyro.session.derive",
    "deeppyro.session.fork",
    "deeppyro.session.layout",
    "deeppyro.session.metadata",
    "deeppyro.session.image_projection",
    "deeppyro.migrate",
    "deeppyro.session.migrate_cli",
    "deeppyro.session.migrate_service",
    "deeppyro.session_query",
    "deeppyro.session_query.query",
    "deeppyro.session_query.filters",
    "deeppyro.session_query.types",
    "deeppyro.agent.session",
    "deeppyro.agent.session_state",
    "deeppyro.agent.session_turn",
    "deeppyro.agent.session_tools",
    "deeppyro.agent.session_records",
    "deeppyro.agent.session_support",
    "deeppyro.agent.session_stream",
    "deeppyro.agent.session_prompt",
    "deeppyro.agent.request",
    "deeppyro.agent.inbox",
    "deeppyro.agent.retry",
    "deeppyro.agent.stream",
    "deeppyro.agent.images",
    "deeppyro.agent.options",
    "deeppyro.prompt.projection",
    "deeppyro.projection",
    "deeppyro.projection.registry",
    "deeppyro.projection.checkpoint",
    "deeppyro.compact.compactor",
    "deeppyro.compact.surface",
    "deeppyro.compact.tokens",
    "deeppyro.compact.refs",
    "deeppyro.compact.compress_tool",
    "deeppyro.compact.block_tools",
    "deeppyro.goal.replay",
    "deeppyro.goal.round_driver",
    "deeppyro.llm.messages",
    "deeppyro.llm.messages_anthropic",
    "deeppyro.llm.anthropic",
    "deeppyro.llm.openai_compat",
    "deeppyro.llm.image_budget",
    "deeppyro.builtin.activity",
    "deeppyro.builtin.hook_journal",
    "deeppyro.builtin.llm_retry",
    "deeppyro.builtin.projections",
    "deeppyro.builtin.session_query",
    "deeppyro.hooks.records",
    "deeppyro.tui.history",
)

# Deleted before this plan landed; guarded so it cannot come back.
EXTRA_FROZEN = ("deeppyro.agent.native_provider",)
_FROZEN = frozenset(V4_MODULES) | frozenset(EXTRA_FROZEN)
_STRING_RE = re.compile(r"deeppyro\.(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*")


def _module_name(path: Path, root: Path = SRC) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return "deeppyro." + ".".join(parts)


def _is_package_file(path: Path) -> bool:
    return path.name == "__init__.py"


def _absolute(node: ast.ImportFrom, module: str, *, is_package: bool) -> str | None:
    """Resolve an ImportFrom to an absolute module name (None if it escapes)."""
    if node.level == 0:
        return node.module
    context = module if is_package else module.rsplit(".", 1)[0]
    parts = context.split(".")
    climb = node.level - 1
    if climb >= len(parts):
        return None
    parts = parts[: len(parts) - climb]
    if node.module:
        parts += node.module.split(".")
    return ".".join(parts)


def _package_exports(root: Path = SRC) -> dict[str, dict[str, str]]:
    """package -> {symbol: module that defines it} for re-export resolution."""
    table: dict[str, dict[str, str]] = {}
    for path in root.rglob("__init__.py"):
        if "__pycache__" in path.parts:
            continue
        pkg = _module_name(path, root)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exports: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            absolute = _absolute(node, pkg, is_package=True)
            if absolute is None or not absolute.startswith("deeppyro"):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                target = absolute if node.module else absolute + "." + alias.name
                exports[alias.asname or alias.name] = target
        table[pkg] = exports
    return table


def _targets(path: Path, tree: ast.AST, exports: dict[str, dict[str, str]], root: Path = SRC) -> set[str]:
    """Every module a file references: imports, submodule candidates, re-exports."""
    module = _module_name(path, root)
    is_package = _is_package_file(path)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute(node, module, is_package=is_package)
            if base is None:
                continue
            out.add(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                out.add(base + "." + alias.name)
                exported = exports.get(base, {}).get(alias.name)
                if exported:
                    out.add(exported)
    return out


def _is_frozen(target: str) -> bool:
    return any(target == name or target.startswith(name + ".") for name in _FROZEN)


def _string_refs(tree: ast.AST) -> set[str]:
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            refs.update(match.group(0) for match in _STRING_RE.finditer(node.value))
    return refs


def _scan(root: Path = SRC) -> list[str]:
    violations: list[str] = []
    exports = _package_exports(root)
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path, root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover
            violations.append(f"{module}: unparsable ({exc})")
            continue
        for target in _targets(path, tree, exports, root):
            if _is_frozen(target):
                violations.append(f"{module}: imports {target}")
        for ref in _string_refs(tree):
            if _is_frozen(ref):
                violations.append(f"{module}: string reference {ref}")
    if root == SRC:
        for name in _FROZEN:
            rel = name.removeprefix("deeppyro.").replace(".", "/")
            if (root / (rel + ".py")).exists() or (root / rel / "__init__.py").exists():
                violations.append(f"module still exists on disk: {name}")
    return sorted(set(violations))


def _bundle_rows() -> set[str]:
    if not BUNDLE.is_file():
        return set()
    data = tomllib.loads(BUNDLE.read_text(encoding="utf-8"))
    rows: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for inner in value.values():
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)
        elif isinstance(value, str) and ":" in value:
            rows.add(value.split(":", 1)[0].strip())

    walk(data)
    return rows


def test_no_v4_imports_in_src() -> None:
    violations = _scan()
    assert not violations, "\n".join(violations)


def test_no_deleted_module_in_bundle() -> None:
    assert BUNDLE.is_file(), f"bundle not found: {BUNDLE}"
    rows = _bundle_rows()
    leaks = sorted(rows & _FROZEN)
    assert not leaks, leaks


def test_scanner_self_test(tmp_path: Path) -> None:
    """The scanner catches direct, submodule, lazy, re-export and string forms."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from deeppyro.session import Session\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "a.py").write_text(
        "from deeppyro.core import wire\n"
        "import deeppyro.session.store\n"
        "def f():\n"
        "    from deeppyro.agent import session\n"
        "X = 'deeppyro.tui.history'\n",
        encoding="utf-8",
    )
    violations = _scan(tmp_path)
    joined = "\n".join(violations)
    assert "deeppyro.core.wire" in joined
    assert "deeppyro.session.store" in joined
    assert "deeppyro.tui.history" in joined
```

The guard is deliberately one-directional (it proves v4 is absent, not that every native module is perfect). It also serves as the review artifact: the diff of `V4_MODULES` per wave is the exact deletion set.

### 7.3 Commands that prove the final state

```powershell
# 1. no v4 imports / paths / bundle rows
.venv\Scripts\python.exe -m pytest -q tests/test_v4_free.py -p no:cacheprovider
# 2. deleted paths are gone (should print nothing)
git ls-files src/deeppyro | Select-String -Pattern "core/(wire|payloads|protocols)\.py|session/(store|writer|reader|events|derive|fork|layout|metadata|image_projection|session)\.py|session_query/|projection/|tui/history\.py|migrate.py|hooks/records\.py|compact/(surface|tokens|compactor|refs|compress_tool|block_tools)\.py"
# 3. no v4 dependency or flags left
Select-String -Path pyproject.toml -Pattern "zstandard"
Select-String -Path src/**/*.py -Pattern "DEEPPYRO_NATIVE_LOOP|--legacy-v4|native_loop_enabled"
# 4. full suite + static gates (merge gate)
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe scripts\typecheck.py
.venv\Scripts\python.exe -m pytest -q tests/native/test_differential.py tests/native/test_differential_synthetic.py
```

## Appendix A — Evidence, artifacts, reproducibility

**What was measured (and how).** A static AST graph was built over all 717 Python files (365 `src/deeppyro` modules, 352 `tests` test modules): imports (`ast.Import`/`ast.ImportFrom`, including function-body lazy imports), top-level definitions, `__all__`, line counts; module paths resolved for both absolute and relative imports; package `__init__` re-exports resolved to defining modules (`from deeppyro.session import Session` → `session/__init__.py` and `session/session.py`). Test-function counts are static counts of top-level `test_*` defs. The analysis artifacts lived outside the repo (`graph.json`, per-module inventory, reference and test-classification JSON, generated markdown tables); they are reproducible from the commands below and are not committed by design.

**Staleness.** Line counts/symbol lists in §1 were computed from the working tree at analysis time (46 modified + 21 untracked files, 1 deleted file). Importer/test counts come from the AST snapshot; the working tree modified files that matter (`builtin/inspect.py`, `builtin/session_title.py`, `core/protocols.py`, `core/wire.py`, `projection/registry.py`, `prompt/projection.py`, `llm/*`) changed docstrings/native helpers, not importer sets, but re-run the inventory in C0 before W1. The control-plane wave (bundle signing) may add bundle rows or digest tests — reconcile before deleting rows.

**A.1 Reproduce the inventory** (any Python 3.12+):

```python
# scripts are illustrative; the committed deletion plan does not ship analysis code
import ast, json, subprocess
from pathlib import Path
root = Path("D:/code/deeppyro")
files = sorted((root / "src" / "deeppyro").rglob("*.py"))
def module_name(p: Path) -> str:
    rel = p.relative_to(root / "src" / "deeppyro").with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__": parts.pop()
    return "deeppyro." + ".".join(parts)
out = {}
for p in files:
    tree = ast.parse(p.read_text(encoding="utf-8"))
    imports = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names: imports.append(("import", a.name, n.lineno))
        elif isinstance(n, ast.ImportFrom):
            imports.append(("from", n.module, n.lineno))
    out[module_name(p)] = {"lines": len(p.read_text(encoding="utf-8").splitlines()),
                           "imports": imports}
print(json.dumps(out, indent=1))
```

**A.2 Reproduce the file set and wave assignment** (§3.3 + §1.3): the 58 module paths in `V4_MODULES` minus the historically deleted `agent/native_provider.py` map one-to-one to the wave lists. `deeppyro.session` (package) and the dual-path files (`builtin/{sessions,session_title,inspect}.py`, `builtin/compress.py`, `builtin/llm.py`, `core/__init__.py`) are edited, not deleted.

**A.3 Decisions to confirm before execution**

| ID | Decision | Recommendation | Consequence if reversed |
|---|---|---|---|
| D1 | projection package + activity + projections plugin | delete with TUI switch (W2) | keep package means retyping 791 L to native events, keeping a 300 L plugin and tests/projection; adds ~1.3k lines to the retained surface |
| D2 | migrate (3 files, 1,611 L) | delete; migrate on 0.2.x (W1) | quarantine keeps wire/derive/events/layout/reader/writer + zstandard and an explicit wire exception; contradicts ADR 0001 and the empty allowlist |
| D3 | Anthropic transport | port native transport in an M-wave **or** ship a hard boot error + breaking-change note | "port" adds a native provider adapter and tests; "document" is a user-visible capability regression |
| D4 | zstandard dependency | remove (W5/W6) | keeping it only makes sense under D2-reversed quarantine |
| D5 | core/event_catalog | keep as frozen data; delete generation chain + catalog docs | deleting the table means moving 94 event specs into code/docs in the de-brand wave (larger, separate) |
| D6 | v4 flags/env | remove completely (M6) | keeping them implies a selectable code path that no longer exists |
| D7 | compact module names | decide in M5: native rewrite in place (keep paths) or new modules (delete v4 files) | affects the deletion footprint by ~1.5k L and the `V4_MODULES` set; either is suite-green if the guard is adjusted in the same wave |

## Appendix B — What the analysis did not verify

- It did not run the test suite (task constraint), so counts are static and the wave gates are design, not measurement. The first real gate is W0.
- It did not inspect test bodies except imports; a test module listed as REWRITE may still contain v4-only cases that should move to DELETE (noted where identified).
- It did not verify runtime behavior of the plugin bundle after row removal (kernel boot, signing digests) — that is the W0 control-plane prerequisite.
- It did not audit the 88-file local root `fixtures/` corpus file-by-file (untracked, personal DSH logs); only its readers and the generator were mapped.
- It did not resolve every v4 symbol exported by `deeppyro.compact`/`deeppyro.projection`/`deeppyro.goal`/`deeppyro.prompt`/`deeppyro.llm` hubs to a line; the hub counts in §6.2 are `__all__`/from-import counts, sufficient for the undercount argument but not a line-level trace.
