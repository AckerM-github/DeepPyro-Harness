# The kernel event catalog

This document describes the single bus catalogue that decides whether a name is a
domain event, how it dispatches, and who produces it. The transport is
`deeppyro.hooks.service.HookService` (service name `hooks`); the catalogue is
`deeppyro.core.events`.

## 1. One list, three sources

`EVENT_SPECS` merges exactly three sources, in this order:

| source | contents | status |
| --- | --- | --- |
| `_PRODUCERS` in `core/events.py` | the 15 names deeppyro really dispatches | `implemented` |
| `BUS_EVENTS` in `core/event_catalog.py` | all 94 DSH bus events from the M0 catalog | `implemented` if produced, else `reserved` |
| `_RESERVED` in `core/events.py` | 21 names deeppyro declares without a DSH counterpart | `reserved` |

The table has 115 entries: 15 implemented and 100 reserved. A name outside the
table is not a bus event, so `PluginContext.on()` routes it to the runtime context's local
dispatcher instead (nothing in the kernel uses that path).

## 2. Generated, never hand-edited

`core/event_catalog.py` is generated:

```powershell
node scripts/extract_dsh_events.mjs --dump $env:TEMP\dsh-events.json
python scripts/gen_event_catalog.py --json $env:TEMP\dsh-events.json
python scripts/gen_event_catalog.py --check        # CI-style drift check
```

The generator strips the `dsh/node_modules/` prefix from evidence paths, sorts
names and modes, and keeps the first call site, so the committed file has no
environment-specific detail.

## 3. Mode evidence and the one deliberate divergence

The catalog's `modes` column records *dispatch* modes only. Subscription verbs
(`on`/`once`) are tracked separately as `subscribed`, because a handler's arity
tells us how the event is dispatched: a trailing `next` parameter means the
caller expects a waterfall, a single parameter means plain emit. Before that
distinction the catalog reported `emit` for four names that DSH actually
waterfalls; `mode_conflicts()` therefore dropped from four entries to one.

| name | deeppyro | DSH catalog | status |
| --- | --- | --- | --- |
| `llm/stream` | emit | waterfall | deliberate deviation, see below |

DSH waterfalls `llm/stream` over the *stream object itself*
(`this.ctx.waterfall(this, "llm/stream", options, () => this.adapterStream(...))`),
so a handler can replace the whole iterator. deeppyro emits one event per delta
instead: the streaming path is hot, and every consumer so far (sink, journal,
observability) wants deltas, not the iterator. Converging would give plugins the
power to rewrite the stream but would put an await plus a payload copy on the
per-token path. The deviation stays explicit — it is returned by
`mode_conflicts()` and pinned by a test — until a consumer needs to replace the
stream rather than watch it.

## 4. Reserved entries carry a reason

Every reserved spec has either a DSH evidence path (`dsh_ref`) or an explicit
`reason`. A reserved name is recognised, so a plugin subscribing to it gets a
clear subscription instead of a silent no-op, but nothing emits it yet. Adding a
producer means moving the name into `_PRODUCERS` and giving it a mode in
`HOOK_MODES`; the catalogue test fails until both exist.

## 5. What is deliberately absent

`internal/*` plumbing events from the former framework port are gone: the native runtime
raises no events of its own, so names such as `internal/dispatch` do not exist here.
Session records (`turn/end`, `tool/call`, …) are also absent: they are durable log
entries, and `session/archived`-style additions must be written with `ignorable=True`
rather than registered here.

## 6. Real consumers

The bus is not a single-consumer pipe: six in-tree subscribers prove it, and
`tests/integration/test_bus_consumers.py` pins the ordering rules.

| subscriber | name(s) | kind |
| --- | --- | --- |
| `builtin/hook_journal.py` | every dispatch | observer (`hook/invoked` + `hook/result`) |
| `builtin/hook_observability.py` | 6 curated names | logging |
| `builtin/approval_policy.py` | `approval/request` | waterfall |
| `builtin/prompt_extension.py` | `system-prompt/assemble` | waterfall |
| `builtin/llm_retry.py` | `agent/request-error` | emit listener |
| `builtin/session_title.py` | `agent/turn-stopping` | serial |

Ordering is registration order, `prepend=True` runs first, `once=True` disposes
before its handler runs, a scoped listener only sees dispatches whose scope
contains every pair, `global_=True` bypasses filtering, and a disposer takes
effect for the next dispatch.

## 7. Transport vs catalogue

Three pieces, three jobs:

| piece | knows | does not know |
| --- | --- | --- |
| `core/event_catalog.py` | every DSH bus name + evidence | what deeppyro emits |
| `core/events.py` | which names exist, their mode, their producer | how a dispatch runs |
| `core/bus.py` (`EventBus`) | listener registry, scope filter, freezing, observers, five modes | any event name |

`HookService` is a facade over the very same `EventBus`: it forwards
registration and dispatch and adds the one policy that belongs to hooks rather
than to the bus -- which names may still be registered. Retired names fail with a
message naming their replacement; the bus itself is name-agnostic, which is what
lets the kernel journal, prompt assembly, and plugins share one registry.

`HooksPlugin` provides both services (`bus` and `hooks`) from one instance, and
`PluginContext.on()` prefers `bus`, so a plugin subscription and a hook
subscription land in the same table. `tests/integration/test_single_bus.py`
pins that: a subscription through either door is dispatched through the other.

## 8. internal/* plumbing

The native runtime (`kernel/container.py`, `kernel/fiber.py`) raises **no** plumbing
events: service registration, fiber state changes and configuration updates are plain
method calls. The former catalogue of ten upstream `internal/*` events was deleted with
the vendored port, so the names are not in `EVENT_SPECS` at all.

`is_bus_event("internal/…")` is False simply because no such name is registered; binding
a handler to one would be a dead subscription, which the "declared but unproduced"
reservation rule in §4 is designed to prevent. `scripts/extract_dsh_events.mjs` may still
list upstream `internal/*` names as reference for DSH parity — they are not part of this
contract.

## 9. Milestone status

| milestone | state | evidence |
| --- | --- | --- |
| M0 catalogue rebuild | done | `scripts/extract_dsh_events.mjs`, `docs/spec/dsh-event-catalog.md` |
| M1 single bus in front of plugins | done | `core/bus.py`, `PluginContext.on()` routes bus names, `hook-bridge` deleted |
| M1b transport/catalogue split | done | `EventBus` raises anything, `HookService` keeps the name policy |
| M2 catalogue + payload contract | done | `core/event_catalog.py` (94 names), `EVENT_SPECS` = 115 entries |
| M3 mode evidence + semantics | done | `extract_dsh_events.mjs` reads dispatch modes, `test_bus_modes.py` |
| M3b internal/* plumbing | done | catalogue dropped with the vendored port; the native runtime raises no plumbing events |
| M4 consumers | done | six in-tree subscribers listed in §6, `test_bus_consumers.py` |
| M5 documentation freeze | done | this document plus `hooks.md` §9 |

One deviation survives on purpose: `llm/stream` is `emit` here and `waterfall` in
DSH (§3). Everything else matches name for name.

## 10. Reserved namespaces (P1)

"The bus knows the name before the subsystem exists" is the rule for these
namespaces. Each entry carries a reason and, when DSH has the same event, the
call site that proves it:

| namespace | entries | why declared now |
| --- | --- | --- |
| `subagent` | 5 | the plugin exists, the events are not raised yet |
| `team` | 2 | team members land in `team/` state files, no events yet |
| `schedule` | 3 | the scheduler fires jobs, not events |
| `compaction` | 4 | compaction is a record, its lifecycle is silent |
| `goal` | 2 | **both implemented** in M5: `goal/changed` and `goal/activation-changed` (section 11) |
| `workspace` | 3 | `workspace/changed` is implemented for cwd moves; the rest wait |
| `jobs` | 1 | job bookkeeping is polled, not published |
| `fs` | 5 | file watching is not implemented |
| `web` | 1 | no fetch subsystem |
| `credentials` | 2 | credentials are read on demand |
| `settings` | 2 | settings write-through has no observers |
| `loader` | 6 | DSH's loader has no counterpart here |
| `agent-preset` | 1 | presets resolve statically |
| `app-boot` | 1 | boot does not publish itself |
| `mcp` | 2 | **implemented**: `mcp/connected` and `mcp/disconnected` fire from `mcp.service` (see `mcp.md`) |
| `api-session` | 6 | the web API owns these, there is no web API |
| `sandbox` | 1 | policy changes are read, not announced |
| `command` | 1 | **implemented**: `command/executed` fires for every run |

`spill` and `attachment` are absent from the DSH catalogue, so nothing is
declared for them: an empty namespace would be a shell, and the brief forbids
shells.

## 11. The goal bus contract (M5)

Two names carry the goal state machine:

| name | mode | producer | payload |
| --- | --- | --- | --- |
| `goal/changed` | emit | `goal.service` | `{session, goal?, change{operation, ref{id, revision}, goal?}}` |
| `goal/activation-changed` | emit | `goal.service` | `{sessionId, goal?{id, revision, activation}}` |

`change` is the frozen DSH object: `operation` is one of `create`, `edit`,
`pause`, `resume`, `complete`, `block`, `clear`; `ref` identifies the snapshot the
operation produced (after `clear` it names the tombstone revision); `goal` is the
full goal view or `None` after `clear`.

DSH publishes these events per agent, so its frame needs no session id. Our bus
is global, so `goal/changed` carries one extra `session` key on top of `change`,
and `goal` (the objective string) stays for the pre-M5 consumers. Everything
inside `change` is exactly the contract in `docs/spec/goal.md` section 6.

`goal/activation-changed` fires only when the process-local activation actually
changes: `create` and `resume` arm a goal, `pause`/`complete`/`block`/`clear`
disarm it, `edit` leaves it alone, and every new agent starts disarmed.
