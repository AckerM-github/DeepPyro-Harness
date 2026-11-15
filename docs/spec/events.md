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
table is not a bus event, so `PluginContext.on()` routes it to cordis instead.

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

## 3. Mode conflicts (M3 work list)

Four implemented names dispatch with a mode DSH does not use for them. They are
listed by `events.mode_conflicts()` and pinned by a test:

| name | deeppyro | DSH catalog |
| --- | --- | --- |
| `approval/request` | waterfall | emit |
| `llm/stream` | emit | waterfall |
| `system-prompt/assemble` | waterfall | emit |
| `tools/post-execute` | waterfall | emit |

`HOOK_MODES` stays the dispatch truth until M3 reconciles them one at a time.

## 4. Reserved entries carry a reason

Every reserved spec has either a DSH evidence path (`dsh_ref`) or an explicit
`reason`. A reserved name is recognised, so a plugin subscribing to it gets a
clear subscription instead of a silent no-op, but nothing emits it yet. Adding a
producer means moving the name into `_PRODUCERS` and giving it a mode in
`HOOK_MODES`; the catalogue test fails until both exist.

## 5. What is deliberately absent

`internal/*` events of cordis (`internal/dispatch`, `internal/plugin`, …) are
kernel plumbing, not domain events: they stay inside the vendored cordis port and
are not part of this table. Session records (`turn/end`, `tool/call`, …) are also
absent: they are durable log entries, and `session/archived`-style additions must
be written with `ignorable=True` rather than registered here.
