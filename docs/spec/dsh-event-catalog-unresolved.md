# DSH event catalog: unresolved evidence and deeppyro mapping

Generated from `scripts/extract_dsh_events.mjs --dump` over the shipped asar (14654 files). Static totals: bus=94, internal=19, record=61, noise=2328.

## 1. Subscribe-only names (evidence gap)

These names appear only in an `on(...)` call: nothing in the shipped code dispatches them, so a listener for one would never fire. They are excluded from the bus contract until an emitter shows up.

- `agent/disposed` (agent)
- `approval/request` (approval)
- `command/executed` (command)
- `credentials/record-updated` (credentials)
- `goal/changed` (goal)
- `llm/adapters-updated` (llm)
- `session/created` (session)
- `session/disposed` (session)
- `session/event` (session)
- `session/flush` (session)
- `subagent/end` (subagent)
- `subagent/provider-removed` (subagent)
- `system-prompt/assemble` (system-prompt)
- `tools/post-execute` (tools)
- `tools/ptc-dispatch-log` (tools)
- `tools/result` (tools)
- `workflow/agent-end` (workflow)
- `workflow/agent-start` (workflow)
- `workflow/log` (workflow)
- `workflow/phase` (workflow)

Count: 20

## 2. Unresolved call sites

75 call sites pass an expression we cannot resolve to a literal name (helper indirection, string variables, template literals). They are reported, not guessed: a name only enters the catalog through a literal.

## 3. deeppyro's current bus names

| deeppyro hook | in DSH catalog | DSH modes | evidence |
| --- | --- | --- | --- |
| `agent/pre-step` | yes | on, waterfall | 17 site(s) |
| `agent/request` | yes | on, waterfall | 2 site(s) |
| `agent/request-error` | yes | on, waterfall | 4 site(s) |
| `agent/status` | yes | emit, on | 9 site(s) |
| `agent/turn-stopping` | yes | on, serial | 2 site(s) |
| `agent/assistant-stream` | yes | emit, on | 4 site(s) |
| `agent/error` | yes | emit, on | 10 site(s) |
| `system-prompt/assemble` | yes | on | 1 site(s) |
| `llm/stream` | yes | on, waterfall | 7 site(s) |
| `session/created` | yes | on | 7 site(s) |
| `session/disposed` | yes | on | 12 site(s) |
| `tools/pre-execute` | yes | on, waterfall | 5 site(s) |
| `tools/execute` | yes | on, waterfall | 3 site(s) |
| `tools/post-execute` | yes | on | 4 site(s) |
| `approval/request` | yes | on | 2 site(s) |

## 4. Names retired from the bus

| name | DSH shape | decision | migration |
| --- | --- | --- | --- |
| `tool/call` | record (0 dispatch sites) | removed from the bus, constant kept as DEPRECATED | `tools/pre-execute`, `tools/execute`, `tools/post-execute`, `tools/result` |
| `model/selection` | record | removed from the bus, constant kept as DEPRECATED | record `request/header` + `request/context` |
| `approval/asked` | record | removed from the bus, constant kept as DEPRECATED | record `approval/asked` written by the approval gate |
| `approval/decided` | record | removed from the bus, constant kept as DEPRECATED | record `approval/decided` written by the approval gate |

## 5. Three numbers

| measurement | value |
| --- | --- |
| static bus names in the shipped DSH asar | 94 |
| names deeppyro currently dispatches (`HOOK_NAMES`) | 15 |
| runtime-observed in the harness smoke | 10 |


The difference is the useful signal, not the totals:

| set | size | meaning |
| --- | --- | --- |
| DSH bus names | 94 | the contract to grow into |
| deeppyro declares | 15 | names with a constant and a dispatch site |
| observed in the smoke | 10 | `agent/pre-step`, `agent/request`, `agent/status`, `agent/turn-stopping`, `approval/request`, `llm/stream`, `system-prompt/assemble`, `tools/pre-execute`, `tools/execute`, `tools/post-execute` |
| declared, not observed here | 5 | `agent/error`, `agent/request-error`, `agent/assistant-stream`, `session/created`, `session/disposed` — they need a failing provider, a streaming sink, and the sessions service respectively |
| DSH bus names deeppyro does not implement | 79 | M2 catalogue work |

Reproduce with `uv run pytest tests/integration/test_event_harness.py -s`.

## Mode evidence

`modes` in the catalog and in `core/event_catalog.py` lists *dispatch* modes only. `on`/`once` are subscription verbs and are recorded separately (`subscribed` in the JSON dump); a handler whose last parameter is `next` implies a waterfall dispatch.
