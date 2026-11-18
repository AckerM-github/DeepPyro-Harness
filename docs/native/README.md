# Native schema 1 — implementation, baseline and 0.3.0 status

Status: **native-only as of 0.3.0 (2026-11-19)**. The native message, session, fold, trace and `AgentLoop`
modules are the only runtime; TUI, headless and the loopback Web UI all use native schema 1. The v4 session stack,
v4 loop, `core.wire`, projections, `session_query`, `dpyro migrate` and the four 0.2.0 fallback flags were deleted.
The M0 recorded baseline and provenance remain valid and are still the reference for native behavior.

## Deliverables

| Path | What it is |
| --- | --- |
| [schema.md](schema.md) | Native session schema 1: header, envelope, core `EventKind`s, Message model, fold, trace, errors, fork/resume/checkpoint, 15 recorded refinements (D-1..D-15). |
| [../adr/0001-three-track-native-agent.md](../adr/0001-three-track-native-agent.md) | ADR: three-track break, zero compatibility, retained DSH design assets, milestones and hard gates; 0.3.0 status update at the top. |
| [../../fixtures/native/](../../fixtures/native/) | 10 real provider recordings, manifest with checksums, validation report, and the two small files used by the sequential-tool case. |
| [README.md](README.md) | This implementation, provenance, layout, validation and replay note. |
| [coverage.md](coverage.md) | What the recorded baseline proves, which surfaces must be synthesized (`tests/fixtures/native/synthetic/`), and how much re-recording costs. |
| [goal.md](goal.md) | Native goal rounds with no schema change: `goal.changed` round facts, `GoalService.admit_round`, `NativeRoundDriver`, budget/round-limit blocking, restart and remaining limits. |
| [compaction.md](compaction.md) | Native compaction: `compaction.done` records, refs sidecar and token accounting. |
| [../../src/deeppyro/core/messages.py](../../src/deeppyro/core/messages.py) | Frozen native `Message`, four content parts and eight sources; strict role/part matrix and JSON round-trip. |
| [../../src/deeppyro/session/schema.py](../../src/deeppyro/session/schema.py) | Schema 1 header/envelope, core `EventKind`s, `NativeCodec` reader/writer, writer lock and atomic append. |
| [../../src/deeppyro/session/fold.py](../../src/deeppyro/session/fold.py) | Pure fold to `SessionView`: replacement, compaction, state, approvals, tool pairing and missing-result repair. |
| [../../src/deeppyro/observability/trace.py](../../src/deeppyro/observability/trace.py) | Trace records, canonical hashing, strict reader and `join_trace` against session events. |
| [../../src/deeppyro/agent/loop.py](../../src/deeppyro/agent/loop.py) | Native `AgentLoop`: fold → `ContextPlan` → provider stream → assistant message, tools, budget/cancel, `run.*` events and trace. |
| [../../src/deeppyro/agent/context_plan.py](../../src/deeppyro/agent/context_plan.py) / [run_state.py](../../src/deeppyro/agent/run_state.py) / [checkpoint.py](../../src/deeppyro/agent/checkpoint.py) | Frozen request plan, run state and atomic checkpoint sidecar for kill/resume. |
| [../../tests/fixtures/native/golden-session.jsonl](../../tests/fixtures/native/golden-session.jsonl) | One complete tool turn used for byte-exact decode → fold → encode checks. |

0.3.0 has no second session format and no migration reference: the old `INTERFACES.md` / `session.md` contract
pages were deleted with the v4 stack. Old `session.v4.jsonl` files are rejected, not read.

## Recorded baseline provenance

- Captured 2026-11-17 by harness `m0-1`; real DeepSeek OpenAI-compatible chat-completion traffic, not synthetic replay data (ADR §4 rule 9).
- Provider `deepseek`; base URL `https://api.deepseek.com/v1`; requested model `deepseek-flash`; response model `deepseek-flash` on every response.
- `temperature=0`, `stream=true`, request timeout 120 s; `max_tokens` 8–384; all 17 request bodies set `thinking: {"type": "disabled"}`.
- 10 cases, 17 HTTP requests, 7 tool calls, 298,506 bytes of recordings, 689 SSE payload lines.
- All 10 cases `status=ok`; 18/18 required validation checks passed (21 checks total, 0 failed).

The capture harness was `D:/dshworkspace/m0/record_streams.py` (outside this repository). The repository stores the data baseline, not the recorder. Every request body needed to reproduce a stream is inside the recording, and `manifest.json` carries the sha256 and byte count of each file.

```text
fixtures/native/
  README.md
  manifest.json                 # case metadata, counts, sha256, bytes
  validation.json               # 18/18 required checks, 21 total, 0 failed
  data/hello.txt                # read by case 09
  data/notes.txt                # listed by case 09
  recordings/01_text_short.jsonl
  recordings/02_text_bullets.jsonl
  recordings/03_text_json.jsonl
  recordings/04_text_multiturn.jsonl
  recordings/05_text_truncated.jsonl
  recordings/06_tool_single.jsonl
  recordings/07_tool_parallel.jsonl
  recordings/08_tool_error.jsonl
  recordings/09_tool_sequential.jsonl
  recordings/10_tool_denied.jsonl
```

`manifest.json` is the machine-readable index. `config_path` is deliberately redacted to `%USERPROFILE%/.dpro/config.toml`; no API key, `Authorization` header or `sk-` string occurs in any recording (774 lines / 10 files scanned by the recorder). The repository marks `fixtures/native/**` as `-text`, so checkout cannot rewrite line endings and invalidate the pinned sha256 values.

## Case matrix

| # | Case | Shape | Requests | Tool calls | Finish reasons |
| --- | --- | --- | --- | --- | --- |
| 01 | `01_text_short` | exact short reply | 1 | 0 | stop |
| 02 | `02_text_bullets` | three-bullet explanation | 1 | 0 | stop |
| 03 | `03_text_json` | strict JSON-only output | 1 | 0 | stop |
| 04 | `04_text_multiturn` | second request replays the first assistant reply | 2 | 0 | stop |
| 05 | `05_text_truncated` | tiny budget, truncated output | 1 | 0 | length |
| 06 | `06_tool_single` | forced `add(17,25)` then final answer | 2 | 1 | tool_calls, stop |
| 07 | `07_tool_parallel` | two calls in one assistant response | 2 | 2 | tool_calls, stop |
| 08 | `08_tool_error` | missing-file error result | 2 | 1 | tool_calls, stop |
| 09 | `09_tool_sequential` | `list_dir` then `read_file` in separate steps | 3 | 2 | tool_calls, stop |
| 10 | `10_tool_denied` | policy-denied `shell` call | 2 | 1 | tool_calls, stop |

## What the recording format contains

UTF-8 JSONL, one object per line. A case starts with `case`, then interleaves `llm.request`, `llm.response.start`, `llm.sse`, `llm.response.end`, `tool.call` and `tool.result`, and ends with `case.end`.

- `llm.request.body` is the exact JSON body sent, including `stream: true` and `thinking: {"type": "disabled"}`.
- `llm.sse.raw` is the exact `data: {...}` payload, including the final `data: [DONE]`. SSE comments/keep-alives are not payloads and are not recorded.
- `llm.response.end` carries the assembled content, tool-call count, `finish_reason`, `usage` and response model.
- Tool arguments are recorded exactly as streamed, then the harness's local tool executed them and recorded the result. Case 10 never executed a shell command: its denial text is a policy simulation.

## Validation and replay checks

`validation.json` is the recorder's report over the same files:

- required: exactly 10 files, filename/case boundaries, JSON parse, request lifecycle, request bodies, non-empty messages, SSE payloads (689 lines), tool cases 06–10, case 09 order, cases 08/10 error results, no secrets, manifest checksums, case-end counts, all statuses `ok`, finish reasons present;
- optional but observed: case 07 two parallel calls, case 05 `finish_reason=length`, streaming usage present.

To re-check a checkout without the recorder, compare each `manifest.json` sha256 with the corresponding file and parse every JSONL line. The differential replay asserts (all 10 cases match at fixture level):

1. every recorded `llm.request` is replayed against a fake provider that emits the recorded `llm.sse` chunks;
2. the native loop produces the same assistant content, tool calls, finish reason and usage;
3. the native session log round-trips and folds back to the same visible messages;
4. no session log contains provider token/cost/latency snapshots (schema §8.5); those belong in the trace store.

## Native implementation

The modules are importable and covered by dedicated tests:

| Module | Contract | Tests |
| --- | --- | --- |
| `core/messages.py` | [schema §6](schema.md); 4 parts, 8 sources, no unknown fields | `tests/core/test_messages.py` |
| `session/schema.py` | [schema §2–§5](schema.md); `EventKind`, `SessionHeader`, `Event`, `NativeCodec`, `NativeSession` | `tests/session/test_native_schema.py` |
| `session/fold.py` | [schema §7](schema.md); single-pass, deterministic, no clock or I/O | `tests/session/test_fold.py` |
| `observability/trace.py` | [schema §8](schema.md); hashes only, no prompt/tool payloads | `tests/observability/test_trace.py` |
| golden fixture | decode → fold → encode is byte-identical | `tests/session/test_golden.py` |
| `agent/loop.py` | [schema §5.5](schema.md); `ContextPlan`, budget/cancel, native tool results, `run.started`/`run.finished` | `tests/native/` |
| `agent/checkpoint.py` | resumable run sidecar; repairs dangling steps/turns on resume | `tests/native/test_checkpoint.py` |

Run the native surface only:

```powershell
uv run --no-sync pytest tests/core/test_messages.py tests/session tests/observability tests/native -q
```

Behavioral notes:

- **v4 is rejected, not read.** A line-1 `"version": 4` header or a zstd magic prefix raises `SessionV4Error`. There is no fallback reader and no migration path.
- **Append is all-or-nothing.** The writer validates the event and its seq/turn/step order before writing; a failed write truncates back to the previous offset. A second writer fails on the `<id>.jsonl.lock` file.
- **The trace store is separate.** Default location is `~/.dpro/traces/<run_id>.jsonl`, overridable with `DEEPPYRO_TRACES_DIR`; records carry hashes, sizes, latency and IDs, never message text or tool arguments.
- **Trace smoke is real-session based.** `test_trace_join_smoke_with_native_session` writes a native turn with one tool call, writes one model-call and one tool-call trace record, then joins both by `run_id`/step/call ID.
- **One runtime for all entries.** TUI, headless and the loopback Web host all run the same native `AgentLoop`; there is no second loop and no loop-selection flag in 0.3.0.

## Known anomalies and limits

- Case 05 `finish_reason=length` is intentional (tiny `max_tokens`) and is the truncation baseline, not a failure.
- Cases 06 and 09 use forced `tool_choice`. `deepseek-flash` defaults to thinking mode, which rejects forced tool choice with HTTP 400 and spends small budgets on reasoning; every request therefore disables thinking. This is a capture decision, recorded in each case header and the manifest.
- Case 07's two calls were emitted voluntarily by the model in one response; the recorder did not retry to force them.
- Cases 08 and 10 intentionally produce error tool results. Case 10's shell denial is simulated by the harness; no shell command ran.
- Cases 08 and 09 contain the capture workspace path `D:/dshworkspace/m0/...` inside tool arguments/results. It is data from the real stream and is preserved byte-for-byte for replay; do not rewrite recordings.
- The baseline does not cover image/attachment parts, approval prompts, compaction checkpoints, MCP/PTC, subagents or multi-agent handoff. Hand-authored offline pairs under `tests/fixtures/native/synthetic/` cover those surfaces; matrix, synthesis rules and re-recording cost: [coverage.md](coverage.md).
- 0.3.0 boundaries (old sessions unreadable, Web UI local-only, provider transports, plugin API v3) are summarized in [docs/release/0.3.0.md](../release/0.3.0.md).

## M0 exit state

M0 was documentation and data only. M1 added `core/messages.py`, `session/schema.py`, `NativeCodec`, fold and the trace skeleton, and kept tool schemas byte-stable. M1–M5 are complete; their checklists are ADR §6.1–§6.5, and the frozen decisions are schema §13. 0.3.0 completed the migration: v4 deleted, native-only runtime, TUI and Web native.