# Native compaction (schema 1)

Status: **additive path, opt in.** The v4 compactor, surface, refs, blocks and
compress tool keep working unchanged. This page documents the native modules that
compact a schema 1 `NativeSession` through its fold: automatic compaction writes
exactly one `compaction.done` fact, and the model-authored compress path writes one
fact per block. Nothing here is wired into `agent/loop.py` yet; the auto-compactor
is exposed as a callable plus a pure pre-send policy, and the TUI/headless seam is
`NativeCompactService` / `NativeCompactPlugin`.

Contract of record: [native-capabilities.md](../design/native-capabilities.md) section 2
(design) and [schema.md](schema.md) section 7 (fold). v4 behaviour reference:
[../../src/deeppyro/compact/compactor.py](../../src/deeppyro/compact/compactor.py).

## What changed relative to v4

| Concern | v4 | native (this wave) |
| --- | --- | --- |
| Surface | `surface.py` folds `LogRecord`s and assigns seq-based ordinals | the schema 1 fold is the surface: `view.messages` only, indexed 0..n-1 |
| Compaction log | `compaction/start`, `compaction/summary`, a `user/message` replacement and `compaction/end` bracket | exactly one `compaction.done` fact; no bracket, no partial states |
| Region selection | `select_region(messages, retain_tokens)` over surface nodes; tool-pair cuts via open-call counters | `select_region(messages, keep_recent_tokens)` over `view.messages`; the same cut-safety property |
| System prompt | a surface node that must be explicitly excluded | `view.system`, never in `view.messages`; uncompactionable by construction |
| Refs | `compact/refs.py`, keyed by seq, separate `.refs.jsonl` | `compact/native_refs.py`, keyed by message id, separate `.native.refs.jsonl` |
| Blocks | `.jsonl` sidecar, seq range | same sidecar, additive keys: `source`, `compactionId`, `messageRange` |
| Usage | written into the v4 checkpoint replacement | trace only (`ModelCallRecord`); the session log carries no token/latency/retry data |
| Summary payload | framed parts + surface ops + `sourceEventSeqs` | a plain `user` message framed by `prompts.frame_summary`, source `CompactionSource(compaction_id)` |
| Replay | needs the v4 surface replay plus the message replacement | fold alone; no sidecar is required to produce the visible transcript |

## Durable facts

* `compaction.done` is exactly `{compaction_id, covers, message}` (schema 1).
  `covers` is a non-empty list of message ids that are contiguous in the fold at
  append time; `message` is a `user` message whose source is
  `CompactionSource(compaction_id)` with the matching id. The schema validator
  rejects every other key.
* The summary content is three `TextPart`s from
  `prompts.frame_summary`: preamble + open tag, the model summary, close tag.
  `compact/prompts.py` is unchanged and shared with v4.
* The fold applies the fact atomically: it removes the covered messages and
  inserts the summary at the start position. A non-contiguous or already-covered
  range fails the fold instead of corrupting the view, so the compactor validates
  the region again right before appending.
* A summary message is pinned: the fold rejects a `compaction.done` whose
  `covers` contains the summary it inserts, and `covers` may not contain a message
  that a previous compaction already dropped. Region selection therefore stops
  after the last `CompactionSource` message and native refs never address a
  summary.
* The only writer is `NativeSession.append`; compaction passes the optional
  envelope through when the caller is inside a turn (`run_id`, `turn_id`,
  `step_id`, `trace_id`), and appends without one outside turns. The schema
  requires no envelope for this kind.
* Failure appends nothing: provider errors, aborts, length/content-filter stops,
  an empty summary and a summary that is not smaller than the shadowed content all
  raise before the single append. There is no partial compaction state to close.

## Region selection

`select_region(messages, keep_recent_tokens)` returns a `CompactionRegion` or
`None`:

1. **Pin the prefix**: candidates start after the last message whose source is
   `CompactionSource`. A summary can never be covered, which keeps ranges
   contiguous across repeated compactions.
2. **Keep the tail**: walk newest to oldest accumulating
   `context_plan.estimate_message_tokens` until the retained tail reaches
   `keep_recent_tokens`; that message index is the exclusive end of the region.
3. **Make the cut tool-safe**: extend the end left while the cut is unsafe, then
   advance the start right to the next safe cut. A cut is safe when
   `messages[i].role != "tool"` and every `ToolCallPart` seen before it has its
   tool result before it. `tool_safe_cuts` computes all n+1 cuts in one forward
   pass; unbalanced tool traffic fails closed (no safe cut).
4. **Never empty**: if the pinned prefix meets the retained tail there is nothing
   to compact and the result is `None`.

The request reuses `agent/turn_messages`/`agent/context_plan`: messages are
`(*view.messages[:start], instruction)` plus `view.system`, so the estimator used
for the trigger is the same one the loop uses for the next request.

## Automatic compaction

```python
region = CompactionRegion(start_index, end_index, message_ids, message_tokens)
done = CompactionDone(compaction_id, region, summary_message, event, response)

engine = NativeCompactor(provider, model, keep_recent_tokens=8000,
                         context_window=32768, threshold=0.85, max_tokens=None,
                         blocks=None, refs=None, trace_store=None)
engine.should_compact(view)                # policy only, no provider call
engine.select(view)                        # region preview
await engine.compact(session)              # -> CompactionDone | None
```

`compact` accepts either a `NativeSession` or a `NativeSessionHandle`-shaped
adapter (it unwraps `.native_session`), folds the log unless a view is passed, and
returns `None` when no region is selectable. The provider stop reasons keep the
v4 error strings:

| Stop | Error |
| --- | --- |
| provider stream error | `LlmError("summarization failed: ...")` |
| aborted | `AbortedError("summarization was aborted")` |
| length | `LlmError("summarization truncated at the token cap (incomplete checkpoint)")` |
| content filter | `LlmError("summarization was blocked by a content filter")` |
| tool calls | `LlmError("summarization attempted to call tools; expected a plain text summary")` |
| empty text | `LlmError("summarization produced no text summary content")` |
| not smaller than the region | `LlmError("summary is not smaller than the shadowed content (...)")` |
| session changed while summarizing | `SessionError("the session changed while summarizing; compaction aborted")` |

The size guard compares `estimate_message_tokens(framed summary)` with
`region.message_tokens`. Because `CHECKPOINT_PREAMBLE` and the tags dominate a
short summary, small regions cannot be compacted; that is the v4 guard kept
verbatim, not a regression.

When `trace_store` is set and the caller supplied all four envelope ids, the
compactor appends one `ModelCallRecord` (provider, model, prompt hash, input/
output tokens, latency, finish reason, retries, `context_message_ids`) to the
trace. Trace append failures are logged and suppressed; observability never breaks
compaction and the session log never carries usage.

The block sidecar entry is best effort (`source="auto"`, `compaction_id`,
message-id range, empty refs). A sidecar failure is logged and ignored: the
session fact is authoritative and fold/replay do not need the sidecar.

## Model-authored compress path

```python
compressor = NativeCompressor(blocks, refs)
outcome = compressor.compress(session, content, envelope=None)
# -> NativeCompressOutcome(ok, text, meta)
```

`parse_content` keeps the v4 user-facing grammar:
`m00150–m00220 optional topic` headers, one plain string with all ranges,
v4 error texts and codes (`COMPACT_INVALID_CONTENT`, `COMPACT_INVALID_RANGE`,
`COMPACT_EMPTY_SUMMARY`). Validation happens entirely before the first append, over
`view.messages` indices:

* `COMPACT_INVALID_RANGE` - reversed range or out of conversation order.
* `COMPACT_REF_NOT_FOUND` / `COMPACT_REF_NOT_ON_SURFACE` - a ref is out of range or
  points at a message a previous compaction dropped; the legacy text
  ("refs point at already-compressed messages") is preserved.
* `COMPACT_OVERLAP` / `COMPACT_ADJACENT_RANGES` - ranges must ascend without
  overlapping; adjacent ranges must be merged into one header.
* `COMPACT_SPLITS_TOOL_PAIR` - a block starts on a tool result whose call is
  outside it, or ends on an assistant message whose tool results are outside it.
  Same code and message shape as v4, expressed as cut safety over indices.
* `COMPACT_EMPTY_RANGE` - the range covers no visible messages.
* `COMPACT_SYSTEM_PROMPT` no longer exists: `view.system` is not a message, so the
  first block may start at index 0. This is a deliberate 0.3 change.

Commits are per block and oldest-first: `BlockStore.add` (additive `source`,
`compaction_id`, message ids) then exactly one `compaction.done` per block, all
under one `compaction_id` per invocation. A failure after some blocks leaves the
earlier facts committed, rolls back only the uncommitted refs/blocks and returns
`COMPACT_PARTIAL_WRITE` with the committed block ids; a failure before the first
append rolls both sidecars back and re-raises. Success text, fingerprints and
`MINIMUM_STATUS` / `UNCOVERED_STATUS` keep the v4 wording so TUI/CLI output is
unchanged.

## Refs sidecar

`native_refs.py` keeps the deterministic `m00005` vocabulary (`REF_WIDTH=5`,
`canonical_ref`, `ref_ordinal`) but persists
`{ref, message_id, assigned_at}` in
`<root>/<encoded session id>.native.refs.jsonl`, deliberately separate from the v4
`.refs.jsonl` until 0.3. `RefStore.plan(session_id, view)` is a pure plan: existing
entries keep their ref in file order, new visible messages receive the next
ordinals in view order, and `ensure` persists them. `addressable_messages` skips
`CompactionSource` summaries, so a compress call can never cover a pinned summary.
The refs file is a rebuildable cache: everything needed for replay is in the
session log, and refs are never rendered into prompt text.

## Blocks sidecar

`compact/blocks.py` stays wire-free and gains only additive fields:
`source` (`"compress"` default, `"auto"`), `compaction_id`, `messageRange`
(`start`/`end` message ids). v4-shaped records keep their exact JSON shape, so
`tests/compact/test_blocks.py` and the v4 readers are unchanged. One sidecar holds
both model-authored and automatic summaries for the session; block ids remain
`b1`, `b2`, ... and `search_context`/`decompress` (still the v4 tools reading the
same store) work for both.

## Service and plugin entry points

`NativeCompactService` is the plain object the TUI/headless layer can build once:

```python
service = NativeCompactService(provider, model, blocks_root=root,
                               keep_recent_tokens=8000, context_window=32768,
                               threshold=0.85, max_tokens=None,
                               trace_store=None)
service.should_compact(view)          # pre-send policy
await service.pre_send_compact(session)  # policy + auto compact, None if not needed
await service.native_compact(session)    # manual /compact
service.native_compress(session, text)   # compress tool path -> outcome
```

`should_compact_before_send(view_or_messages, *, context_window, threshold=0.85)`
is the pure policy function a later `TurnLayer` pre-send hook can call; it uses
`context_plan.message_tokens` and returns `False` for a non-positive window.
`NativeCompactPlugin` mounts as the `"compact"` service (`plugin_id =
"native-compact"`, inject `config` + `sessions`), resolves the sidecar root from
`compact.blocksDir` or the sessions store (`native-blocks/`), and `attach(provider,
model, trace_store=...)` returns the shared service. Config fields are snake_case:
`enabled`, `blocks_dir`, `keep_recent_tokens`, `context_window`, `threshold`,
`max_tokens`.

Nothing in `agent/loop.py` changes in this wave: the loop does not auto-compact,
`/compact` wiring lands with the TUI-native runtime work, and the pre-send policy
is documented rather than wired. The v4 `CompressPlugin`/`CompressTool` remain the
registered tool until native `tools/contract.py` exists (the native compressor is a
library, not a `BaseTool` subclass, precisely because that contract is not there
yet).

## Tests and gates

| File | Coverage |
| --- | --- |
| `tests/compact/test_native_compactor.py` | policy, region selection (tool-pair boundary, pinned summary, balanced tool traffic), fact shape and replay after restart, system prompt preservation, second compaction never covering the summary, failed request / aborted / oversized summary leaving no fact, changed-session guard, trace-only usage, service manual + pre-send APIs, AST scan proving the native modules import no v4/wire modules |
| `tests/compact/test_native_compress.py` | parser grammar and error texts, refs registration and replay, per-block facts, tool-pair guard, overlap/adjacency/empty guards, partial write and rollback, v4 text formatting |
| existing `tests/compact` | v4 compactor, surface, refs, blocks, block tools, compress tool - all untouched and green |

Gates: `.venv/Scripts/python.exe -m pytest tests/compact tests/native -q`;
`ruff check`/`ruff format` on `src` and `tests`; `scripts/typecheck.py`.

## Limits and follow-ups

* The native auto-compactor is not wired into `AgentLoop`; it is callable only.
  Wiring is the next native-runtime wave and must keep the one-fact contract.
* `compact/native_compress.py` is a library, not a native tool: the native tool
  contract (`src/deeppyro/tools/contract.py`) does not exist yet.
* Native `block_tools` (search/decompress) still live in the v4 module reading the
  same `BlockStore`; a native re-base is 0.3 work.
* In 0.3 the final shape rewrites `compact/compactor.py` in place, re-bases
  `refs.py`/`compress_tool.py`, deletes `surface.py`/`tokens.py`, and folds these
  `native_*` modules into the canonical ones; the v4 paths and tests are preserved
  until then.
