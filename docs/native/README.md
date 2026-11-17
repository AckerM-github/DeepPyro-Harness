# Native schema 1 — M0 baseline and fixtures

Status: M0 complete for the native contract; implementation starts in M1. This directory records the M0 deliverables and the replay baseline that M1 must satisfy. Product code was not changed in M0 (ADR D8).

## Deliverables

| Path | What it is |
| --- | --- |
| [schema.md](schema.md) | Native session schema 1: header, envelope, 24 core `EventKind`s, Message model, fold, trace, errors, fork/resume/checkpoint, 15 recorded refinements (D-1..D-15). 449 lines. |
| [../adr/0001-three-track-native-agent.md](../adr/0001-three-track-native-agent.md) | ADR: three-track break, zero compatibility, retained DSH design assets, milestones and hard gates, locked M0 decisions. |
| [../../fixtures/native/](../../fixtures/native/) | 10 real provider recordings, manifest with checksums, validation report, and the two small files used by the sequential-tool case. |
| [README.md](README.md) | This provenance, layout, validation and replay note. |

The old contracts are retained only as migration references: [../spec/INTERFACES.md](../spec/INTERFACES.md) and [../spec/session.md](../spec/session.md) now carry frozen/superseded banners.

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

To re-check a checkout without the recorder, compare each `manifest.json` sha256 with the corresponding file and parse every JSONL line. M1 differential replay should assert:

1. every recorded `llm.request` can be replayed against a fake provider that emits the recorded `llm.sse` chunks;
2. the native loop produces the same assistant content, tool calls, finish reason and usage;
3. the native session log round-trips and folds back to the same visible messages;
4. no session log contains provider token/cost/latency snapshots (schema §8.5); those belong in the trace store.

## Known anomalies and limits

- Case 05 `finish_reason=length` is intentional (tiny `max_tokens`) and is the truncation baseline, not a failure.
- Cases 06 and 09 use forced `tool_choice`. `deepseek-flash` defaults to thinking mode, which rejects forced tool choice with HTTP 400 and spends small budgets on reasoning; every request therefore disables thinking. This is a capture decision, recorded in each case header and the manifest.
- Case 07's two calls were emitted voluntarily by the model in one response; the recorder did not retry to force them.
- Cases 08 and 10 intentionally produce error tool results. Case 10's shell denial is simulated by the harness; no shell command ran.
- Cases 08 and 09 contain the capture workspace path `D:/dshworkspace/m0/...` inside tool arguments/results. It is data from the real stream and is preserved byte-for-byte for replay; do not rewrite recordings.
- The baseline does not cover image/attachment parts, approval prompts, compaction checkpoints, MCP/PTC, subagents or multi-agent handoff. Those are M2+ gates (schema §5.2 reserved events, §8 trace).

## M0 exit state

M0 is documentation and data only. M1 starts with `core/messages.py`, `session/schema.py`, `NativeCodec`, fold and the trace skeleton; it must reject v4 explicitly, keep tool schemas byte-stable, and pass the four existing gates. See ADR §5–§7 and schema §13.
