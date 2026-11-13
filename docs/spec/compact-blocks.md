# compress / block / ref store (P1)

Status: implemented in `src/deeppyro/compact/{blocks,refs,compress_tool,block_tools}.py`
plus the builtin plugin `src/deeppyro/builtin/compress.py`.  This document records the
format, the transaction protocol, the retrieval tools, and the compatibility boundaries
against the reference DSH host.  Public-API registration in `docs/spec/INTERFACES.md` is
owned by the Lead.

## 1. Why this exists

The model needs a way to replace long, already-consumed conversation ranges with its own
summaries so the context keeps shrinking without losing the task state.  DeepPyro stores
those summaries durably in two places:

- a **block store** next to the sessions (one file per session), holding every summary in
  full so later `search_context` / `decompress` calls can retrieve it exactly;
- the **session log** itself, as a `compaction/summary` event plus a checkpoint
  `user/message` that replaces the summarized range on the message surface.

The session log stays the single source of truth: replaying it (see
`docs/spec/session.md`) reproduces exactly the same messages the model saw, without
reading the block store.

## 2. Block store format (version 1)

```
<blocks-root>/<encode_segment(session_id)>.jsonl        # block records
<blocks-root>/<encode_segment(session_id)>.refs.jsonl   # frozen m -> seq mapping
```

`blocks-root` defaults to `<session store root>/blocks`.  One JSON object per line,
UTF-8, compact separators (`,`/`:`), trailing `\n`, appended with `flush` + `fsync`.
The filename segment uses the same UTF-16-safe escaping as session directories
(`deeppyro.session.layout.encode_segment`).

`BLOCK_STORE_VERSION = 1`.  A record:

```json
{"version":1,"id":"b1","sessionId":"session-...","range":{"start":"m00005","end":"m00072"},
 "startSeq":51,"endSeq":118,"title":"Windows crash diagnosis",
 "summary":"...full text...","createdAt":1794062030984}
```

- `id`: monotonically increasing per session file (`b1`, `b2`, ...).  Explicit ids are
  accepted but duplicates raise `COMPACT_BLOCK_CONFLICT`.
- `range` keeps the *refs* the model used; `startSeq`/`endSeq` are the resolved event
  seqs from the surface fold at compression time.
- `summary` is the raw block body (not framed, not tokenized); the checkpoint carries the
  framed version.
- `createdAt` is epoch milliseconds.

Reading tolerates damage: unparsable lines are skipped and counted
(`BlockStore.load` returns `(records, corrupt_count)`); unknown trailing bytes are
ignored.  Files are append-only; `offsets()`/`rollback()` exist solely for the
transaction rollback described in §5.  A process-local `RLock` serializes id allocation.

## 3. Ref sidecar format

```json
{"ref":"m00005","seq":51,"messageId":"msg_...","assignedAt":1794062030984}
```

Refs are canonical `m%05d` strings (`m5`, `M00005`, ` 5 ` normalize to `m00005`).
A ref is resolved once and then **frozen**: re-registering the same ref with the same seq
is a no-op, a different seq raises `COMPACT_REF_CONFLICT`.  Frozen refs never move even
when the surface changes, so a model that saw `m00012` can still address it later.

Unregistered refs are resolved against the *current derived message list*:
`m00001` is the first non-system derived message (`system` prompts are not
addressable; nodes that project to no message are skipped).  Registration happens only
after the whole call validated and only writes the pending entries.

## 4. Compress tool

```python
CompressTool(store, blocks: BlockStore, refs: RefStore)
name = "compress"; parallel_safe = False
parameters = {"content": string}   # the only argument
async def run(args, ctx) -> ToolResult
```

`content` is ONE string holding every range.  Each block starts with a header line

```
m00005–m00072 optional topic
```

(en dash, hyphen, or em dash all accepted), followed by that block's free-form summary.
Parsing rules: the first non-empty line must be a header; each header's body must be
non-empty after stripping; `start <= end`.  Rejections carry stable codes
(`COMPACT_INVALID_CONTENT`, `COMPACT_INVALID_RANGE`, `COMPACT_EMPTY_SUMMARY`,
`COMPACT_REF_NOT_FOUND`, `COMPACT_REF_NOT_ON_SURFACE`, `COMPACT_SYSTEM_PROMPT`,
`COMPACT_EMPTY_RANGE`, `COMPACT_OVERLAP`, `COMPACT_ADJACENT_RANGES`,
`COMPACT_SPLITS_TOOL_PAIR`, `COMPACT_SESSION_UNAVAILABLE`, ...).  A rejected call
writes **no** session event.

Validation (all before the first append):

1. ranges are listed in ascending, non-overlapping, non-adjacent order (adjacent ranges
   must be merged into one block so block boundaries stay meaningful);
2. resolved seqs are on the current surface (a frozen ref pointing at an already
   compressed range is `COMPACT_REF_NOT_ON_SURFACE`);
3. the range never starts at the system prompt (`COMPACT_SYSTEM_PROMPT`);
4. the range covers at least one visible message (`COMPACT_EMPTY_RANGE`);
5. a range that overlaps a tool call/result pair must contain the whole exchange —
   `COMPACT_SPLITS_TOOL_PAIR` otherwise, because a checkpoint that cuts the pair leaves
   a tool result without its call.

The session is taken from `ToolContext.services` in this order: a live
`session`/`current_session`/`agent_session` object whose `session_id` matches the
context (preferred — it sees appends immediately), then `services["sessions"]`: a
plugin/service exposing `.session`/`.current`/`.current_session`, or `.store`, or
any object with `open(session_id)`; finally the tool's own store is used to reopen the
session by `ctx.session_id`.  If nothing matches: `COMPACT_SESSION_UNAVAILABLE`.

## 5. Transaction protocol

For each block, in order:

```
compaction/start      {compactionId, source:"compress", turn}
compaction/summary    {compactionId, source, blockId, range, title,
                       summary:[{type:"text",text:body}],
                       shadowedRange:{start,end}, shadowedSeqs:[...],
                       shadowedTokenCount}
user/message          checkpoint message
                      surfaceOp: {"op":"replace","startSeq":..,"endSeq":..}
                      sourceEventSeqs: sorted([summarySeq, *shadowedSeqs])
compaction/end        {compactionId, source:"compress", blockIds:[...]}
```

The checkpoint message is a `user` message with
`source={"kind":"compact-checkpoint","compactionId","blockId","range"}` and content
`frame_summary(body)` (`<compacted-summary>` framing, see
`deeppyro.compact.prompts`).  It is a normal surface event: the session fold performs the
in-place replacement, so the checkpoint sits exactly where the first shadowed message
was and every gap message keeps its position.

Multi-block calls write **one checkpoint per block** (each replacing its own contiguous
range) plus **one `compaction/summary` per block**, all sharing a `compactionId`.  This
is required because a single `surfaceOp` can only replace one contiguous interval; gaps
between blocks stay visible by design.

Write ordering and failure boundaries:

1. block + ref sidecars are written first (offsets recorded);
2. `compaction/start` and the per-block summary/checkpoint events follow, then
   `compaction/end`; every append is durable (flush + fsync) before the next;
3. if the first session append fails, both sidecars are truncated back to their recorded
   offsets and the call fails — the session log is untouched, the error is
   `COMPACT_UNEXPECTED` (or the underlying `SessionError` wrapped into it);
4. if a later append fails, the tool cannot delete already-written events.  It appends a
   best-effort `compaction/end` carrying `error`, returns `is_error=True` with code
   `COMPACT_PARTIAL_WRITE` and `committedRecords`, and the checkpoints written so far
   remain valid (they are self-consistent surface replacements).  The caller may re-run
   compress for the remaining ranges.
5. Validation/parse failures always mean zero writes.

Result text mirrors the host format:

```
[Compressed m00005–m00072 → 1 block(s), ~51086 tokens saved.]
 · b1 full summary 4385ch [2026-11-11 22:36:59 – 2026-11-11 22:41:25] · head "..." … tail "..."
Fingerprints show head/tail only — every summary is stored in full. ...
<blank>
No compressible ranges remain — ... (or) Uncovered history remains — ...
```

Fingerprints use the first 30 / last 100 characters of the summary with newlines flattened
to spaces (no whitespace collapsing — same as the observed host text).  Timestamps are
the first/last shadowed event's `time` in local time (`%Y-%m-%d %H:%M:%S`).
`savedTokens = sum(max(0, round(shadowedTokens - checkpointTokens)))` with the
`compact.tokens` estimator (chars / 3.5).

## 6. Builtin plugin

```python
class CompressPlugin(Plugin):          # plugin_id="compress"
    inject = ["config", "sessions", "tools"]
    Config = CompressConfig            # blocksDir: str|None, compressEnabled: bool = True
```

`setup` resolves the block root (global `[compact]` / `[compress]` section in the
config file wins over the plugin row, then the default `<session store root>/blocks`),
builds `BlockStore`/`RefStore`, and registers **two** tool factories: one returning
`CompressTool`, one returning the `SearchContextTool` + `DecompressTool` pair over the
same block store.  `compressEnabled = false` skips registration entirely (retrieval has
nothing to read when compression is off).  Invalid types raise `ConfigError`.  The plugin does
not construct the session store: it expects the `sessions` service (the session itself
arrives through `ToolContext.services` at call time, see §4).

## 7. Compatibility boundaries vs. the reference host

Observed from 30 real session fixtures (48 sessions scanned, 8 direct `compress` calls):

- The host's tool description ends with a "billion-context ... issues" attribution; the
  DeepPyro description is identical in substance but drops that sentence.
- The host keeps blocks in its own sidecar store and **does not** persist a checkpoint in
  the session log: after a compress call the log only shows the assistant tool call and
  the tool result.  DeepPyro deliberately adds `compaction/start|summary|end` plus the
  checkpoint `user/message`, so a replay reconstructs the compressed context without the
  sidecar.  This is a known, intentional deviation.
- Host `m` numbering is not the same as ours: it counts additional non-message events
  (tool calls) and renumbers after each compression.  DeepPyro refs are frozen on first
  use against the derived messages (system prompt excluded) and never renumber.  Refs are
  stable for the lifetime of the session, which matters more than matching the host's
  transient numbering.
- Host range headers may point at messages whose content was later replaced; we reject
  ranges not on the current surface instead of silently projecting them.
- Host has extra protections not implemented in P1: last-user / recent-zone protection,
  `Withdrawn N message(s)` and `Excluded ... protected message(s)` lines, and the
  "compression target" analysis behind `No compressible ranges remain`.  P1 derives the
  final status line from a simple check: are any non-system, non-checkpoint messages still
  visible?
- `search_context` and `decompress` consume this same block store (see §8).  No host
  fixture contains a real compressed checkpoint, so the session-event format above is
  inferred from the documented transaction and is locked by our own tests, not by
  fixtures.
- Block/summary text is arbitrary model-authored text; we do not validate it beyond
  non-empty.  JSON framing (`{"summary": ...}`) is **not** implemented: the body is
  treated as plain text (the host accepts JSON-string bodies too, but we keep the plain
  form; documenting it avoids silent truncation).

## 8. Retrieval tools: search_context / decompress

Compression is only useful if the model can get a detail back out of a summary later.
Two read-only tools (`src/deeppyro/compact/block_tools.py`) expose the per-session block
file; neither touches the session log, and neither can recover the original messages —
the block store only ever held the summaries.

Both tools are `parallel_safe = True` (they only read) and both validate arguments with
`deeppyro.tools.args`, raising `ToolError` for a malformed call.

### 8.1 Session resolution

The block file is addressed by session id, so neither tool needs to open the (possibly
huge) session log:

1. a live session injected by the runtime wins: `services["session"]`,
   `services["current_session"]`, `services["agent_session"]`, or a sessions service
   exposing `.session` / `.current` / `.current_session`; a candidate whose id
   contradicts `ctx.session_id` is ignored;
2. otherwise `ctx.session_id` is used directly;
3. otherwise the call fails with `ToolError("no session is available: ...")`.

### 8.2 search_context

```json
{"type":"object",
 "properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":20}},
 "required":["query"],"additionalProperties":false}
```

- the query is stripped; blank/missing/non-string queries are `ToolError`s;
- matching is a **case-insensitive substring** search (Python `casefold`) over, in
  priority order: the flattened summary (whitespace runs collapsed to single spaces), the
  ref range rendered as `m00001–m00005`, the block title, and the local creation stamp
  (`%Y-%m-%d %H:%M:%S`).  Matching a ref/title/stamp still returns the summary excerpt;
- hits are ordered by `startSeq` descending (ties by numeric block id), i.e. the most
  recent compressed range first;
- `limit` defaults to 5 and is capped at 20; the header says how many hits exist when the
  limit cut the list: `Found 7 compressed block(s) matching "x" (showing 5 most recent).`;
- each hit is formatted as three lines:

  ```
  m00001–m00005 · b1 · <first 120 characters of the summary>…
     head "<up to 200 chars around the hit>" … tail "<up to 200 chars from the hit>"
     <title · ><n> chars · 2026-11-11 22:36:59
  ```

  A ref/title/stamp hit says `matched range` / `matched title` / `matched created`
  instead of `head`, and shows the first/last 200 characters as the excerpt;
- the whole result text is capped at **8 KiB** (`MAX_TEXT_BYTES = 8192`, UTF-8 bytes).
  Truncation drops whole hit groups from the end (never half an entry) and appends
  `…[truncated to 8KB — narrow the query or lower limit]`; `meta.truncated` is true and
  `meta.count` / `meta.blocks` reflect only the hits that were returned;
- no hits (including a missing or empty block file) is a normal, non-error result:
  `No compressed blocks matched "<query>".`;
- `meta = {query, blocks:[ids], count, total, truncated}`.

### 8.3 decompress

```json
{"type":"object","properties":{"block_id":{"type":"string"}},
 "required":["block_id"],"additionalProperties":false}
```

- looks the id up in the current session's block file only; an unknown id (or the id of
  another session's block) raises `ToolError("block not found: <id>")`;
- returns the **entire** summary without truncation, preceded by

  ```
  b3 · m00150–m00220 · full summary

  <summary text>
  ```

- `meta = {block, range:{start,end}, startSeq, endSeq, createdAt}`.

Limits (intentional): no cross-session search, no regex/boolean/BM25 scoring, no paging
beyond `limit`, no original-message recovery, and no index — every search scans the
session's block file (it holds one line per compression, so this stays small).

## 9. Tests

`tests/compact/`:

- `test_blocks.py` — id allocation/persistence, duplicate ids, corrupt-line tolerance,
  offsets/rollback, concurrent appends across threads, path escaping.
- `test_refs.py` — canonicalization, addressable-message projection, resolve-without-
  writing vs. `ensure_refs`, frozen refs surviving surface changes, conflicts, bad lines,
  rollback, `RefEntry` round-trip.
- `test_compress_tool.py` — parsing and rejection codes, tool schema, single-block
  transaction shape, `sourceEventSeqs` coverage, surface fold strictness, multi-block
  gaps/positions, geometry guards, tool-pair splits, system-prompt guard, frozen-range
  re-compression, all-compressed status, session resolution (services/plugin/store),
  write-failure rollback, plugin config handling.
- `test_block_tools.py` — retrieval schemas, query/limit validation, no-session error,
  empty/missing store, case-insensitive summary matching, ref/title/time matching, recency
  ordering, limit cut, bounded excerpts, 8 KiB text cap (whole-hit truncation), corrupt-line
  tolerance, session isolation, session resolution (live session / sessions service /
  context id), full-summary decompress, unknown block error, plugin registration.

Run: `uv run pytest -o addopts= -q tests/compact`.
