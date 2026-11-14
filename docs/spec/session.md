# Session log (session.v4): storage, derivation, fork

Status: implemented by session-dev (task-1). Owns src/deeppyro/session/**, tests/session/**, this document. Contract source: docs/spec/INTERFACES.md §4; behavioural source: the real DSH session implementation shipped in D:\deepseekharness\resources\app.asar\dsh\node_modules\@deepseek-ai\dsh-session\lib\index.js (plus dsh-session-persistence-jsonl), reverse-engineered together with the 30 real logs in fixtures/sessions/.

## 1. Public API

    from deeppyro.session import (
        SessionStore, Session, SessionInfo, derive_messages,
        fork_session, SessionForkError,
    )

    class SessionStore:
        def __init__(self, root: pathlib.Path | None = None) -> None  # None -> config.session_dir()
        def create(self, cwd: pathlib.Path, *, agent_preset: str | None = None,
                   session_id: str | None = None) -> Session
        def open(self, session_id: str) -> Session
        def list(self) -> list[SessionInfo]
        def latest(self) -> SessionInfo | None

    class Session:  # satisfies core.protocols.SessionLogLike
        header: SessionHeader
        records: list[LogRecord]
        path: pathlib.Path
        session_id: str                      # property == header.id
        last_seq: int                        # -1 for an empty log
        def append(self, type: str, data: dict, **envelope: object) -> int
        def derive_messages(self, *, strict: bool | None = None) -> list[Message]
        def close(self) -> None
        def usage(self) -> dict              # {"input": int, "output": int, "cached": int}
        def turn_count(self) -> int
        def last_step(self) -> tuple[int, int]

    @dataclasses.dataclass(slots=True)
    class SessionInfo:
        id: str; path: pathlib.Path; cwd: str; createdAt: int
        title: str | None; turns: int; recordCount: int; agentPreset: str | None

Extra public helpers (not in INTERFACES but used by tests/other workstreams): fold_surface(records, *, strict=True), event_message(record), derive_messages(records, *, strict=True), read_session_file, read_header, write_new_session, append_events, encode_event_line, decompress_zstd_frames, the layout helpers (project_key, encode_segment, session_file_path, flat_session_path, discover_session_files), fork_session, build_fork_seed, open_turn_closers, CLOSER_TEXT, SessionRead, and the event-type constants.

## 2. On-disk format

### 2.1 Two accepted layouts

DSH (authoritative) nests each log:

    <root>/<projectKey(cwd)>/<encodeSegment(sessionId)>/session.v4.<jsonl|jsonl.zstd>

SessionStore.create deliberately writes the simpler flat layout

    <root>/<encodeSegment(sessionId)>.jsonl

so locally created sessions are easy to inspect; open/list accept the flat layout and both nested DSH shapes (<project>/session.v4.* and <project>/<session>/session.v4.*). layout.session_file_path produces the canonical nested path when a caller wants DSH parity.

Encoding rules (copied from DSH):

* encodeSegment(id): "." -> "~002E", ".." -> "~002E~002E"; ASCII [A-Za-z0-9._-] (except ~) passes through; every other UTF-16 code unit becomes ~XXXX (upper-case hex). session-<uuid> is therefore unchanged.
* projectKey(cwd): runs of /, \, : collapse to -; safe characters pass through; everything else is ~XXXX; the readable part is left-stripped of -, falls back to "root", is truncated to 251 chars, and is wrapped as --<readable>--. Example: C:\Users\example\.dsh\im -> --C-Users-example-.dsh-im--.
* Current log file name: session.v4.jsonl or session.v4.jsonl.zstd.

### 2.2 JSONL framing

* Line 0 is the header object (section 3). Every following line is exactly one event envelope: compact JSON (json.dumps(..., ensure_ascii=False, separators=(",", ":"))), UTF-8, terminated by \n. No pretty printing, no CRLF, no BOM.
* Session.append assigns seq = last_seq + 1 and time = now_ms() (or an explicit time= envelope override), serializes, writes one line, then flush() + os.fsync(). A failed write is rolled back with truncate(original_size); the in-memory record list is updated only after the durable append succeeded.
* New files are materialized through a temporary file, fsync, and os.replace so readers never observe a half-written header.

### 2.3 zstd framing

zstd files are not one big stream: each durable write is an independent checksummed frame (ZstdCompressor(write_checksum=True)). A new log has one header frame and (if there are prefix events, e.g. fork) one event frame; every append adds one frame. Reading loops ZstdDecompressor().decompressobj(), consuming unused_data until the buffer is exhausted; a truncated/torn trailing frame is ignored (its readable prefix is kept), so a crashed append cannot destroy earlier history.

### 2.4 Read-time tolerance

read_session_file returns SessionRead(header, records, truncated, trimmed):

* missing final newline -> the incomplete tail line is dropped and truncated becomes True;
* seq must be non-decreasing; equal/duplicate or decreasing seq is SessionError; a gap (trimmed fixture logs) is accepted, recorded as trimmed=True, and the reader continues from the observed seq;
* header id must equal the requested session id when opened through the store;
* malformed JSON, unknown non-ignorable event types, and every envelope/message validation failure raise SessionError.

## 3. Header

Physical keys, in serialization order (optional keys are omitted, never null): type, version, id, createdAt, [cwd], [parentSession], isSeeded, [origin], delegationDepth, [agentPreset].

* type == "session"; version == 4; id non-empty string; createdAt/delegationDepth non-negative integers; isSeeded boolean;
* cwd optional but, when present, a non-empty absolute path;
* origin may only be "subagent";
* unknown keys are rejected (the retired v3 fields sandboxMode, approvalPolicy, seedLength are therefore rejected too);
* there is no inheritedEventCount header field: for seeded sessions the inherited length is expressed by the session/end-seed {data:{"inherited":true}} event seq.

A header without cwd is normalized to cwd="" in the SessionHeader model so callers always have a string.

## 4. Event envelope

Required: type, seq, time, data. Optional: surfaceOp, sourceEventSeqs, ignorable. No other keys. seq/time are non-negative integers; data is an object; ignorable may only be true.

* Unknown type: accepted only with ignorable: true, and it may not carry surfaceOp/sourceEventSeqs. Such records are log-only.
* sourceEventSeqs is persisted as a list whose members are a seq or a [start, end] range (ranges are decoded to ints). Values must be unique, strictly increasing, all < seq; assistant/message must not carry it.
* The surface types are system/message, developer/message, user/message, assistant/message, tool/result. Each must carry surfaceOp; all other known types must not.
* surfaceOp is either the string "append" or an object with exactly {op: "replace", startSeq, endSeq}; both bounds are non-negative integers, startSeq <= endSeq, and both < seq.

KNOWN_EVENT_TYPES is exactly core.wire.RECORD_TYPES (62 names, matching the 62 DSH v4 types, including compaction/*, feedback/*, goal/change, hook/*, plan/mode, schedule/change, team/message/*, tool-workflow/*, subagent/model-selection-policy).

### 4.1 Message shape validation

For the five message-producing types, data must contain a valid message:

| type | message location | role | required source |
|---|---|---|---|
| system/message | data.message | system | kind == "system-prompt" |
| developer/message | data.message | developer | any non-empty kind |
| user/message | data itself | user | any non-empty kind |
| assistant/message | data.message | assistant | kind == "model", non-empty provider, model |
| tool/result | data.message | tool | kind == "tool", callId, and message.toolCallId == callId |

Common checks: non-empty string id, content is an array. developer/message and role == "developer" must occur together; non-developer messages may not contain tool-addition/tool-removal blocks. Developer tool additions need a non-empty toolName, must not inline a tool definition, and require data.headerSeq pointing at an earlier request/header; absent additions must not have headerSeq. tool/result with data.error requires message.isError == true.

request/header extra rules: data header must not contain system, and must omit empty tools/adapterDefaults. The record data is `{header, reason[, startsSeries]}` where startsSeries is only written when the record starts a request series. request/context record data is `{provider, model[, contextWindow][, systemPromptUpdate]}`; see `docs/spec/loop.md` §4.

### 4.2 Hook journal records

hook/invoked and hook/result are the only two known types written by the hook
journal (src/deeppyro/builtin/hook_journal.py through
AgentSession.record_hook_event). They are plain non-surface records: they carry
no surfaceOp/sourceEventSeqs, and derivation, the surface fold and compaction
never see them, so journaling cannot change what the model saw.

| type | data keys |
| --- | --- |
| hook/invoked | `{hook, mode[, scope{session[, preset]}][, turn, step, tool, callId, provider, model, phase, status]}` |
| hook/result | `{hook, mode, ok, outcome, durationMs[, valueType][, preview][, error{name, message}]}` plus the same optional context keys |

`outcome` is "value" | "none" | "error"; `preview` is a string (or compact
JSON) truncated to 120 chars; `valueType` is only present for non-string
values. Keys are written in a fixed order for golden tests. The two hottest
hooks (llm/stream, agent/assistant-stream) are never journaled, and dispatches
without an agent (approval/*, session/created|disposed) have no session to
write to. The record is appended before the live event is emitted, so a replayed
log and the NDJSON/TUI stream agree; SessionQuery.hook_trace pairs invoked with
the nearest following result for the same hook.

The names align with the DSH v4 vocabulary, but the record shape above is a
deeppyro contract (the reference implementation defines no writer).

## 5. Canonical surface fold

The durable log is not the model history. The model history is the surface: an ordered list of seqs produced by replaying every event through the fold.

    nodes = []
    for event in events (seq order):
        if unknown and ignorable:                 skip
        if type in PROJECTION_EVENTS:             skip        # image/offload: no projection interpreter yet
        if type not in SURFACE_EVENTS:            skip
        surfaceOp missing:                        strict -> error, tolerant -> skip
        surfaceOp == "append":                    nodes.append(seq)
        surfaceOp == replace(startSeq, endSeq):
            startIdx = index of startSeq in nodes (must exist)
            endIdx   = index of endSeq in nodes (must exist, >= startIdx)
            shadowed = nodes[startIdx:endIdx+1]
            validate sourceEventSeqs cover every shadowed seq
            if type == tool/result: rewrite may change only message.content
            if startIdx == 0 and nodes[0] is system/message:
                replacer must be system/message over exactly that one node
            nodes[startIdx:endIdx+1] = [seq]      # in place, not append

fold_surface(records, strict=True) is the exported implementation; plan_surface_transition validates one next event without mutating state so Session.append can validate first and commit only after the durable write. Error texts intentionally mirror DSH, e.g.:

* surface replace at seq X: startSeq and endSeq must reference earlier events
* sourceEventSeqs must reference earlier events: X >= current seq Y
* surface replace: sourceEventSeqs must include every shadowed surface node; missing X
* tool/result surface replacement may change only content
* surface replace: node 0 holds the system prompt and may be rewritten only by a system/message over exactly that node
* developer/message headerSeq must reference an earlier request/header

### 5.1 Strict vs tolerant mode

* strict (complete, contiguous logs): every rule above is enforced. Use fold_surface(..., strict=True) / derive_messages(..., strict=True).
* tolerant (trimmed fixture logs, i.e. a seq gap): a replace whose target node was cut out is applied as an append instead of failing, and a surface record without surfaceOp is skipped. Session.derive_messages() picks tolerant mode automatically when Session.trimmed is true; callers can force either mode with the strict= keyword.

## 6. derive_messages rules, type by type

Measured on all 30 fixtures: 8,138 events, 1,868 surface-eligible events, 9 replacements (all single-node), 1,859 final surface nodes, 1,851 derived messages.

For each surface node, in surface order, project one message:

| event type | derived message | rule / evidence |
|---|---|---|
| system/message | data.message | system prompt lives in history, not in request/header (23 fixture sessions have request/context.systemPromptUpdate == "in-history", 32 system/message records). Empty content -> skipped. |
| user/message | data itself | flat shape {id, role, content, source}, no turn/step wrapper. Real source.kind values: user (143), time-context (61), runtime-context (39), dsh-mnemon instructions (19) and memory-snapshot (37), skill-catalog (25), user-approval (12), tool-jobs (6), working-directory (1), agent-message (1). Empty content is still derived. |
| assistant/message | data.message | content parts seen: text 1,276, reasoning 713, tool-call 716. data.usage and data.stream are log metadata, not message content. Empty content -> skipped. |
| tool/result | data.message | role "tool", toolCallId, isError, source {kind:"tool", callId}; data.meta (cwd, url/statusCode, paths/lines, diffs, ...), data.error, turn/step stay in the log. Empty content is still derived. |
| developer/message | data.message | Mnemon memory-snapshot retirement: 8 records, all content: [], source {kind:"dsh-mnemon", form:"memory-snapshot-retired"}, surfaceOp replace over the retired user/message. Empty content -> skipped, so retirement is an empty placeholder that keeps node position/id stable while hiding the old memory snapshot from the model. |

Non-surface records never produce messages:

* agent/inbox/spliced — inbox state only: {target:"next-turn", start, inserted:[messages], removedCount?, outcome?}. The actual message becomes visible through the later user/message with the same message id (328 records).
* assistant/attempt — failed/streamed attempt metadata (data.stream chunks); not a delivered message (27 records).
* tool/call — pairs with tool/result by callId; never derived itself (716 records).
* turn/start|end, step/start|end — lifecycle only (145/144, 776/775).
* request/header, request/context — wire request metadata (39/28).
* llm/retry, llm/retry-started, session/title, session/title-llm-request, session/end-seed, permission/preset, sandbox/mode, approval/policy|asked|decided, model/selection, agent-preset/selected, todo/write, workspace/changes, deliverables/presented, job/registered|settled, command/run|done, subagent/descriptor|catalog, team/*, tool/ptc-dispatch(|-start), web/deepseek-search-llm-request, session-log-deepseek/delivery-accepted, working-directory/change — log-only.
* compaction/start|summary|end|prune, feedback/*, image/offload, hook/*, goal/change, plan/mode, schedule/change, tool-workflow/*, team/message/* — in the known-type set but absent from these fixtures; see §7.
* unknown types with ignorable: true — skipped.

The projection helper is event_message(record); it returns None for non-surface records and for empty-content system/developer/assistant messages.

## 7. Compaction / history rewriting

No fixture contains a compaction/* record, so compaction behavior was derived from the DSH source rather than observed data. The mechanism is the surface fold itself:

* a compaction checkpoint is a user/message whose surfaceOp is {"op":"replace","startSeq":..,"endSeq":..} spanning the summarized range, with sourceEventSeqs covering every shadowed node; compaction/start, compaction/summary, compaction/end are log-only bookkeeping around it; when a manual command triggers compaction, its id is carried as an optional `sourceCommandId` on all three bookkeeping records and on the checkpoint message source;
* compaction/prune rewrites history without a checkpoint;
* after folding, the model sees exactly the checkpoint/summary message in the old range's position, not the summarized messages.

This module keeps those records in Session.records and derives the folded surface; it does not synthesize summaries or implement image/offload projections (see deviations).

## 8. Store semantics

* create(cwd, agent_preset=None, session_id=None): generates session-<uuid4> when session_id is omitted; rejects empty ids, relative cwds, and existing logs; writes a header-only file and returns an empty Session ready for append.
* open(id): locates flat or nested files (uncompressed or zstd), reads and validates the whole log eagerly, and verifies header.id == id. Unknown id or invalid log -> SessionError.
* list(): discovers logs (flat + nested), skips unreadable/invalid files, de-duplicates by session id, and returns SessionInfo newest-first by (createdAt, id). latest() is the first entry or None.
* SessionInfo: title is the last non-empty session/title.data.title (fixtures show fallback titles then provider titles, so last wins); turns is the number of turn/start records; recordCount is len(records).
* Session.usage() sums assistant/message.data.usage over the session: input += inputTokens, output += outputTokens, cached += cacheReadTokens (missing keys count as 0; cacheWriteTokens/totalTokens are not reported). Fixture session-d745f174-c79f-4b69-a147-d6c57947e3f7: {"input": 92530, "output": 21643, "cached": 397184}.
* Session.turn_count() counts turn/start; last_step() scans backwards for the newest step/start/step/end and returns (turn, step), or (0, 0) when there is none.
* Session.append accepts time=, surfaceOp=, sourceEventSeqs=, and ignorable= envelope keywords. Surface records without surfaceOp default to "append"; explicit replace objects are validated against the current surface before writing (this is how developer retirement and compaction checkpoints are appended). After close() an append raises SessionError.

## 9. Fork

fork_session(store, src_id, *, at_seq=None) -> Session reproduces DSH SessionStore.fork + buildForkSeed:

1. Open the source; unknown id -> SessionForkError("SESSION_NOT_FOUND").
2. Boundary: default = last event seq; empty source = no events; explicit at_seq must be a non-negative, non-bool int, < len(events), and events[at_seq].seq == at_seq -> otherwise SessionForkError("INVALID_BOUNDARY"). A trimmed source is rejected with INVALID_BOUNDARY because a contiguous prefix is required.
3. Child header: fresh session-<uuid4>, fresh createdAt, inherited cwd/agentPreset/origin, parentSession = source id, delegationDepth = source + 1, isSeeded = true.
4. Seed = prefix events 0..boundary (original seq/time preserved) + session/end-seed {seq: boundary+1, time: <last prefix time>, data: {"inherited": true}} + openTurnClosers(prefix, {"kind":"forked"}).
5. open_turn_closers closes an open turn/step so the child never starts mid-turn: synthetic tool/result events for pending tool calls (in assistant tool-call order), then step/end if a step was open, then turn/end {data:{turn, reason:{kind:"forked"}}}. All synthetic events are contiguous, reuse the last event time, and are appended after the end-seed.
6. Synthetic results: message.id = "forked-tool-result-<callId>-<seq>", role "tool", isError: true, surfaceOp "append". A call that had a tool/call record is "started": {"name":"ToolOutcomeUnknownError","code":"TOOL_OUTCOME_UNKNOWN"} with sourceEventSeqs: [callSeq]; a call that never started: {"name":"ToolNotStartedError","code":"TOOL_NOT_STARTED"} with no sources. Text comes from CLOSER_TEXT["forked"]["started"|"notStarted"] (same wording as DSH: retry only if read-only/idempotent, otherwise verify external state; the parent may have completed the call after the fork point).
7. The child is written to the store's flat layout and returned open for appending; the source is never modified. Empty source: seed is empty, no end-seed marker, header still isSeeded: true.

Fork is independent of the "interrupted" closer kind; open_turn_closers also implements kind="interrupted" for a future live-interrupt path.

Seed events are re-serialized through the canonical writer, so a long sourceEventSeqs list may be re-compressed into [start,end] ranges; the decoded meaning and seq/time are identical. The only error codes exposed are SESSION_NOT_FOUND and INVALID_BOUNDARY (DSH also has SESSION_ALREADY_EXISTS, unreachable here because the child id is random).

## 10. Fixtures: what was reverse-engineered and covered

* fixtures/sessions/: 30 real logs (max 2.8 MB here; 4 are trimmed to first 70 + last 70 events), 8,138 events total, 39 of the 42 manifest type samples appear. 30/30 parse, pass envelope validation, and strict-derive (trimmed logs only because their cuts do not land inside a replace's shadowed range; they are still marked trimmed and get tolerant mode by default).
* Replacements observed: 9, all single-node — 8 developer/message over a retired user/message (sessions 04a44e0f seq 76,187,188,189; 16b700bc seq 175,326,327; d745f174 seq 146 -> targets 13/78/170/179 and 13/178/219 and 13) and 1 system/message over the system prompt (ddb1ba3d seq 50 -> seq 8; derive[0].id becomes the new id). In-place replacement is what keeps the system prompt at history position 0.
* Tool pairing: every tool/result carries sourceEventSeqs containing the matching tool/call seq, toolCallId == source.callId, and surfaceOp "append"; unmatched in-flight calls are visible as tool-call content blocks in an assistant/message. Non-trimmed sessions have results subset of calls. The one exception is trimmed d3917b55, whose first kept result references a call cut away by trimming.
* The first line of each fixture is a header; session-66de8981 is a real fork of session-c423d78a (isSeeded, parentSession, delegationDepth 1, origin "subagent") and confirms that a fork copies the prefix verbatim, keeps parent seq numbers, and continues from parent last seq + 1.
* Interrupted assistant messages (3): data.interrupted == true, only reasoning content; they are ordinary surface nodes and are derived.

## 11. Deterministic decisions / deviations from the letter of the contract

1. SessionStore.create writes the flat <root>/<encoded-id>.jsonl layout instead of the nested DSH path; open/list support both (and layout.session_file_path gives DSH-compatible placement).
2. derive_messages/fold accept a strict keyword (module functions default True; Session defaults to not trimmed). Complete logs are always strict; the tolerant path exists solely for externally trimmed fixtures.
3. open reads logs eagerly rather than lazily (INTERFACES allowed lazy loading); zstd files up to 12 MB decode quickly and validation needs all events anyway.
4. image/offload projections are not implemented: such records are kept in the log and skipped by the fold instead of being projected into a message. No fixture contains one; a real DSH log with image offload would need a projection interpreter before it can be replayed in the model history.
5. A seq gap is trimmed=True and tolerated instead of rejected outright, so the provided fixtures are readable; strict fold rules still apply to what remains.
6. SessionStore rejects empty session_id and relative cwd explicitly (DSH assumes valid input).
7. SessionInfo.turns counts turn/start (fixtures have one more start than end because some sessions end mid-turn); title is the last session/title, which matches the fixture history where fallback titles are later replaced by provider titles.

## 12. Tests

    uv run pytest tests/session -q           # 213 passed
    uv run python -c "import deeppyro.session"
    uv run ruff check src/deeppyro/session tests/session   # All checks passed

Coverage map:

* tests/session/test_events.py (45) — header/envelope/message/developer/request-header validation, source seq encoding, header serialization.
* tests/session/test_reader.py (81) — all 30 fixtures, all 40 by-type samples, seq and type histograms, torn tail, zstd frames (including a multi-MB real-log round-trip), discovery.
* tests/session/test_derive.py (46) — strict derive of every fixture, fixture-specific replacements, synthetic append/replace folds, every fold validation error, empty-content projection, tolerant mode.
* tests/session/test_store.py (17) — create/open/list/latest, append round-trip and durability contract, nested and zstd layouts, layout encodings, usage/turn_count/last_step.
* tests/session/test_fork.py (13) — prefix + end-seed + closers, boundary errors, pending started/not-started calls, trimmed rejection, real-fork fixture.
* tests/session/test_invariants.py (11) — cross-fixture surface metadata, shadow coverage, tool pairing, derived-message legality, manifest aggregates, stable fixture statistics, SessionLogLike conformance.
* tests/agent/test_hook_records.py (10) — record_hook_event writes both hook types with the observed seq/event order, rejects other types, and keeps derivation/surface unchanged.
* tests/session_query/test_hook_trace.py (13) — hook_trace pairing (open results, orphan results, limit validation, missing session).
