# Native replay coverage and re-recording

Status: **current coverage note for KI-011, KI-031 and KI-034** (native-only after
0.3.0 deleted the v4 session, compaction and wire layers). The recorded baseline
under `fixtures/native/` is real provider traffic, but it only exercises text and
tool turns. This page records exactly what it proved, which protocol surfaces are
covered by hand-authored native cases, and what re-recording costs. KI-011 and
KI-031 are resolved; KI-034 remains an accepted limit of the baseline.

## 1. What the recorded baseline covers

Provenance and the full case matrix live in [README.md](README.md#recorded-baseline-provenance):
10 cases, 17 HTTP requests, 7 tool calls, 298,506 bytes, 689 SSE payload lines,
captured 2026-11-17 from `deepseek-flash` with `temperature=0`, streaming, and
`thinking: {"type":"disabled"}` on every request.

| Protocol surface | Recorded? | Where |
| --- | --- | --- |
| streaming text deltas, `finish_reason=stop` | yes | 01-04 |
| multi-turn text (request replays assistant history) | yes | 04 |
| truncated output, `finish_reason=length` | yes | 05 (intentional tiny `max_tokens`) |
| forced single tool call + final answer | yes | 06 |
| parallel tool calls in one assistant message | yes | 07 |
| tool error result (`isError`) | yes | 08 |
| sequential tools across two steps (injected second prompt) | yes | 09 |
| policy-denied tool (simulated shell denial) | yes | 10 (no shell ran) |
| provider `usage` and response model in `llm.response.end` | yes | every case |
| image / attachment parts | no | hand-authored case (`image.json`) |
| approval prompts and outcomes | no | hand-authored case (`approval.json`) |
| compaction checkpoints | no | hand-authored case (`compaction.json`) |
| MCP tool names (`mcp__<server>__<tool>`) | no | hand-authored case, tool seam only (`mcp.json`) |
| PTC nested dispatch inside the outer tool result | no | hand-authored case (`ptc.json`) |
| subagent spawn and child ids | no | hand-authored case (`subagent.json`) |
| multi-agent handoff | no | hand-authored native `task.*` facts (`handoff.json`) |
| trace side channel (per-call tokens/latency/retries) | no (recordings store provider streams, not trace records) | the native loop derives tokens/latency from the stream and, since KI-010, records real retry counts from `RetryScheduled`; the baseline trace stays synthesized where usage is compared |

The recordings are data only: replay never re-executes a provider request and
never needs an API key. `scripts/record_replay.py` asserts each recorded request
body against a fake provider that emits the recorded SSE payloads, so the
baseline can be re-verified offline on every checkout.

## 2. Surfaces the recordings cannot reach

[`tests/fixtures/native/synthetic/`](../../tests/fixtures/native/synthetic/) holds
one hand-authored JSON case per capability. Each file contains:

- `native`: a schema 1 `session_id`, `created_at` and `Event` list (optionally
  `preset` and `origin`);
- optional `trace`: `RunRecord` + `ModelCallRecord`/`ToolCallRecord` lines;
- `coverage` (`covered` or `xfail`), `limitations` and, for xfail, a reason
  citing KI-011.

Unknown fields are rejected (a stale v4 side cannot silently ride along), and the
loader validates known capabilities, the schema 1 session id shape, and the
event kinds.

| Capability | Case | Status | What the case proves |
| --- | --- | --- | --- |
| image / attachment | `image.json` | covered | exactly one image part in the user message, `media_type` is `image/png`, the attachment survives `fold` |
| approval | `approval.json` | covered | one `approval.requested` + one `approval.decided` with matching ids, the decision references a recorded `tool.call`, no approval stays pending |
| compaction | `compaction.json` | covered | one `compaction.done` replaces a contiguous message span; the summary carries the compaction source id and stays in the fold, covered ids disappear |
| MCP | `mcp.json` | covered | `mcp__demo__lookup` name survives; schema 1 keeps no MCP lifecycle event; the trace carries model calls |
| PTC | `ptc.json` | covered | exactly one outer `ptc` call; the nested dispatch stays inside its result; no PTC lifecycle event |
| subagent | `subagent.json` | covered | the `subagent` tool result names the child id and the folded history carries it; no subagent lifecycle event |
| handoff | `handoff.json` | covered | native `task.state`, `task.handoff` and `task.handoff.result` are core kinds; run id, from/to, summary, open items, task id and budget are checked, and the fold projects one completed task plus one handoff |

The harness [`tests/native/synthetic.py`](../../tests/native/synthetic.py) runs
the native side through production parsers only: `NativeCodec.read` plus `fold`,
after materializing the fixture events into a temporary session directory. It
asserts semantic projections rather than bytes:

1. message sequence: role, id, content parts, source facts and `tool_call_id`;
2. tool call/result pairing: call id, name, canonicalized arguments, result text
   and `is_error`;
3. usage presence: the native session log must contain no token, latency or cost
   fields (schema §8.5 keeps usage in the trace); when the fixture carries a
   trace, `join_trace` must bind it to the session and the per-call counters,
   prompt hashes and tool names are checked;
4. one capability-specific invariant from the table above.

Handoff uses native schema 1 task facts: `task.state`, `task.handoff` and
`task.handoff.result`. Per-handoff token usage lives in the trace side channel
only (schema §8.5), so the native handoff payload must never sprout a usage field.

Run it directly:

```powershell
uv run pytest tests/native/test_differential_synthetic.py -q
uv run python scripts/differential_matrix.py --json native-differential-matrix.json
```

`scripts/differential_matrix.py` prints the covered/uncovered matrix, writes the
deterministic JSON report and exits non-zero when a capability has no case. The
`native-replay` CI job runs it plus `pytest tests/eval tests/native`, is blocking,
keeps `windows-latest` × Python 3.12/3.13, has a 20-minute timeout and uploads
both reports on success and failure.

### Adding a synthetic case

1. Create `tests/fixtures/native/synthetic/<capability>.json` in the shape above;
   the native side must describe the capability as a real conversation shape.
2. A `covered` case must provide `native`; the harness materializes it through the
   production codec, so a malformed event or a missing capability invariant fails
   the case instead of being faked.
3. If the capability is genuinely unrepresentable, mark it `xfail`, cite KI-011 in
   `xfail_reason`, and add the missing pieces to `limitations`. The test uses
   `pytest.mark.xfail(strict=True)`: the gap is visible, and the day a real
   representation lands the case XPASSes and forces the diff to be reviewed.
4. The matrix loader validates unique ids, known capabilities, schema-1 session
   ids and event kinds, and rejects unknown top-level fields, so a typo or a stale
   v4 side cannot silently drop a capability from the report.

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

The 26 MB v4 session corpus that used to live under `fixtures/sessions/` is gone and is **not**
part of this baseline; no replay or synthetic case depends on it (KI-030).
New protocol surfaces should be added as hand-authored synthetic cases under
`tests/fixtures/native/synthetic/` instead of re-recording, because the recorded
baseline is intentionally frozen: its value is that its bytes and checksums never
move.
