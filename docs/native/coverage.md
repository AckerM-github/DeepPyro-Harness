# Native replay coverage and re-recording

Status: **0.2.0 coverage note for KI-011, KI-031 and KI-034** (2026-11-18). KI-011
and KI-031 are resolved; KI-034 remains an accepted limit of the baseline. The
recorded baseline under [`fixtures/native/`](../../fixtures/native/) is real
provider traffic, but it only exercises text and tool turns. This page records
exactly what it proves, which protocol surfaces must be synthesized for the
differential, and what re-recording costs.

## 1. What the recorded baseline covers

Provenance and the full case matrix live in [README.md](README.md#recorded-baseline-provenance):
10 cases, 17 HTTP requests, 7 tool calls, 298,506 bytes, 689 SSE payload lines,
captured 2026-11-17 from `deepseek-flash` with `temperature=0`, streaming, and
`thinking: {"type":"disabled"}` on every request.

| Protocol surface | Recorded? | Where |
| --- | --- | --- |
| streaming text deltas, `finish_reason=stop` | yes | 01–04 |
| multi-turn text (request replays assistant history) | yes | 04 |
| truncated output, `finish_reason=length` | yes | 05 (intentional tiny `max_tokens`) |
| forced single tool call + final answer | yes | 06 |
| parallel tool calls in one assistant message | yes | 07 |
| tool error result (`isError`) | yes | 08 |
| sequential tools across two steps (injected second prompt) | yes | 09 |
| policy-denied tool (simulated shell denial) | yes | 10 (no shell ran) |
| provider `usage` and response model in `llm.response.end` | yes | every case |
| image / attachment parts | no | needs synthetic |
| approval prompts and outcomes | no | needs synthetic |
| compaction checkpoints | no | needs synthetic |
| MCP lifecycle and `mcp__<server>__<tool>` names | no | needs synthetic (tool seam only) |
| PTC nested dispatch (`tool/ptc-dispatch*`) | no | needs synthetic |
| subagent catalog / descriptor / child ids | no | needs synthetic |
| multi-agent handoff | no | native `task.*` facts (synthesized, see §2) |
| trace side channel (per-call tokens/latency/retries) | no | synthesized where usage is compared |

The recordings are data only: replay never re-executes a provider request and
never needs an API key. `scripts/record_replay.py` asserts each recorded request
body against a fake provider that emits the recorded SSE payloads, so the
baseline can be re-verified offline on every checkout.

## 2. Surfaces that must be synthesized

[`tests/fixtures/native/synthetic/`](../../tests/fixtures/native/synthetic/) holds
one hand-authored JSON pair per capability. Each file contains:

- `v4`: a session v4 header plus `LogRecord` lines (hand-written, offline);
- `native`: a schema 1 `session_id`, `created_at` and `Event` list;
- optional `trace`: `RunRecord` + `ModelCallRecord`/`ToolCallRecord` lines;
- `coverage` (`covered` or `xfail`), `limitations` and, for xfail, a reason
  citing KI-011.

| Capability | Case | Status | What is compared beyond the generic checks |
| --- | --- | --- | --- |
| image / attachment | `image.json` | covered | exactly one image part per side, same attachment, native `media_type` |
| approval | `approval.json` | covered | `approval/asked`+`decided` vs `approval.requested`+`decided`; id, tool, call id, reason, outcome mapping (`allowed-once`→`allowed`) |
| compaction | `compaction.json` | covered | v4 surface replace range vs native `covers`; summary text after stripping the v4 preamble/tags |
| MCP | `mcp.json` | covered | `mcp__demo__lookup` name and result survive both sides |
| PTC | `ptc.json` | covered | nested `tool/ptc-dispatch*` stay non-surface; only the outer call/result is visible |
| subagent | `subagent.json` | covered | child id from `subagent/catalog` appears in the tool result; no native lifecycle event exists |
| handoff | `handoff.json` | covered | v4 `task/state`, `task/handoff`, `task/handoff/result` (ignorable extension records) vs native `task.state`, `task.handoff`, `task.handoff.result`; run id, from/to, summary, open items, task id, prompt/turn/call id/budget, last-wins task status and result `ok`/text/error. Usage boundary: v4 embeds the parent snapshot in `handoff.state.usage` and the child usage in the result record, while native keeps both in the trace only |

The harness [`tests/native/synthetic.py`](../../tests/native/synthetic.py) runs
both sides through production parsers only: `read_session_file` plus
`derive_messages` for v4, `NativeCodec.read` plus `fold` for native. It compares
semantic projections rather than bytes:

1. message sequence: role, id, content parts, source facts and `tool_call_id`;
2. tool call/result pairing: call id, name, canonicalized arguments, result text
   and `is_error`;
3. usage presence: v4 assistant `usage` must match the synthetic trace's
   `model_call` records, and the native session log must contain no token,
   latency or cost fields (schema §8.5 keeps usage in the trace);
4. one capability-specific invariant from the table above.

Handoff is the one surface whose two representations have different shapes: v4
task tools write `task/state`, `task/handoff` and `task/handoff/result` as
*ignorable extension records* (they are not in `RECORD_TYPES`, so a v4 reader
skips them unless `ignorable` is true), while native schema 1 added first-class
`task.state`, `task.handoff` and `task.handoff.result` events. The case compares
the same facts on both sides and asserts the documented boundary: v4 keeps the
parent and child token usage in the session records, native keeps usage in the
trace only (schema §8.5), so the native facts must never sprout a usage field.

Run it directly:

```powershell
uv run pytest tests/native/test_differential_synthetic.py -q
uv run python scripts/differential_matrix.py --json native-differential-matrix.json
```

`scripts/differential_matrix.py` prints the covered/uncovered matrix, writes the
deterministic JSON report and exits non-zero when a capability has no case. The
`native-replay` CI job runs it plus `pytest tests/eval tests/native`, is blocking
(the old `continue-on-error` is gone), keeps `windows-latest` × Python
3.12/3.13, has a 20-minute timeout and uploads both reports on success and failure.

### Adding a synthetic case

1. Create `tests/fixtures/native/synthetic/<capability>.json` in the shape above;
   both sides must describe the *same* conversation, not two unrelated examples.
2. A `covered` case must provide both `v4` and `native`; the harness will reject
   a case whose vocabularies cannot express the capability, so do not fake one.
3. If the capability is genuinely unrepresentable, mark it `xfail`, cite KI-011 in
   `xfail_reason`, and add the missing pieces to `limitations`. The test uses
   `pytest.mark.xfail(strict=True)`: the gap is visible, and the day a real
   representation lands the case XPASSes and forces the diff to be reviewed.
4. The matrix loader validates unique ids, known capabilities and schema-1 session
   ids, so a typo cannot silently drop a capability from the report.

## 3. Re-recording the real baseline

`scripts/record_replay.py` is a **replay and eval driver, not a recorder**. It
never imports a provider client and never opens a network connection:

```text
python scripts/record_replay.py [all|replay|eval]
    --native-root fixtures/native          # recordings + manifest.json
    --golden tests/fixtures/eval/golden.jsonl
    --json native-replay-report.json
```

It prints `native replay: N/M cases passed` and `golden eval: N/M cases passed`,
writes a deterministic JSON report and exits 1 on any failure. Replay cost is
zero API tokens and a few seconds; it only needs the checked-in fixtures and does
not need node.

Re-recording the 10 real cases is a separate, manual operation:

1. Run the external capture harness (historically `D:/dshworkspace/m0/record_streams.py`,
   not stored in this repository) against `https://api.deepseek.com/v1` with a
   real API key. That is **17 real chat-completion requests** producing ~298 KB
   of JSONL, not counting retries; treat the token cost as a normal eval budget.
2. Keep the same capture rules: provider `deepseek`, model `deepseek-flash`,
   streaming, `temperature=0`, `thinking: disabled` (thinking mode rejects forced
   `tool_choice`), exact `llm.request.body` and `llm.sse.raw`, exact tool
   arguments/results, and the two small data files used by case 09.
3. Replace the files under `fixtures/native/recordings/` (and any `data/` files),
   then regenerate `manifest.json` (per-case sha256, bytes, counts and totals) and
   `validation.json`, and update the provenance/case-matrix sections of
   [README.md](README.md).
4. Never commit an API key, `Authorization` header or `sk-` string; the existing
   recordings were scanned for that. Do not hand-edit recordings: sha256 and byte
   counts are pinned and `fixtures/native/**` is marked `-text` so checkout
   cannot rewrite line endings.
5. Re-verify with `uv run python scripts/record_replay.py --json native-replay-report.json`
   and `uv run pytest tests/eval tests/native -q`.

The 26 MB session corpus under `fixtures/sessions/` is untracked and is **not**
part of this baseline; no replay or differential case depends on it (KI-030).
New protocol surfaces should be added as synthetic pairs under
`tests/fixtures/native/synthetic/` instead of re-recording, because the recorded
baseline is intentionally frozen: its value is that its bytes and checksums never
move.
