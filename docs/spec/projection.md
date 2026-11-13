# Session projections and checkpoints (`deeppyro.projection`)

Status: implemented (P1 B1/B2, owner: loop-dev). Consumes only the public session
surface (`deeppyro.session.Session` header/records/path/last_seq) and `core.wire` record
types; it never writes the session log. The advance signal is explicit: callers invoke
`registry.notify(session, record)` after each durable append. Checkpoints are stored in an
adjacent JSON sidecar so opening a long session does not require re-folding history.

Modules:

| module | contents |
| --- | --- |
| `src/deeppyro/projection/registry.py` | `ProjectionRegistry`, `ProjectionDefinition`, `ProjectionHandle`, `ProjectionError`, `inherited_event_count` |
| `src/deeppyro/projection/checkpoint.py` | sidecar encode/decode/read/write, `save_checkpoint`, `load_checkpoint`, `ProjectionCheckpoints` |
| `tests/projection/` | registry, checkpoint, activity-parity and fork-cut tests |

Related docs: `docs/spec/activity.md` (ActivityService, the comparison oracle),
`docs/spec/session.md` (log/surface/trimmed semantics), `docs/plan/p1-context.md` section 3
(design source; this document records the implemented contract).

## 1. Public API

```python
from deeppyro.projection import (
    ProjectionDefinition,
    ProjectionError,
    ProjectionHandle,
    ProjectionRegistry,
    ProjectionCheckpoints,
    inherited_event_count,
    sidecar_path, read_checkpoint, write_checkpoint,
    save_checkpoint, load_checkpoint,
)

definition = ProjectionDefinition(
    key="activity/todos",          # stable, namespaced unit id
    state_version=1,               # bump when apply/state layout changes
    init=lambda header, inherited_event_count: [],
    apply=lambda state, record: state,   # identity gate is mandatory
    wire=lambda state: tuple(state),     # client-visible value (optional)
    summary=True,
)

registry = ProjectionRegistry()
handle = registry.register(definition)          # refcounted registration
handle.state_of(session)                       # folds lazily, then advances
registry.notify(session, session.records[-1])  # after every durable append
values = registry.snapshot(session)            # wire values of live units
rows = registry.checkpoint(session)            # {key: {ver, seq, val}}
registry.restore(session, rows)                # replay tail, no listener events
```

Signatures (all keyword-only where marked):

| call | result |
| --- | --- |
| `register(definition)` | `ProjectionHandle`; sharing one key/version/summary increments a refcount |
| `keys()` | live keys, registration order |
| `state_of(session, key)` | convenience read of one live unit |
| `snapshot(session, *, keys=None)` | {key: wire(state)} for live units that have a wire |
| `as_of_seq(session)` | session.last_seq after materializing every live unit |
| `notify(session, record)` | advance every live unit through record; fires change listeners |
| `on_changed(listener)` | listener(session_id, key, wire_value) -> None; returns idempotent unsubscribe |
| `checkpoint(session)` | {key: {"ver": int, "seq": int, "val": state}} (deep-copied) |
| `restore(session, checkpoint)` | install usable rows, refold the rest; no notifications |
| `ProjectionHandle.state_of(session) / reader_for(session) / dispose()` | read stays available after dispose |

## 2. Cells, init/apply, identity gate

A cell is the materialized state of one unit for one session, keyed by
`(session.session_id, key)`. Cells are lazy: nothing is computed at registration.
The first read (or the first notify) calls `init(header, inherited_event_count)` once and
replays records in log order. Later reads advance to `session.last_seq`.

Contract for `apply(state, record)` (same discipline as DSH):

- Unrelated records MUST return the same state object (`is`), not a copy.
- A record that changes the unit MUST return a complete next state, never a delta.
- State SHOULD be JSON-native (dict/list/str/int/float/bool/None) because checkpoints
  serialize it; tuples become lists after a restore roundtrip.

The identity gate is what keeps notifications meaningful: `notify` only fires when the
wire value changes identity. Reads, snapshot, checkpoint and restore never fire listeners.
`init` receives the inherited event cut (section 7) so seeded sessions can seed statistics.

## 3. Notification and self-healing

`notify(session, record)` is the only advancement API. For every live unit it
applies all records up to and including `record.seq` (skipping ones already applied),
then applies the record itself exactly once. Duplicate notifies are no-ops because the cell
remembers `observed_seq`. If a caller forgets to notify, the next notification or read
self-heals by replaying the gap in order; trimmed logs (seq gaps) are tolerated.

After a unit changes, its optional `wire(state)` is evaluated. Listeners fire in
registration order only when the wire value changed identity (`is` comparison) relative
to the last observed view. Listener exceptions are logged and swallowed so one broken
consumer cannot break appends or other listeners; unsubscribe is idempotent.

`notify` order matters: the TUI/AgentSession should notify after `Session.append` returns
(the record is durable and seq-ordered), and before emitting dependent UI events.

## 4. Sidecar checkpoints

Path: `str(session.path) + ".projections.json"`, i.e. adjacent to the log file for both
flat and nested session layouts (`session-<id>.jsonl.projections.json`).

```json
{"version": 1, "cells": {"activity/todos": {"ver": 1, "seq": 1437, "val": [...]}}}
```

- `ver` is the unit state_version; `seq` is the last record seq folded into `val`;
  `val` is a deep copy of the JSON-native state.
- Only live registrations are checkpointed; a unit whose handle was disposed but whose cell
  was already observed keeps its row so a later re-registration can still restore (DSH parity).
- Writes are atomic: encode and validate first (non-JSON state raises `ProjectionError`),
  write a same-directory temp file, flush + `os.fsync`, then `os.replace`. Failed writes
  remove the temp file; no partial sidecar is ever visible.
- This is write-behind storage: call `checkpoint` / `save` on flush boundaries or session
  close, not per event. Per-event sidecar writes would dominate the append cost.
- Reads are tolerant: a missing file is not an error; corrupt JSON, an unknown `version`, or
  malformed rows log a warning and degrade to no checkpoint (bad rows are skipped one by
  one). A corrupt sidecar must never prevent opening a session.

`ProjectionCheckpoints(registry, *, enabled=True)` bundles this: `path_for(session)`,
`save(session) -> Path | None` (None when disabled), `load(session) -> bool`, and
`restore_if_available(session) -> bool`. `save_checkpoint` / `load_checkpoint` are the
functional equivalents.

## 5. Restore algorithm

1. Read and decode the sidecar (tolerant, section 4).
2. For each live registration, accept a row only when it is a mapping with
   `ver == state_version` (bool is not an int), `seq` an int in `[-1, session.last_seq]`,
   and a `val` field. Otherwise the row is unusable.
3. Usable row: install a deep copy of `val` at `observed_seq = seq`, then replay only
   records with seq > row.seq (in log order, gaps tolerated).
4. Unusable or missing row: discard any cell and refold from `init` plus every record.
5. Unknown keys in the checkpoint are ignored. Restore fires no listener events and does
   not change the session log.

Per-key independence differs from DSH `restoreFloor`: DSH computes one floor (the lowest
usable watermark) and either replays the whole registry from there or rejects the restore.
This port restores each unit independently - a stale unit refolds fully while fresh units
replay only their tail - because sessions are per-key and a partial restore is strictly
more useful than an all-or-nothing one. `as_of_seq` is always the session last seq after
materialization, matching DSH `snapshot.asOfSeq`.

## 6. Fork and the inherited event cut

`inherited_event_count(session)` returns the list POSITION of the first
`session/end-seed` record whose data has `inherited: true`, else 0. A fork writes that
marker immediately after the copied prefix (see `session/fork.py`), so its index equals the
number of inherited events. Positions, not seq arithmetic, keep this correct for trimmed
logs.

The count is passed to every `init` call. Cells still fold the whole retained log
(including the inherited prefix), which reproduces the parent materialized state exactly;
the cut is metadata for units that want to distinguish inherited history from post-fork
activity. A child never copies the parent sidecar; if the cut marker was trimmed away from
a fixture sample, the count is 0 by design (documented limitation: the retained log cannot
prove the cut).

## 7. Wiring points

- `builtin/projections.py` (Lead) should be a plugin with `provide="projections"`,
  owning one `ProjectionRegistry` and one `ProjectionCheckpoints`, registering the
  activity/stats/title definitions and exposing the registry plus checkpoint helpers.
- AgentSession (or the TUI session driver) calls `registry.notify(session, record)` after
  each successful append (tool results, todos, jobs, member updates, ...). There is no
  session-module observer in P1; a missed notify is recoverable, so integration can be
  incremental.
- On clean shutdown or an explicit flush barrier, call `ProjectionCheckpoints.save(session)`;
  on open, call `restore_if_available(session)` before serving clients.
- The activity definitions live in `tests/projection/activity_defs.py` as the parity oracle
  for `ActivityService.refresh`; production equivalents belong to the B3 plugin, not to
  this package (activity.py is owned elsewhere and must not be modified here).

## 8. Deliberate differences from DSH

| DSH | this port | why |
| --- | --- | --- |
| stateSchema/viewSchema and IPC wire payloads | none; wire() returns Python values | no client protocol in P1 |
| snapshot returns {asOfSeq, values} | snapshot returns values only, plus as_of_seq() helper | keeps the plan signature |
| session drives projections from append observers | explicit notify(session, record) | no session-module edits allowed in P1 |
| restoreFloor all-or-nothing floor | per-key restore rows | partial restore is more useful |
| listener exceptions propagate | logged and swallowed | one bad TUI listener must not break the loop |
| summaryKeys, cachedSnapshot, onEventChanged | not implemented | P1 non-goals |
| ProjectionError in core errors | local ProjectionError(SessionError, ValueError) | core.errors is frozen |

## 9. Test evidence

- `tests/projection/test_registry.py`: definition validation, key sharing and refcounts,
  stateVersion/summary conflicts, lazy fold, duplicate notify, missed-notify self-heal,
  identity gate, late registration, snapshot keys/wire filtering, listener error isolation,
  deep-copied checkpoint rows, inherited-marker counting, unknown-key errors.
- `tests/projection/test_checkpoint.py`: sidecar path/shape, atomic write (no temp
  leftovers), restored values equal full replay on five fixture sessions (including trimmed
  and replace-heavy logs), a synthetic 9-successive-replace session, tail-only replay
  (apply count), stale stateVersion full refold, row beyond log end discarded, corrupt and
  missing sidecars non-fatal, no listener events on restore, enabled/disabled toggle,
  non-JSON state rejection, encode/decode tolerance.
- `tests/projection/test_activity_parity.py`: the four activity definitions match
  `ActivityService.refresh` field by field on seven fixture sessions plus a synthetic
  session covering team/member, subagent descriptor/catalog, plan/mode and job lifecycle.
- `tests/projection/test_fork.py`: synthetic fork with an open tool call (marker
  index and init argument), real fixture fork marker position, and the trimmed seeded
  fixture limitation (marker absent implies inherited count 0).

Run: `uv run pytest -o addopts= -q tests/projection` (42 tests, all green). Restore
performance evidence: on fixture session-e33a50ed (1437 records) a full snapshot takes
1.4 ms, checkpoint save 4.9 ms and sidecar load 1.2 ms; a synthetic 5012-record session
restores from checkpoint plus a 10-record tail in 2.0 ms versus 4.5 ms for full replay, and
the tail-only test proves only post-checkpoint records are re-applied.

## 10. Non-goals (P1)

Client wire schema and IPC push, projection caches for session listings, migrating the
whole TUI subscription graph, and checkpoint-policy flush barriers (those already exist as
append flush + fsync in `deeppyro.session`).
