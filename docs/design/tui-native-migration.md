# TUI native migration (KI-001 / KI-037)

Status: design note for 0.3, not yet implemented for the turn engine. Read-only
native session visibility is tracked separately (KI-037). Authority for release
scope stays in `docs/release/0.2.0-known-issues.md`.

## 1. Goal and non-goals

Goal: the interactive TUI runs the schema 1 native agent loop by default, while
the v4 reader stays available for old sessions through `dpyro migrate`.

Non-goals for the first switch:

- no dual-write and no silent session-format conversion;
- no v4 deletion (that follows the provider port, KI-010);
- no behaviour change for team/subagent child sessions in the same commit.

## 2. Current coupling

The TUI is not one `AgentSession` call site. Its runtime binds services to a v4
session object:

| TUI call site | Needs from the session |
| --- | --- |
| `tui/runtime_session.py:71` | `store.create(cwd, agent_preset=...)` |
| `tui/runtime_session.py:85-96` | `agents.create(session=..., sink=..., services=...)` |
| `tui/runtime_session.py:140-157` | `header.cwd`, `session.append`, `session.inject` |
| `tui/runtime_session.py:159-210` | `bind_session(session)` for team/subagent, `bind_parent(agent)` |
| `tui/runtime_session.py:111-138` | `projections.save/attach/detach(session)` |
| `tui/runtime_status.py:91-113` | `session_id`, `usage()`, `context_tokens` |
| `tui/app_turns.py:35-47` | `agent.send(text)` streaming into the sink |
| `tui/app_turns.py:85-90` | `agent.interrupt()` |
| `tui/app_sessions.py:228` | `store.open(id)` + `derive_messages()` |

The native loop already provides the hard part: `AgentLoop.run` folds
`NativeSession.events` on every run (`agent/loop.py:492-494`), so multi-turn is
repeated runs over one open session. What is missing is the host-shaped read
surface and the child-session plumbing.

## 3. Target layering

```
Textual app (tui/*)
        |
        v
TuiRuntime (facade, tui/runtime.py)
        |
        +-- NativeAgentHost (agent/native_host.py)  <- one live session
        |      +-- NativeSessionHandle (v4-shaped read surface)
        |      +-- AgentLoop (native turn engine)
        |
        +-- NativeSessionIndex (session/native_index.py)  <- list/preview
        |
        +-- Service adapters: working_directory, projections, team, subagent
```

`NativeAgentHost` and `NativeSessionHandle` are specified in
`docs/design/native-host.md`. The handle deliberately exposes only the read
surface the TUI already uses; it is not a compatibility layer for arbitrary v4
APIs.

## 4. Phases

### Phase 1 (this round): read-only visibility (KI-037)

- `/sessions` merges v4 and native entries, labelled by source.
- `/resume` on a native id prints a bounded transcript and explains migration.
- The live v4 session is untouched; the turn engine is unchanged.

Exit: a native session produced by headless is visible in the TUI.

### Phase 2: single-agent native turns behind a flag

- Add `DEEPPYRO_TUI_NATIVE=1` (and a `--native-ui` alias) selecting a native
  backend for the standard preset; v4 stays the default until the flag has
  passed the full TUI test matrix.
- `new_session` creates `NativeSession`; `send` drives `NativeAgentHost`;
  `interrupt` cancels the in-flight task; usage/context status read the handle.
- `/sessions` lists both formats; `/resume` on native reattaches the native
  backend, on v4 keeps the v4 path (read-only until migrated).
- Team preset and any preset whose tools need v4 child sessions stay on v4 in
  this phase; the runtime refuses the flag with a clear message instead of
  half-binding services.

Exit: standard-preset TUI turns write schema 1, stream tokens/tools, persist
title and usage, and survive restart. The v4 path is still green.

### Phase 3: service bindings

Port in this order, one commit each, with the v4 path still present:

1. `working_directory`: already keyed by `session_id` + `header.cwd`; the handle
   adapter is enough. Add a native test for `cd` + notice injection.
2. `projections`: attach to `NativeSession` append; if the projection registry
   requires v4 record shapes, add a native record adapter rather than making
   projections understand two schemas.
3. goal/plan/todo: these are tools that write session facts; verify each tool's
   session dependency and route through the handle's append mapping.
4. `schedule`: the TUI schedule host only needs `send`/status; verify cold-start
   replay.
5. team/subagent: these create child sessions through `AgentsPlugin`, which is
   v4. They need a native child factory before the team preset can move. This is
   the last binding and the reason phase 2 refuses the flag for team mode.

### Phase 4: default flip

Flip the default only after phase 3 and a release-candidate window. Keep
`--legacy-v4` for one minor release, then remove the v4 TUI path with the
provider port (KI-010).

## 5. Session handle contract

The TUI may rely on exactly these members; anything else is a bug:

- `session_id: str`
- `header.id`, `header.cwd`, `header.created_at`, `header.preset`
- `append(record_type, data)` for the known event vocabulary; unknown types raise
- `inject(text, source=...) -> bool` (bounded queue, visible counter)
- `usage() -> {input, output, cached}`
- `context_tokens() -> int`
- `derive_messages() -> list` for transcript rebuild
- `aclose()`

Do not add `store`, `writer`, or arbitrary v4 attributes to the handle.

## 6. Risks and open questions

- **Team/subagent child sessions** are the largest unknown: `AgentsPlugin` and
  `SessionsPlugin` create v4 children, and team records are v4-shaped. A native
  child factory must land before the team preset moves.
- **Projections** may encode v4 record semantics; changing the registry touches
  activity/jobs/agents UI. Prefer an adapter at the native append boundary.
- **Compaction** in the native loop is not the v4 compactor; check token budget
  behaviour before claiming parity.
- **Images/attachments** have a native differential case but no TUI rendering
  path yet.
- **Resume semantics**: native sessions can be resumed by opening the file and
  running again; v4 sessions must be migrated first. The UI must say which one
  happened.
- **Two formats in one list**: sorting, title derivation, and archive filtering
  must be unified in the index, not in each command.

## 7. Test strategy

- Unit: handle contract, append mapping, inject bounds, index parsing.
- Integration: headless writes a native session, TUI lists and reads it; native
  TUI turn writes schema 1 and a second process reopens it.
- Parity: run the existing TUI scenario tests twice (v4 and native) once phase 2
  lands; diff only the stream/persistence assertions, not the UI text.
- Gates: full pytest, ruff, format, pyright ratchet, plus the native differential
  matrix.
