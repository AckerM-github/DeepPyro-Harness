# The kernel event catalog

> **状态**：现行（0.3.0） · **层级**：native 内核总线目录 · **最后核对**：2026-11-19
> 0.3.0 native-only：事件目录就是 `src/deeppyro/core/events.py`，没有外部 DSH 目录、没有生成脚本、没有生成文件。
> 传输层是 `deeppyro.hooks.service.HookService`（也作为服务名 `hooks` 暴露）；本文描述目录与派发语义。

Every domain event goes through one bus. A name either exists (with a dispatch mode and a producer) or it
does not exist at all; there is no second, private channel. `core/events.py` answers three questions for every
name: does it exist, how does it dispatch, and who produces it.

## 1. One list, two sources

`EVENT_SPECS` merges exactly two sources, in this order:

| source | contents | status |
| --- | --- | --- |
| `_PRODUCERS` in `core/events.py` | the 24 names deeppyro really dispatches | `implemented` |
| `_RESERVED` in `core/events.py` | 22 domain names a plugin may subscribe to before a producer exists | `reserved` |

The table has **46 entries: 24 implemented and 22 reserved**. A name outside the table is not a bus event,
so `PluginContext.on()` would treat it as a dead subscription. Reserved entries carry a `reason` and a
namespace-shaped `producer` string; they have no dispatcher yet.

`EventSpec` is a frozen dataclass with `name`, `mode`, `status`, `producer` and `reason`; `EventMode` is one of
`emit`, `serial`, `waterfall`, `bail`, `parallel`; `EventStatus` is `implemented` or `reserved`.
Public read helpers: `spec(name)`、`is_bus_event(name)`、`mode_of(name)`、`producer_of(name)`、`names(include_reserved=True)`.

## 2. Implemented events

`producer` names the subsystem responsible for dispatching the event; it is the catalog's declaration, not a
promise that every path emits it in every situation. Call sites are authoritative.

| event | mode | producer |
| --- | --- | --- |
| `agent/pre-step` | waterfall | `agent.loop` |
| `agent/request` | waterfall | `agent.loop` |
| `agent/request-error` | waterfall | `agent.loop` |
| `agent/turn-stopping` | serial | `agent.loop` |
| `agent/assistant-stream` | emit | `agent.loop` |
| `agent/status` | emit | `agent.loop` |
| `agent/error` | emit | `agent.loop` |
| `llm/stream` | emit | `agent.loop` |
| `system-prompt/assemble` | waterfall | `prompt.service` |
| `session/created` | emit | `sessions.service` |
| `session/disposed` | emit | `sessions.service` |
| `tools/pre-execute` | waterfall | `tools.pipeline` |
| `tools/execute` | waterfall | `tools.pipeline` |
| `tools/post-execute` | waterfall | `tools.pipeline` |
| `approval/request` | waterfall | `sandbox.approval` |
| `command/executed` | emit | `commands.service` |
| `workspace/changed` | emit | `working_directory.service` |
| `goal/changed` | emit | `goal.service` |
| `goal/activation-changed` | emit | `goal.service` |
| `mcp/connected` | emit | `mcp.service` |
| `mcp/disconnected` | emit | `mcp.service` |
| `agent/inbox/inserted` | emit | `agent.inbox` |
| `agent/inbox/claimed` | emit | `agent.inbox` |
| `agent/inbox/discarded` | emit | `agent.inbox` |

`HOOK_MODES` in `hooks/types.py` is the mode source for hook-facing names; nine higher-level names
(`command/executed`, `workspace/changed`, the two goal events, the two MCP events, the three inbox events)
are declared in `_EXTRA_MODES` because they were never part of the legacy hook surface. Building the table
fails fast: a `HOOK_MODES` name without a producer, or an implemented name without a mode, raises at import.

## 3. Dispatch modes

| mode | contract |
| --- | --- |
| `emit` | every listener runs; return values are ignored |
| `serial` | listeners run in order, each may return a replacement value that the next listener sees |
| `waterfall` | listeners receive the payload and a `next()` continuation; a listener may wrap or replace the result |
| `bail` | the first listener returning a non-`None` value stops the chain and that value becomes the result |
| `parallel` | listeners are awaited concurrently; ordering is not part of the contract |

`HookService` is a facade over `EventBus`: it forwards registration and dispatch and adds the one policy that
belongs to hooks rather than the bus — which names may be registered. The bus itself is name-agnostic, so the
kernel journal, prompt assembly, tool pipeline and plugins share one registry. Payloads are frozen recursively
before dispatch (mappings become read-only proxies, lists/tuples become tuples, custom objects keep identity),
so handlers treat payload data as read-only. `HooksPlugin` provides both `bus` and `hooks` from one instance,
and `PluginContext.on()` prefers `bus`; a subscription through either door is dispatched through the other.

## 4. Reserved events

Reserved names let a plugin subscribe before the producing subsystem exists; they carry a reason and are
recognised, so the subscription is explicit rather than a silent no-op. Adding a producer means moving the
name into `_PRODUCERS` and giving it a mode in `HOOK_MODES` (or `_EXTRA_MODES`), after which the catalog tests
fail until both exist.

| namespace | entries | why reserved now |
| --- | --- | --- |
| `api-session` | 4 | the web API owns these; the loopback host does not publish a separate session API yet |
| `compaction` | 3 | compaction is a durable record (`compaction.done`); its lifecycle is silent |
| `credentials` | 1 | credentials are read on demand |
| `fs` | 1 | file watching is not implemented |
| `jobs` | 1 | job bookkeeping is polled, not published |
| `loader` | 1 | plugin loading does not publish itself |
| `llm` | 1 | model selection is a session record, not a bus event |
| `plugin` | 1 | plugin status is read through services |
| `sandbox` | 1 | policy changes are read, not announced |
| `schedule` | 1 | the scheduler fires durable runs, not bus events |
| `settings` | 1 | settings write-through has no observers |
| `subagent` | 2 | child-session lifecycle is folded from session facts |
| `team` | 2 | team membership lives in team state, not events |
| `tools` | 1 | `tools/result` is a session record name |
| `web` | 1 | no fetch subsystem |

## 5. What is deliberately absent

- **No external/D​SH catalog.** 0.3.0 removed `core/event_catalog.py`, `scripts/gen_event_catalog.py`,
  `scripts/extract_dsh_events.mjs` and the `docs/spec/dsh-event-catalog*.md` evidence files. The catalog is
  hand-maintained native code; there is no generated file to drift or regenerate.
- **No `internal/*` plumbing events.** The native runtime raises no events of its own; service registration,
  fiber state changes and configuration updates are plain method calls.
- **No session records on the bus.** `turn/end`, `tool/call`, `approval/asked/decided`, `model/selection` and
  similar names are durable session-log entries, not bus events. `model/selection`、`tool/call`、
  `approval/asked`、`approval/decided` 常量在 `hooks/types.py` 中仅保留一个 minor 的可导入性，已不再注册/派发。

## 6. In-tree consumers

| subscriber | name(s) | kind |
| --- | --- | --- |
| `builtin/hook_observability.py` | curated debug/report names | logging observer |
| `builtin/approval_policy.py` | `approval/request` | waterfall |
| `builtin/prompt_extension.py` | `system-prompt/assemble` | waterfall |
| `builtin/session_title.py` | `agent/turn-stopping` | serial |
| `builtin/usage.py` | `llm/stream`, `agent/pre-step`, `agent/turn-stopping`, `session/disposed` | emit listeners + record fold |
| `builtin/commands.py` | `command/executed` | emit |
| `mcp/service.py` | `mcp/connected`, `mcp/disconnected` | emit |
| `goal/native_driver.py` | `goal/changed`, `goal/activation-changed` | emit |
| `working_directory/service.py` | `workspace/changed` | emit |

Ordering is registration order, `prepend=True` runs first, `once=True` disposes before its handler runs, a
scoped listener only sees dispatches whose scope contains every pair, `global_=True` bypasses filtering, and a
disposer takes effect for the next dispatch. `tests/integration/test_bus_consumers.py` pins these rules;
`tests/integration/test_bus_modes.py` and `tests/integration/test_single_bus.py` cover catalog modes and the
single-bus property.

## 7. The goal bus contract

| name | mode | producer | payload |
| --- | --- | --- | --- |
| `goal/changed` | emit | `goal.service` | `{session, goal?, change{operation, ref{id, revision}, goal?}}` |
| `goal/activation-changed` | emit | `goal.service` | `{sessionId, goal?{id, revision, activation}}` |

`change` is the frozen domain object: `operation` is one of `create`, `edit`, `pause`, `resume`, `complete`,
`block`, `clear`; `ref` identifies the snapshot the operation produced (after `clear` it names the tombstone
revision); `goal` is the full goal view or `None` after `clear`. The bus is global, so `goal/changed` carries an
extra `session` key on top of `change`. Everything inside `change` is the contract in [goal.md](goal.md) §6.

`goal/activation-changed` fires only when process-local activation actually changes: `create` and `resume` arm a
goal, `pause`/`complete`/`block`/`clear` disarm it, `edit` leaves it alone, and every new agent starts disarmed.