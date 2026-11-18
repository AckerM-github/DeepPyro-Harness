# TUI native runtime — implementation design (0.3.0 hard cut)

Status: **implemented (0.3.0)** — proposal delivered. Target was DeepPyro 0.3.0,
native-only runtime; the TUI now runs the native `AgentLoop` and the splash offers
F1 Web / F2 terminal. Repository snapshot below (HEAD `7d03ef6` plus in-flight native
modules) is plan-time: several files listed there were removed or renamed during the
cut (e.g. `llm/native_provider.py`, v4 session modules). Treat the body as the design
record; code/tests are authoritative.
Repository snapshot: HEAD 7d03ef6 plus the in-flight native modules in the working tree
(`src/deeppyro/core/llm.py`, `src/deeppyro/agent/model_call.py`,
`src/deeppyro/llm/native_provider.py`, `src/deeppyro/session/native_fork.py`,
`src/deeppyro/session/native_query.py`; parallel implementation agents are editing
`agent/loop.py`, `agent/context_plan.py`, `builtin/llm.py`,
`headless.py`, `session/native_index.py`, `tui/slots.py`).
Where this document cites those modules it cites the working-tree version; P0 re-verifies the anchors in
Appendix D first.

Non-negotiables honoured here: (1) everything is a plugin — the TUI and the headless runner both go
through the `sessions`/`agents`/`prompt`/`schedule`/`team`/`subagent`
plugin services; (2) Pythonic — frozen slotted dataclasses, `Protocol` for extension seams,
`StrEnum` for vocabulary, `asyncio.TaskGroup` for fan-out, no dict-shaped wire
copies of `Message`; (3) AI-native — sessions store facts only (`docs/native/schema.md` §8.5),
token/cost/latency/retry go to the trace store, and every screen can be re-derived by folding the
session log.

---

## 0. Executive summary

The TUI keeps its widget layer, its slot/event-dict contract and its command surface. What changes is
the layer underneath: `TuiRuntime` and its mixins stop talking to
`SessionsPlugin`/`AgentsPlugin` as they exist today (v4 `Session` +
`AgentSession`) and instead talk to two re-implemented plugins:

* `SessionsPlugin` becomes a thin plugin over the schema-1 session primitives
  (`NativeSession` at `src/deeppyro/session/schema.py:407`, catalogue reader
  `src/deeppyro/session/native_index.py`, read/write browser
  `src/deeppyro/session/native_query.py`, fork `src/deeppyro/session/native_fork.py`).
* `AgentsPlugin` becomes a factory for `NativeAgentHost`
  (`src/deeppyro/agent/native_host.py:434`), resolving the preset, building the
  preset-scoped tool registry, the native provider (`LlmPlugin.native_provider`,
  `src/deeppyro/builtin/llm.py:61`) and the prompt source.

Three seams are added:

1. `TuiSink` ← `AgentHooks`: new adapter
   `src/deeppyro/tui/native_hooks.py` re-emits the exact dict event vocabulary the TUI
   already consumes (`tui/app_events.py:16-51` + `tui/slots.py:633`), so no widget,
   slot or plugin status-bar code changes.
2. Prompt facts: new `src/deeppyro/agent/native_factory.py` defines
   `PromptFacts`/`PromptSource` and `AgentBuild`;
   `NativeAgentHost` persists the rendered prompt as a schema-1 `system.message`
   event (`EventKind.SYSTEM_MESSAGE`, `session/schema.py:111`) so every model
   call, compaction and replay sees the same snapshot. Today no writer exists for that event; the native
   loop therefore runs without a system prompt (`agent/loop.py` `_build_plan`
   passes `system=view.system`, always `None` — §4).
3. Native record vocabulary: the seven-entry `RECORD_KIND_MAP` in
   `agent/native_host.py:178-210` is extracted/extended into
   `src/deeppyro/agent/native_records.py` and reused by the loop's tool log
   (`ctx.log`) and by every service that used to read `session.records`
   (permissions, goal, activity, team, subagent). This is what makes "no v4 records" survivable without
   redesigning those services.

Team and subagent keep their plugin services and tool surfaces; their managers stop creating
`Session`/`SessionHeader` from `deeppyro.core.wire` and stop
journaling v4 records; they create child native sessions through `SessionsPlugin` and child
hosts through `AgentsPlugin`, then restore by folding
`plugin.team.*`/`plugin.subagent.*` events and repairing open turns through
the loop.

Compaction stops writing v4 compaction lifecycle records and writes exactly one schema-1
`compaction.done` event per compacted region, with usage/latency sent to the trace store
instead of the session log. The `compress` tool keeps its user-facing message-with-refs
protocol, re-based on message ordinals over the folded message list.

Each phase below is independently testable; the v4 stack stays alive behind alternate plugin ids until
the final deletion phase, so no intermediate commit needs to break the TUI.

### Honest blockers (details in §13)

* Providers that are not OpenAI-compatible cannot run the native loop:
  `llm/registry.py:96` `create_native_provider` raises for any other API. The
  TUI boots through the native provider, so 0.3.0 would drop those providers unless native transports
  land in the same release (KI-010).
* PTC/`run_code` presets have no native surface: the v4
  `AgentsPlugin._surface_registry` (`builtin/agents.py:204`) builds a
  `run_code`-only registry for `presentation == "ptc"`. A native equivalent
  needs a native `run_code` harness (out of scope here). The native runtime must refuse such
  presets explicitly instead of half-working.
* Existing v4 session logs become invisible to the TUI catalogue by construction (the native codec
  rejects the `version` header, `session/schema.py:166-171`; the index skips
  unparseable files, `session/native_index.py:196-200`). 0.3.0 needs a standalone migration
  tool or users lose their history view.

---

## 1. Target layering

```text
DeepPyroApp (Textual widgets + slots + commands)          tui/app.py, app_*.py
        |  event dicts {"type": ...}  (unchanged contract)
TuiSink  <-  TuiAgentHooks (new)  <-  AgentHooks (agent/events.py)
        |                                      ^
TuiRuntime (runtime.py)                        | NativeAgentHost (agent/native_host.py)
  runtime_session/status/schedule/...          |   NativeSessionHandle + AgentLoop
        |                                      |
SessionsPlugin (native)   AgentsPlugin (native factory)   PromptPlugin / PresetsPlugin
        |                                      |
NativeSession (schema 1)   NativeLlmProvider   compactor (new native)   trace store
```

`TuiRuntime` remains a composition facade, not a new abstraction layer: it already owns
every cross-cutting concern (provider/model selection, workspace, permission gate, service wiring,
schedule host, hook emission). The change is that its `agent` attribute is a
`NativeAgentHost` and its `session` attribute is a
`NativeSessionHandle` (`native_host.py:214`), whose contract is fixed by
`docs/design/tui-native-migration.md:111-124` and extended in §2 with three read-only
additions.

---

## 2. Control-plane additions (shared by TUI, headless and team/subagent)

### 2.1 `src/deeppyro/session/native_control.py` (new)

```python
from dataclasses import dataclass
from pathlib import Path

from deeppyro.api.constants import DEFAULT_PRESET_ID


@dataclass(frozen=True, slots=True)
class NewSession:
    """Everything needed to create a schema-1 session; no store object leaks to callers."""

    cwd: Path
    preset: str = DEFAULT_PRESET_ID
    origin: str = "user"          # schema ORIGINS: user|headless|subagent|fork
    parent_id: str | None = None
    delegation_depth: int = 0
    session_id: str | None = None
    created_at: int | None = None
```

`SessionsPlugin.create(NewSession)` turns it into a
`SessionHeader` + `NativeSession.create` (`session/schema.py:447`).
No v4 `SessionHeader` is imported anywhere in the native path.

### 2.2 `src/deeppyro/agent/native_factory.py` (new)

```python
from typing import Protocol, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from deeppyro.agent.events import AgentHooks
from deeppyro.core.llm import ToolSpec
from deeppyro.prompt.prompt import PromptContext


@dataclass(frozen=True, slots=True)
class PromptFacts:
    """Facts the runtime owns; the prompt must not re-derive them from v4 state."""

    cwd: Path
    model: str
    permission_preset: str
    approval_policy: str
    platform: str = ""
    sandbox_policy_text: str = ""
    approval_policy_text: str = ""
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AgentBuild:
    """Inputs for AgentsPlugin.host(); replaces AgentsPlugin.create()'s v4 kwargs."""

    provider: str
    model: str
    cwd: Path
    preset: str
    preset_revision: int | None = None
    permission: object | None = None
    hooks: AgentHooks | None = None
    prompt_facts: PromptFacts | None = None
    services: Mapping[str, object] = field(default_factory=dict)
    context_window: int = 32_768
    max_steps: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class PromptSource(Protocol):
    def render(self, tools: Sequence[ToolSpec], context: PromptContext) -> str: ...


class PresetPromptSource:
    """Renders a preset's prompt sections through PromptPlugin; one impl for every front end."""

    def __init__(self, prompt: PromptPlugin, spec: PresetSpec,
                 facts: Callable[[], PromptFacts]) -> None: ...
    def context(self, session: SessionHeader) -> PromptContext: ...
    def render(self, tools, context) -> str: ...
```

`PresetPromptSource.render` is the only prompt implementation. The TUI, headless and a
future web UI pass `PromptFacts`; nothing re-implements v4's `PromptLayer`
(`agent/session_prompt.py`).

### 2.3 `src/deeppyro/agent/native_records.py` (new)

Extracted from `agent/native_host.py:143-210` (`_notice_source`,
`_normalise_title`, `RecordMapping`, `RECORD_KIND_MAP`) and
extended (Appendix B). Public helpers:

```python
def append_mapped(session, record_type, data=None, **envelope) -> Event
def append_native(session, kind, data=None, **envelope) -> Event
def mapped_kind(record_type) -> EventKind            # raises UnsupportedRecordTypeError
```

`append_mapped` keeps the hard rule from the migration design: an unknown v4 record type
raises `UnsupportedRecordTypeError` (`native_host.py:70`) **before** anything is
written. `append_native` is the typed door used by native-only writers (compactor,
team/subagent, activity plugin events).

### 2.4 `NativeSessionHandle` additions (`agent/native_host.py:214-398`)

Keep the documented contract; add three read-only members and one typed writer:

| member | contract |
| --- | --- |
| `events -> tuple[Event, ...]` | read-only snapshot of the log; no lock taken |
| `fold() -> SessionView` | `fold(header, events)`, cached by `last_seq`; single source for state/messages |
| `append_native(kind, data, **envelope)` | typed writer for native writers; no v4 mapping layer |
| `append(record_type, data)` | unchanged: mapped vocabulary only, raises for unknown types |

`header` stays a stable `NativeHeaderView`
(`native_host.py:82`) with the v4 alias properties, because
`tui/app_base.py`, `tui/session_view.py` and the picker read
`createdAt`/`updatedAt`/`agentPreset`. Aliases are
transitional; P6 removes them together with the TUI's last v4-shaped reads.

### 2.5 `NativeAgentHost` additions (`agent/native_host.py:434-612`)

| member | behaviour |
| --- | --- |
| `attach_prompt(source, facts)` | stores source; calls `refresh_prompt()` |
| `refresh_prompt() -> bool` | renders; if text differs from `fold().system.text()`, appends `system.message` (one `Message(role="system", content=(TextPart(text),), source=SystemSource(), id=new_message_id())`); returns whether it wrote |
| `send(text, *, source=None)` | as today plus `source` forwarded to `loop.run`; calls `refresh_prompt()` first |
| `resume() -> RunResult` | drains `handle` notices into `user.message` events (as `send` does today, `native_host.py:513-514`) and runs `loop.run(None, resume=True)`; refuses when busy |
| `deliver(text, *, source)` | appends a `user.message` with the source when idle, else `handle.inject(text, source)`; calls `on_pending` if set |
| `restore()` | repair path for reopen/attach: `loop.repair_open_turn(cause={"kind": "aborted", "by": "disposed"})` (`agent/loop.py:386-424` refactored public) |
| `wait_for_idle()` | awaits the in-flight task; no-op when idle |
| `on_pending(cb)` | single slot; TUI uses it to start a turn for a schedule/subagent notice |
| `last_assistant_text() -> str` | last folded assistant message text; finishes the bubble after an interrupt |
| `turn_count() -> int` | count of `turn.started`; cheap helper for `/status` and history |

`send` keeps raising `asyncio.CancelledError` on interrupt
(`native_host.py:526-529`); TUI contrast is in §5.
`usage()`/`context_tokens()` stay the
`_UsageTap`/`_ContextTap` values (`native_host.py:401-433`); the
TUI must pass a `TraceExportSink` in the host services, otherwise
`context_tokens()` remains 0.

### 2.6 `AgentLoop` additions (`agent/loop.py`)

1. `_tool_log` (currently a no-op, `agent/loop.py:737-748`) becomes
   `functools.partial(append_mapped, self._session)` so tools that write through
   `ctx.log` (`tools/builtin/shell.py:181` job records,
   `tools/builtin/goal.py:144,176,226,343` goal writes) persist native facts. This also
   fixes `/jobs` rows, which today are always empty because JobsService is built without a
   record callback (`builtin/jobs.py:29`, `jobs/manager.py:160-208,340-367`).
2. `run(prompt, *, resume=False, source=None, cancel=None)`;
   `_user_message` (`loop.py:836-845`) takes the source, defaulting to
   `UserSource()`. Schedule and subagent notices use `PluginSource` so
   `fold` keeps their origin.
3. `run_tool_call(..., hooks=...)`: pass
   `ServiceBag(self._services).get(HookService)` at `loop.py:726-729`. Today
   the native loop bypasses the tool pre/post hook pipeline
   (`tools/pipeline.py:464-509`), i.e. plugin tool policy/telemetry silently does not run.
4. `repair_open_turn(cause=None)`: public form of
   `_repair_unfinished_turn` (`loop.py:386-424`);
   `run(resume=True)` calls it, and `NativeAgentHost.restore()` calls it
   directly for sessions restored by team/subagent.
5. `_build_plan` (`loop.py:499-501`) keeps
   `system=view.system`; the fix is entirely on the host side (§4).

`agent/context_plan.py` gains one public helper `tool_specs(tools) -> tuple[ToolSpec, ...]`
so the prompt renderer uses the same tool descriptions as the model request.

---

## 3. Plugin replacement (area 1)

### 3.1 `SessionsPlugin` (rewrite `src/deeppyro/builtin/sessions.py`, 37 lines today)

Keep: plugin id `sessions`, provide `sessions`, inject
`["config"]`, module path in `bundles/core/bundle.toml:26`. Replace the
`SessionStore` body.

```python
class SessionsPlugin(Service):
    provide = "sessions"
    plugin_id = "sessions"
    version = "0.3.0"

    def __init__(self, root: Path) -> None: ...
    @property
    def root(self) -> Path: ...

    # create/read
    def create(self, request: NewSession) -> NativeSession: ...
    def open(self, session_id: str) -> NativeSession: ...   # raises NativeQueryError(NATIVE_QUERY_NOT_FOUND)
    def exists(self, session_id: str) -> bool: ...
    def read(self, session_id: str) -> SessionLog: ...      # read-only, no lock, raises on corrupt
    # catalogue (no locks)
    def look(self, session_id: str) -> NativeSessionInfo: ...
    def list(self, *, include_archived: bool = False, limit: int | None = None,
             offset: int = 0) -> list[NativeSessionInfo]: ...
    def latest(self) -> NativeSessionInfo | None: ...
    def transcript(self, session_id: str) -> NativeTranscript: ...
    # mutations that do not need the append lock
    def rename(self, session_id: str, title: str, *, source: str = "user") -> str: ...
    def archive(self, session_id: str, *, archived: bool = True) -> bool: ...
    # fork
    def fork(self, session_id: str, *, new_id: str | None = None) -> NativeSession: ...
```

Implementation mapping (all already exist, no new storage):

| method | delegate |
| --- | --- |
| `create` | `sessions_path_for(id, self.root)` (`session/schema.py:1252`) + `NativeSession.create` (`schema.py:447`) |
| `open` / `read` | `resolve_native_session_path` + `NativeSession.open` (`schema.py:493`) / `NativeCodec.read` |
| `look` / `list` / `latest` | `native_index.list_native_sessions` (`native_index.py:111-131`), `_read_info` (`:196`), `_session_info` (`:233`) |
| `transcript` | `read_native_transcript` (`native_index.py:134-152`) |
| `rename` / `archive` | `native_query.rename_native_session` / `archive_native_session` (`native_query.py:422,442`) |
| `fork` | `fork_native_session` (`session/native_fork.py:70`); caller closes the returned session |

Error types are the native ones (`SessionLockedError` `schema.py:1218-1227`,
`NativeQueryError`, `NativeForkError`). The plugin does **not** hide lock
contention: `open` of a session locked by another process raises
`SessionLockedError`; the TUI treats that as "read-only available" (§9.2).

`session/__init__.py` exports `NewSession`, `NativeSession`,
`NativeSessionInfo`; it stops exporting `Session`/`SessionStore`.

### 3.2 `AgentsPlugin` (rewrite `src/deeppyro/builtin/agents.py`, 280 lines today)

Keep: plugin id `agents`, provide `agents`, inject
`["config", "llm", "presets", "tools", "prompt"]`, module path in
`bundles/core/bundle.toml:188`, and the `options()` /
`preset()` / `on_created()` / `live_agents()` surface used by
tests.

Replace `create(*, session, provider, registry, options, sink, permission, cwd, services, preset,
preset_revision, service_access) -> AgentSession` (`builtin/agents.py:119-184`) with:

```python
class AgentsPlugin(Service):
    provide = "agents"
    plugin_id = "agents"
    version = "0.3.0"

    def options(self, **overrides) -> AgentOptions: ...          # keep, wire-free
    def preset(self, preset_id=None, revision=None) -> PresetSpec: ...

    def host(self, session: NativeSession, build: AgentBuild) -> NativeAgentHost: ...
    async def aclose(self) -> None: ...                          # closes live hosts
    def live_hosts(self) -> tuple[NativeAgentHost, ...]: ...
    def on_created(self, callback) -> Callable[[], None]: ...
```

`host()` recipe (each step is one existing helper):

1. `spec = self._presets.resolve(build.preset, revision=build.preset_revision)`;
   require materialized (`PresetsPlugin.is_materialized`
   `builtin/presets.py:450`); same error wording as today
   (`builtin/agents.py:127-131`) so `/preset` errors stay stable.
2. `model = build.model or spec.model or self._agent_config.model`;
   `registry = self._tools.registry_for(str(build.cwd), scope=spec.id, revision=spec.revision)`
   (`builtin/tools.py:81`).
3. Apply the existing filter order (`builtin/agents.py:251-280`): team-mode denied tool set
   (`TEAM_MODE_DISABLED_TOOLS`), then `spec.tools` allowlist, then
   `spec.tools_exclude`.
4. `provider = self._llm.native_provider(build.provider)`
   (`builtin/llm.py:61`); a `ConfigError` here is the honest "provider has no
   native transport" failure (`llm/registry.py:96`).
5. `prompt_source = PresetPromptSource(self._prompt, spec, build.prompt_facts)` if facts
   were given.
6. `hooks = build.hooks`, `budget = BudgetLimits(max_steps=build.max_steps or options.max_steps)`,
   `checkpoint_store = CheckpointStore()`, `trace_store = TraceStore()`
   (paths as in `headless.py:1033-1046`).
7. `services = {**build.services, "session": session, "tool_registry": registry}` so tools
   resolve the native session through `ctx.services["session"]` exactly like the native task
   tools already do (`builtin/task_tools.py:596-605`).
8. `host = NativeAgentHost(session=session, provider=provider, model=model, tools=tuple(registry),
   cwd=str(build.cwd), hooks=hooks, budget=budget, checkpoint_store=..., trace_store=...,
   temperature=build.temperature, max_tokens_per_call=build.max_tokens, provider_name=build.provider,
   permission=build.permission, services=services)`.
9. `host.attach_prompt(prompt_source, build.prompt_facts)` when facts exist; register weak
   host; fire `on_created`.

No PTC surface: if `spec.presentation == "ptc"` or the legacy host-wide
`PtcRuntime.mode == "ptc"`, raise
`PluginDependencyError("preset {id!r} requires the PTC runtime, which the native agent host does not provide yet")`.
This is deliberate; see §13 R1.

`AgentsPlugin.create_for_child(...)` is not added: team/subagent call
`host()` with their own `AgentBuild` (§7), because they need different hooks
(`None`), different services additions and a different recovery path.

### 3.3 TUI boot and resume

`TuiRuntime.boot` (`tui/runtime.py:60-130`) changes as follows:

```python
@classmethod
async def boot(cls, sink, ask, cwd=None, provider=None, model=None, config=None,
               resume: str | None = None) -> "TuiRuntime":
    cfg = load_config()
    workspace = resolve_workspace(cwd)                       # tui/runtime_support.py:19
    provider_name = provider or cfg.agent.provider
    model_name = _resolve_model(cfg, provider_name, model=model)
    kernel_app = await boot(cfg)                              # unchanged
    llm = kernel_app.require(LlmPlugin)
    sessions = kernel_app.require(SessionsPlugin)
    sandbox = kernel_app.require(SandboxPlugin)
    tools = kernel_app.require(ToolsPlugin)
    agents = kernel_app.require(AgentsPlugin)
    prompt = kernel_app.require(PromptPlugin)
    ...
    permission = sandbox.gate(workspace, ask=ask)             # unchanged (builtin/sandbox.py:65)
    runtime = cls(..., sessions=sessions, agents=agents, prompt=prompt, ...)
    await runtime.ensure_presets()
    runtime._configure_team(None); runtime._configure_subagent(None)
    if resume is None:
        await runtime.new_session()
    else:
        await runtime.open_session(resume)                    # gives host too
    return runtime
```

`DeepPyroApp.on_mount` (`tui/app.py:99-141`) passes a new
`resume` argument through `TuiRuntime.boot(...)`;
`tui/main.py` grows `--resume SESSION_ID` (it has none today; only
cwd/provider/model are forwarded at `tui/main.py:168-172`). The
`runtime_factory` test seam is unchanged.

Boot no longer requires `ProjectionsService`; `ActivityService`,
`JobsService`, `WorkingDirectoryService`,
`UserQuestionsService`, `SchedulePlugin` remain required.
`TraceStore` (or a `TraceExportSink` plugin) is added to the host
services so the context meter works (§5.4).

### 3.4 `tui/runtime_state.py` (54 lines) field changes

Drop `store` and `projections`. Add
`sessions: SessionsPlugin`, `agents: AgentsPlugin`,
`prompt: PromptPlugin`, `compactor: NativeCompactor`,
`trace_export: TraceExportSink | None`, `notices: list[tuple[str, str]]`.
Keep `agent_preset`, `preset_revision`, `session`,
`agent`, `cwd`, `registry`, `permission`,
`sink`, `ask`, `_created_sessions`,
`_restored_team_sessions`, `_schedule_once`, `_closed` —
the layer code depends on them.

---

## 4. System prompt and prompt context (area 2)

### 4.1 Who builds it

`PresetPromptSource` (§2.2) is the only implementation. `context()`
mirrors v4 `PromptLayer._prompt_context` (`agent/session_prompt.py:23-40`) but
takes `PromptFacts`:

```python
PromptContext(
    cwd=facts.cwd,
    platform=facts.platform or sys.platform,
    session_id=header.id,
    started_at=header.created_at,
    permission_preset=facts.permission_preset,
    approval_policy=facts.approval_policy,
    model=facts.model,
    sandbox_policy_text=facts.sandbox_policy_text,
    approval_policy_text=facts.approval_policy_text,
    instructions=[*collect_instructions(facts.cwd), *spec.instructions],
    env=dict(facts.env),
    direct_tool_names=tuple(tool.name for tool in tools),
)
```

`render()` is `prompt_service.build(tools, context, scope=spec.id,
revision=spec.revision, include=spec.prompt_sections)`
(`builtin/prompt_service.py:92`) — exactly the call today's `AgentsPlugin`
makes (`builtin/agents.py:150`). Preset sections come from
`PromptPlugin.add_section` (`builtin/prompt_service.py:24`); core sections
from `prompt/prompt.py:185` (`CORE_PROMPT_SECTIONS`).

The TUI only fills facts (`TuiRuntime._prompt_facts()`):
`cwd = self.working_directory.get(self.session)` fallback `header.cwd`;
`model = self.model`; `permission_preset`/`approval_policy`
from the bound `PermissionGate`; policy texts from
`deeppyro.sandbox.policy.sandbox_policy_text` /
`approval_policy_text` (same as `agent/session_prompt.py:127-140`).

### 4.2 Where the system message is persisted

`NativeAgentHost.refresh_prompt()` appends a schema-1 **fact**:

```python
Message(role="system", content=(TextPart(text=rendered),), source=SystemSource(),
        id=new_message_id(), created_at=now_ms())
# EventKind.SYSTEM_MESSAGE, data={"message": message.to_dict()}
```

`fold` treats it as the per-session system block, last-wins, and keeps it **out of the
message sequence** (`session/fold.py:238-240` sets `SessionView.system` and
returns). This is the only writer of `EventKind.SYSTEM_MESSAGE` in the codebase today
(definition `session/schema.py:111`, fold consumer, `session/native_query.py`
role map, migration script); the native loop then naturally sends it because
`_build_plan` passes `view.system` (`agent/loop.py:499-501`).

When it is written:

* host construction (`_attach`, session create/open) — so a fresh session never takes an
  un-prompted turn;
* before every `send()`; if render is unchanged the fold comparison short-circuits and
  nothing is written (no log growth per turn);
* `_rebuild_host()` after `/preset`, `/model`,
  `/permission`, `/cwd` (these change facts; the host is recreated
  anyway, so this is the construction write);
* never mid-turn (a running `AgentLoop` captured `view.system` at
  `_build_plan` time; changing it midway would be a lie in the log).

Superseded prompts stay in the log as facts (history of what the model saw). Replay of a run therefore
uses the prompt snapshot in effect for that turn, because `fold` at that prefix yields it.
This is the AI-native property v4's `SystemPromptProjection`
(`prompt/projection.py`, `in_history=True`, replace-head logic) tried to
approximate; here it falls out of the event log.

### 4.3 Preset / instructions / runtime context

* Preset identity: `system.message` facts are rendered from the preset captured at host
  build; the preset fact `preset.selected` (`PRESET_SELECTED`) records the
  selection. A preset switch mid-session rebuilds the host and appends a new prompt snapshot; the header
  `preset` field is immutable (§6.3).
* Instructions: `AGENTS.md` is collected at render time from the effective cwd, so a
  `/cwd` change and host rebuild produce a new snapshot.
* Runtime context: v4 committed a synthetic `user/message` every series with the same
  environment facts (`agent/session_prompt.py:83-91`, `prompt/projection.py`
  `RuntimeContextProjection`). **Dropped.** The environment lives in the system prompt and
  changes are `workspace.cwd_changed` facts; a second copy per turn bought nothing and
  polluted the message sequence. `prompt/projection.py` and its exports become dead in P6.

### 4.4 How compaction sees it

The summarizer request is built from `view.messages` — system is not a message, so it is
never "shadowed" and never summarized. The compactor must also explicitly refuse to cover it; native
message refs cannot address the system snapshot at all (refs enumerate `view.messages`),
which removes v4's `COMPACT_SYSTEM_PROMPT` check by construction (§8.3).

### 4.5 How replay sees it

`fold(header, events)` is pure; `tests/native/replay.py` already replays
native logs. Replaying a turn means: fold up to `run.started` for the turn, read
`view.system` + `view.messages`, and compare with the trace's request hash
(the loop hashes prompt/messages into the trace). The prompt snapshot being a log fact is what makes
this check possible at all.

---

## 5. `TuiSink` ↔ `AgentHooks` adapter (area 3)

### 5.1 New module `src/deeppyro/tui/native_hooks.py`

`AgentHooks` (`agent/events.py`) carries no errors and no usage, and
`NativeAgentHost.send` returns a `RunResult` or raises. So the adapter
only maps stream events; the host/turn layer synthesizes the rest.
`headless.py:939-970` (`_NativeSinkHooks`) is the proof this works; the TUI
adapter is the same shape plus turn/step counters, and the TUI `TurnLayer` supplies
final/usage/error/status like `headless.py:1001-1086` does.

```python
class TuiAgentHooks(NullAgentHooks):
    def __init__(self, sink: TuiSink) -> None:
        self._sink = sink
        self._turn = 0
        self._step = 0

    def run_started(self, event):      self._sink.emit({"type": "status", "phase": "thinking"})
    def turn_started(self, event):     self._turn += 1; self._step = 0
    def step_started(self, event):
        self._step += 1
        self._sink.emit({"type": "step", "turn": event.turn or self._turn, "step": event.step})
    def text_delta(self, event):       self._sink.emit({"type": "text", "text": event.text})
    def reasoning_delta(self, event):  self._sink.emit({"type": "thinking", "text": event.text})
    def tool_call_started(self, event):
        self._sink.emit({"type": "tool_call", "callId": event.call_id,
                         "name": event.name, "arguments": event.arguments})
    def tool_call_finished(self, event):
        self._sink.emit({"type": "tool_result", "callId": event.call_id,
                         "isError": event.result.is_error, "text": event.result.text})
    # run_finished / checkpoint_written / assistant_message / budget_exceeded: no-op
    # (TurnLayer emits final/usage/status and the error path; budget_exceeded is followed by a raise)
```

### 5.2 Complete mapping table (what the TUI consumes today, and its native source)

| TUI event (`tui/app_events.py:16-51`, `tui/sink.py`) | native source | notes |
| --- | --- | --- |
| `text` | `hooks.text_delta` (`agent/loop.py` `_model_call` → `on_text`) | streamed assistant text |
| `thinking` | `hooks.reasoning_delta` | rendered by `ConversationView.stream_thinking` |
| `step` | `hooks.step_started` | `begin_step(turn, step)`; counters in the adapter |
| `tool_call` | `hooks.tool_call_started` | `callId`, `name`, `arguments` unchanged |
| `tool_result` | `hooks.tool_call_finished` | needs `isError` + `text`; v4 had no `name` here either |
| `usage` | `TurnLayer` from `RunResult.usage`/`.cached_input_tokens` | `{"input","output","cached"}` — exactly `TuiSink.last_usage` shape (`tui/sink.py:20-25`) |
| `final` | `TurnLayer` from `RunResult.final_text`; on interrupt `host.last_assistant_text()` | required for `finish_turn`; v4 emitted it at `session_turn.py:362` |
| `error` | `TurnLayer` exception mapping: `ProviderStreamError`/`BudgetExceeded`/other | v4 also used this event for max-token truncation (`session_turn.py:296-310`); native `finish_reason` is on `RunResult`, so map `"length"` to an `error` event with the same truncation wording, then `final` |
| `status` | `run_started` → `thinking`; `TurnLayer` → `done` / `interrupted` | `_phase` drives the status bar only |
| `session` | `TuiRuntime._attach` emits `{"type":"session","sessionId":...}` for slot consumers | matches headless `headless.py:1027` |
| notices (working-directory, schedule) | TUI callbacks, not session events (see §5.3) | content is UX-only |
| interrupt | `TurnLayer` catches `asyncio.CancelledError` (`native_host.py:526-529`) | no event; bubble closed with partial text |

`TuiSink` itself does not change: it still keys on `payload["type"]` and sets
`saw_error`, `final_text`, `last_usage`
(`tui/sink.py:3-46`). Everything downstream (`app_events.handle_event`,
`slots.apply_conversation_slots`, `PluginStatusBar`) keeps its contract;
this is why the widget layer is untouched.

### 5.3 `TurnLayer` rewrite (`tui/app_turns.py:35-47, 85-91`)

```python
async def _run_turn(self, text: str, *, source=None, resume: bool = False) -> None:
    self._sink.begin_turn()
    try:
        if resume:
            result = await self.runtime.agent.resume()
        else:
            result = await self.runtime.agent.send(text, source=source)
    except asyncio.CancelledError:
        partial = self.runtime.agent.last_assistant_text()
        self._sink.emit({"type": "final", "text": partial})
        self._sink.emit({"type": "status", "phase": "interrupted"})
    except (BudgetExceeded, ProviderStreamError) as exc:
        self._sink.emit({"type": "error", "message": str(exc)})
    except Exception as exc:
        self._sink.emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    else:
        self._sink.emit({"type": "final", "text": result.final_text})
        self._sink.emit({"type": "usage", "input": result.usage.input_tokens,
                         "output": result.usage.output_tokens, "cached": result.cached_input_tokens})
        self._sink.emit({"type": "status", "phase": "done"})
        if result.finish_reason == "length":
            self._sink.emit({"type": "error", "message": _truncation_message(...)})
    finally:
        ... schedule queue / idle handling as today (tui/app_turns.py:39-47)
```

`_interrupt_turn` (`tui/app_turns.py:85-91`) becomes
`interrupted = await self.runtime.agent.interrupt()` and notifies
`"interrupt requested"` only when true, `"nothing to interrupt"` otherwise
(host returns `bool`, `native_host.py:534-546`; v4
`AgentSession.interrupt` always returned a value and the UI lied on idle).

Notices: v4 delivered working-directory and schedule notices through `session.inject`,
which landed in the v4 in-memory queue and later became model-visible messages. Native design splits the
two concerns: the **fact** is the event (`workspace.cwd_changed`; schedule delivery
appends a `user.message` with `PluginSource(kind="schedule", taskId=...)`),
the **notice** is a TUI callback (`runtime.configure_schedule_notice` stays,
`tui/runtime_services.py:32`; a new
`configure_working_directory_notice` feeds `ConversationView.add_info`).
Model visibility comes from the prompt refresh (§4.2), not from a synthetic user message. Documented as a
deliberate semantic change (§12.9).

### 5.4 Usage and context meter wiring

* `runtime.session_usage()` → `self.agent.usage()` returning
  `{"input","output","cached"}`; the `_UsageTap` accumulates
  `TurnUsage` from the loop's `UsageSink`
  (`native_host.py:401-415`), including aborted turns.
* `runtime.context_tokens()` → `self.agent.context_tokens()`; the host's
  `_ContextTap` sets it from the latest `ModelCallRecord`
  (`native_host.py:415-433`). **Requirement:** the services bag handed to
  `AgentsPlugin.host` must contain a `trace_export` object;
  `TuiRuntime` constructs `TraceStore`
  (`observability/trace.py:435`) or consumes a plugin-provided
  `TraceExportSink` (`observability/protocols.py:57-66`) and merges it into
  `_plugin_services()` (today `tui/runtime_schedule.py:147-159`). Without
  it the TUI context meter shows 0 — a regression versus v4
  (`runtime_status.py:106-114`).
* Token estimation for threshold decisions is `compact/tokens.py` ported to
  `core.messages` (§8.1); estimates never go into the session log.

---


## 6. Session layer rewrite (area 4)

### 6.1 `tui/runtime_session.py` (273 lines) — old vs new

```text
new_session()                65-77   ->  create native session + facts + host, no store/projections
_attach(session)             79-109  ->  _attach(handle) builds host through AgentsPlugin.host()
_save_projections           111-126  ->  deleted (no sidecar; facts are in the log)
_restore_projections        128-138  ->  deleted
_configure_working_directory 140-157 ->  bind(handle, record=handle.append, inject_notice=notice)
_configure_team             159-182  ->  same call shape, native TeamService.configure
_configure_subagent         184-210  ->  same, native SubagentService.configure
_restore_team               212-235  ->  same guard, native fold-based restore
_record_preset              254-263  ->  append_native(PRESET_SELECTED, native payload)
_record_model_selection     265-267  ->  append_native(MODEL_SELECTED, {provider, model})
```

```python
async def new_session(self) -> NativeSessionHandle:
    await self._detach()                                  # closes host, unbinds services
    preset = self._canonical_preset(self.agent_preset) or DEFAULT_PRESET_ID
    await self._ensure_preset(preset, self.preset_revision)
    session = self.sessions.create(NewSession(
        cwd=self.cwd, preset=preset, origin="user",
        session_id=None, created_at=None))
    handle = NativeSessionHandle(session)
    self._pin_permission_native(handle)                   # sandbox.mode + permission.policy facts
    await self._session_created(handle, cwd=self.cwd, preset=preset)   # hook service event
    await self._attach(handle)
    self._record_model_selection()
    self._record_preset()
    return handle

async def _attach(self, handle: NativeSessionHandle) -> None:
    await self._detach()
    preset = handle.fold().state.preset.preset or handle.header.preset
    revision = handle.fold().state.preset.revision
    build = AgentBuild(
        provider=self.provider_name, model=self.model, cwd=self.cwd,
        preset=preset, preset_revision=revision,
        permission=self.permission, hooks=self._hooks,
        prompt_facts=self._prompt_facts(handle),
        services=self._plugin_services(),
        context_window=self.context_window)
    host = self.agents.host(session=handle.native_session, build=build)
    self.agent, self.session = host, handle
    self.preset_revision = revision
    self._configure_team(handle); self._configure_subagent(handle)
    self._configure_working_directory(handle)
    if self.agent_mode == AGENT_MODE_TEAM:
        await self._restore_team()
    self._emit_session_event(handle)                      # {"type":"session",...} for slots
```

`_hooks` is a `TuiAgentHooks(self.sink)` built in
`TuiRuntime.__post_init__` (§5.1). `_detach()` is new:
`await self.agent.aclose()`, `working_directory.unbind(self.session)`,
`team.close()`, `subagent.close()`, then clears
`self.agent`/`self.session`. It replaces the
`_save_projections` pair and the old-agent `aclose` inside
`_attach` (`runtime_session.py:100-104`).

`_dispose_sessions` / `close()`
(`tui/runtime_preset.py:49-55`, `tui/runtime.py:321-351`) become
`await self._detach()` plus `await self.agents.aclose()`; the
`SESSION_DISPOSED` hook event stays for plugins that want it.

### 6.2 `_configure_working_directory`

```python
def _configure_working_directory(self, handle: NativeSessionHandle) -> None:
    state = handle.fold().state
    self.working_directory.bind(
        handle,
        cwd=Path(state.cwd) if state.cwd else Path(handle.header.cwd),
        record=handle.append,                              # RecordFn(str, dict) -> Any
        inject_notice=self._working_directory_notice,
    )
```

Signature compatibility is verified: `bind(session, *, cwd=None, record=None,
inject_notice=None, recovered=False)` `working_directory/service.py:153`,
`RecordFn = Callable[[str, dict[str, Any]], Any]` and
`NoticeFn = Callable[[str], Any]` `working_directory/types.py`. The
service keys state by `session.session_id`, which the handle has
(`native_host.py:257`). `handle.append` maps
`"working-directory/change"` to `workspace.cwd_changed` with an absolute
path payload (`native_host.py:196-199`, schema validator
`session/schema.py:763`), so no service change is needed. This also fixes a latent v4
bug: the v4 runtime passed `session.inject` on a `Session` object that has
no such method (`runtime_session.py:156`); a `cd` would have raised.

The notice callback is UX-only (§5.3): `ConversationView.add_info("working directory: ...")`.

### 6.3 `open_session` (resume) and the header snapshot check

`runtime_model.py:18-62` rewritten:

```python
async def open_session(self, session_id: str) -> None:
    session = self.sessions.open(session_id)              # may raise SessionLockedError
    handle = NativeSessionHandle(session)
    view = handle.fold()
    self.model = view.state.model.model if view.state.model else self.model
    self.provider_name = _provider_for(view.state.model, self.provider_name)
    self.agent_preset = view.state.preset.preset
    self.preset_revision = view.state.preset.revision
    await self.ensure_presets()
    await self._attach(handle)
```

`tui/runtime_support.py:63-101` `_recorded_preset` and
`_recorded_model_selection` currently scan `session.records` for
`"agent-preset/selected"` / `"model/selection"`; they become one-liners
over `fold(handle.header, handle.events).state.preset` /
`.state.model`. Keep the functions (they are imported by tests) but change the body.

`_session_header_snapshot` / `_assert_session_header_unchanged`
(`runtime_session.py:17-59`) are **deleted** in the native path: a schema-1 header is
written once at create (`session/schema.py:447-491`) and no API can rewrite it; effective
cwd/model/preset live in fold state. While the blue/green phase still runs v4 sessions, the check stays
inside the v4 branch only.

### 6.4 `tui/runtime_status.py` (144 lines)

| member | native implementation |
| --- | --- |
| `session_id` (91-93) | `self.agent.session_id` |
| `session_usage` (95-104) | `self.agent.usage()` -> `{"input","output","cached"}` |
| `context_tokens` (106-114) | `self.agent.context_tokens()`; falls back to `usage()["input"]` |
| `recent_sessions` (116-118) | `self.sessions.list(include_archived=False, limit=limit)` |
| `native_sessions` (120-128) | **removed**; one catalogue (§9) — keep the method as a deprecated alias for one release, returning `recent_sessions` |
| `archived_sessions` (130-133) | `[i for i in self.sessions.list(include_archived=True) if i.archived]` |
| `known_workspaces` (69-82) | `{i.cwd for i in self.sessions.list()}` |
| `set_workspace` (51-67) | same, then `handle.append_native(WORKSPACE_CWD_CHANGED, {"cwd": str(workspace)})` + `await self._attach(handle)` |
| `compact_now` (135-144) | `await self.compactor.compact(self.session)` (§8.2) |
| `permission_preset` (32-41) | derived natively (§7.0) |

### 6.5 `tui/runtime_model.py`, `runtime_preset.py`,
`runtime_catalog.py`

* `_build_provider` (`runtime_model.py:79-89`) returns
  `llm.native_provider(name)`; `_activate_provider` (91-98) is deleted
  (the host holds the provider; there is no v4 `LlmProvider` object in the TUI process).
* `set_model`/`set_provider` (`runtime.py:253-300`):
  update fields, append `model.selected` fact through the **old** handle, then
  `@ await self._attach(self.session)` (rebuild host so the new model applies).
  `_persist_selection` (`runtime.py:302`) keeps writing config; no session write.
* `set_agent_preset` (`runtime_preset.py:57-80`): resolve + ensure, append
  `preset.selected`, rebuild host. The old rollback path
  (`_switch_preset` 89-116) simplifies to "rebuild failed -> restore previous fields and
  rebuild again".
* `agent_presentation` (`runtime_preset.py:24-32`): native always returns
  `"native"`; a `"ptc"` preset raises at preset switch time (§3.2 step 9).

### 6.6 `tui/runtime_schedule.py` (181 lines) — schedule sessions

| function | native implementation |
| --- | --- |
| `create_schedule_session` (63-78) | `sessions.create(NewSession(cwd=spec.cwd or runtime.cwd, preset=preset, origin="headless"))` + `_pin_permission_native` + `_session_created` |
| `create_schedule_agent` (80-94) | `agents.host(session.native_session, AgentBuild(..., hooks=None, services=self._plugin_services()))`; no sink object needed |
| `track_schedule_agent` / `_finish_schedule_agent` (96-114) | `await agent.wait_for_idle(); await agent.aclose()` — the v4 `session.wait_for_idle()` call goes away |
| `_registry_for_preset` (116-131) | deleted; registry is built inside `AgentsPlugin.host` |
| `_ensure_preset` / `_canonical_preset` | unchanged |
| `_plugin_services` (147-159) | same mapping plus `"trace_export"` and (during blue/green) the v4 contracts |

The schedule host (`tui/runtime_schedule_host.py`) keeps its protocol: `session_exists`
(41-51) becomes `sessions.exists(session_id)`; `delegation_depth` (53-55) stays
`0`; `deliver` (56-63) forwards to
`_deliver_to_session` / `_deliver_to_new`, which now use
`agent.deliver(message, source={"kind": "schedule", "taskId": ..., "occurrence": ...})`
instead of `session.followup`. Live detection is
`session_id == runtime.session_id` (same as today, line 67).

### 6.7 Other TUI runtime files

* `runtime_services.py`: `open_schedule_session` (54-56) ->
  `self.sessions.open`; `_session_created` (68-73) keeps the
  `SESSION_CREATED` hook event with `{"sessionId","cwd","preset"}`;
  `_pin_permission` (84-92) becomes `_pin_permission_native(handle)` which
  appends `sandbox.mode` + `permission.policy` (no
  `permission/preset` event exists in schema 1).
* `runtime_support.py`: `_recorded_preset`/`_recorded_model_selection`
  read `handle.fold()` (§6.3); `_record_field`-style v4 attribute access in
  `app_support.py:29-34` is not needed for handle state.
* `runtime.py`: drop `store`/`projections`/`agents.create`
  wiring; add `prompt`, `compactor`, `trace_export`;
  `close()` (321-351) stops schedule, detaches, `await self.agents.aclose()`,
  cancels jobs, stops the kernel.
* `tui/slots.py` `build_slot_context` (721-729) is untouched: the runtime
  facade keeps `session_usage`/`context_tokens`/`session_id`/
  `provider_name`/`model`/`cwd`.

`tui/history.py` (354 lines) is v4-record based; new
`tui/native_history.py` folds instead:

| v4 reader | native source |
| --- | --- |
| `reasoning_from_session` (114), `reasoning_map` (164) | fold `assistant.message` -> `ReasoningPart` per step |
| `processes_from_session` (260), `process_map` (334) | fold `tool.call`/`tool.result` pairs -> `ProcessEntry` |
| `is_ptc_mode_message` (83) | deleted; native has no PTC follow-up messages |

Consumers switch imports: `app.py:189-222` (`/thinking`) and
`app_sessions.py:46-80` (`_render_history`) import from
`tui/native_history.py` in P4.

---

## 7. Team, subagent and team restore (area 5)

### 7.0 Shared prerequisite: native branches for v4-record services

Five services must learn to read native sessions (fold) and write native events (append_native) before
team/subagent/compaction can run. This is one small adapter pattern, not five rewrites.

| service | file:lines | native change |
| --- | --- | --- |
| `SandboxPolicyService` | `sandbox/policy.py:68-86` | `last_mode` reads `fold.state.sandbox_mode`; `set_mode` appends `sandbox.mode` |
| `ApprovalService` | `sandbox/approval.py:71-128` | `last_policy` reads fold `state.permission_policy`; `request` appends `approval.requested` / `approval.decided` (schema shapes, `session/schema.py:695-715`) |
| `PermissionPresetsService` | `sandbox/permission.py:73-131` | `derive` from fold state; `apply` appends `sandbox.mode` + `permission.policy`; `pin_initial` writes those two facts |
| `ActivityService` | `builtin/activity.py:145-246` | native branch: todos/plan_mode from fold state; team/subagent rows from `plugin.team.*`/`plugin.subagent.*` events |
| `GoalService` | `goal/service.py:68-72,81-97,390,486` | native branch: `view` from fold `state.goal`/`rounds_started`; `_change_payload` emits snake_case goal payload for `goal.changed` |

Mechanics: every service today starts with `records = session.records`. Add one guard:

```python
def _native_view(session) -> SessionView | None:
    events = getattr(session, "events", None)
    header = getattr(session, "header", None)
    if events is None or header is None or not hasattr(session, "fold"):
        return None
    return session.fold()
```

Tools obtain the handle via `ctx.services["session"]` (the loop already exposes it,
`agent/loop.py:737-748`). Keep `ctx.log` working: after
`_tool_log = partial(append_mapped, session)` (§2.6) goal/job writes land as native events.
The plugin goal tool (`tools/builtin/goal.py:144,176,226,343`) then works unchanged.

`goal/round_driver.py` imports v4 `Message`/`SessionHeader`
(`:54-56`) and is mounted from the bundle (`bundles/core/bundle.toml:52`):
P4 ports it to `core.messages` + native host, or the bundle entry is dropped with a
documented capability loss (goal rounds run only through /goal).

### 7.1 `TeamService` (`src/deeppyro/team/manager.py`, 1135 lines)

```text
_configure/_Member dataclass          keep fields; session/agent become NativeSessionHandle/NativeAgentHost
_create_child_session  536-557        -> sessions.create(NewSession(cwd, preset=..., origin="subagent",
                                          parent_id=self.parent_session_id, delegation_depth=1))
_create_agent          618-648        -> agents.host(child.native_session, AgentBuild(hooks=None, ...))
_MemberSink            107-116        -> deleted (hooks=None; child stream stays out of the TUI)
_record_member         724-752        -> append_native(plugin.team.member, {"version":1,"team_id":...,"member":...})
_record_message        754-765        -> append_native(plugin.team.message, {"member_id":...,"text":...,"state":"queued"|"delivered"})
_close_open_turn       501-520        -> host.restore() (loop.repair_open_turn; no open_turn_closers)
_open_member_session   494-499        -> sessions.open(member.session_id)
_restore_member        427-450        -> open child session + create host + host.restore()
_fold_team_records     1037-1081      -> fold plugin.team.* events (new helper, no LogRecord)
_measure_usage         802 / _cumulative_usage 795 / _record_usage 819-854 -> deleted (usage never journaled to sessions)
_report_usage          856-874        -> sum live member host.usage() and report through UsageSink
team task board        877-1035       -> append_native(plugin.team.task, {"task": task.to_dict()})
_cancel/close          359-396        -> cancel tasks via TaskGroup, then host.aclose()
```

`configure` (`team/manager.py:204-236`) loses `registry` and
`sessions` service object gains `sessions` (the plugin);
`bind_session(handle)` stores the handle. The plugin wiring
(`builtin/team.py` 8-30) changes to inject
`["agents", "sessions", "sandbox", "tools"]` and construct
`TeamService(agents, sessions, sandbox)` — same shape; only the two service types change.

Team member usage propagation, honestly: child token usage is **not** copied into the parent log (schema 1
has no team usage event; schema §8.5 forbids it). It is reported to the `UsageSink` with
`source="subagent"` and `session_id=child.session_id`
(`observability/protocols.py:22-52`), so traces/dashboards see it. For members restored
from a previous process the host starts with zero counters; the leader's `/team` panel has
no usage column (`tui/screens/team.py:71` `MEMBER_COLUMN_KEYS`), so the
visible regression is limited to `member.last_reply`-free usage that never existed in the
panel. `docs/native/schema.md` §8.5 is the authority here.

### 7.2 `SubagentService` (`src/deeppyro/subagent/manager.py`, 826 lines)

```text
_configure                    157-184   keep; no registry (agents.host builds it)
_create_child_session         557-578   -> sessions.create(NewSession(cwd, origin="subagent",
                                            parent_id=parent, delegation_depth=parent_depth+1))
_create_fork_session          580-627   -> sessions.fork(parent_id)  (native_fork: full copy,
                                            origin "fork", depth+1); NO record copying / turn closers
_create_agent                 661-692   -> agents.host(child.native_session, AgentBuild(hooks=None, ...))
_ChildSink                     85-99     -> deleted (hooks=None)
_append_record                706-724   -> append_native on parent (plugin.subagent.*)
_record_subagent              727-763   -> append_native(plugin.subagent.descriptor, payload)
_notify_parent                764-808   -> parent_host.deliver(notice, source=PluginSource(
                                            {"kind": "subagent", "childSessionId": child_id}))
_publish_usage                434-449   -> UsageSink only; no parent session write
_measure/_child_records       401-433   -> deleted
_publish_usage_fallback       472      -> deleted
cancel_all/close/wait_children 491-530 -> TaskGroup + host.aclose()
```

`_ensure_ready` (`subagent/manager.py:532-541`) keeps its two errors but
checks the new types; `builtin/subagent.py` 8-30 wiring is unchanged apart from
the service types.

### 7.3 Parent/child native sessions — invariants

* Child header: `parent_id` = parent id, `delegation_depth` =
  parent depth + 1, `origin` in `{subagent, fork}`
  (`session/schema.py:67` `ORIGINS`). The native index groups children by
  `parent_id`; `TuiScheduleHost.delegation_depth` returns 0 and is only
  used for schedule targets, unchanged.
* Concurrency: two processes cannot append to the same child; the file lock
  (`session/schema.py:1214-1239`) enforces it. A team member restoring a child while
  another process holds it surfaces `SessionLockedError`; the member goes to
  `phase=failed` with a message (same UX as v4's restore failure).
* Team restore walkthrough (replacing `restore` 398-425):
  1. fold parent; collect `plugin.team.member` / `plugin.team.message` /
     `plugin.team.task` events;
  2. for each member: `sessions.open` -> host -> `host.restore()` -> phase
     `restored`;
  3. replay queued messages in order via `host.deliver(...)` (one turn each);
  4. rebuild team id from the first event, unchanged.
* `_restored_team_sessions` guard (`runtime_session.py:212-235`) is kept as
  is: it prevents a second restore on the same run.
* Cross-process safety for fork: `fork_native_session` reads the source completely, so a
  concurrent writer yields a consistent prefix error (schema requires contiguous seqs); the manager maps
  `NativeForkError` to `SubagentError`.

---

## 8. Compaction (area 6)

### 8.1 What must be ported

| module | v4 coupling | native replacement |
| --- | --- | --- |
| `compact/tokens.py` (45) | imports `core.wire.Message`, part type `"tool-call"` | port to `core.messages`, part type `"tool_call"`; same constants `CHARS_PER_TOKEN=3.5`, `MESSAGE_OVERHEAD=4` |
| `compact/compactor.py` (279) | v4 records + `LlmProvider`/`LlmRequest` | new `compact/native_compactor.py` (§8.2) |
| `compact/compress_tool.py` (616) | v4 records, surface, refs, lifecycle | new `compact/native_compress.py` (§8.3) |
| `compact/surface.py` (313) | v4 `LogRecord` surface folding | **deleted**; schema-1 fold already is the surface (`session/fold.py`) |
| `compact/refs.py` (317) | `(seq, Message)` list + `LogRecord` | new `compact/native_refs.py` keyed by message id/ordinal |
| `compact/blocks.py` | `core.wire.Message` | keep sidecar store, type the summary payload with `core.messages.Message` |
| `compact/prompts.py` | wire-free | unchanged |
| `compact/__init__.py` (59) | re-exports | re-export new names; drop surface exports |

Message conversion is nearly identity: native `Message` and
`frame_summary` outputs are already `core.messages` parts. The only
real conversion is the **summary framing text**: `frame_summary` returns
`[{"type": "text", "text": ...}]` dicts; the native compactor wraps them with
`part_from_dict` (`core/messages.py:317`) into a
`Message(role="user", source=CompactionSource(compaction_id=...))`.

### 8.2 `NativeCompactor` algorithm

```python
class NativeCompactor:
    def __init__(self, provider: LlmProvider, model: str, *,
                 keep_recent_tokens: int = 8000,
                 context_window: int = 32_768,
                 threshold: float = 0.85,
                 max_tokens: int | None = None) -> None: ...

    def should_compact(self, messages: Sequence[Message]) -> bool: ...   # tokens > threshold*window

    async def compact(self, handle: NativeSessionHandle) -> bool:
        view = handle.fold()
        if not view.messages:
            return False
        start = _region_start(view.messages, self._keep_recent_tokens)   # prefix split
        if start <= 0:
            return False
        prefix = list(view.messages[:start])
        shadowed = view.messages[start:]
        request = ModelRequest(
            provider=handle.provider_name, model=self._model,
            messages=tuple([*prefix, _instruction_message()]),
            system=view.system, tools=(),            # summarizer sees the system snapshot
            max_tokens=self._max_tokens, session_id=handle.session_id)
        response = await complete_model_call(self._provider, request)
        summary = _summary_text(response)                        # raises LlmError like v4
        if estimate_messages_tokens([_framed(summary)]) >= estimate_messages_tokens(shadowed):
            raise LlmError("summary is not smaller than the shadowed content ...")
        compaction_id = new_compaction_id()
        message = Message(role="user", id=new_message_id(),
                          content=tuple(part_from_dict(p) for p in frame_summary(summary)),
                          source=CompactionSource(compaction_id=compaction_id),
                          created_at=now_ms())
        handle.append_native(EventKind.COMPACTION_DONE, {
            "compaction_id": compaction_id,
            "covers": [m.id for m in shadowed],
            "message": message.to_dict(),
        })
        self._trace.append(_model_call_record(response))         # usage/latency: trace only
        return True
```

Key differences from v4 and why:

* **One event, not three.** `compaction.done` (schema validator
  `session/schema.py:723-750`) carries the summary and the exact covered ids; fold
  removes them and inserts the summary (`session/fold.py:374-420`). No
  `compaction/start`/`end` lifecycle, so no "in progress" state to
  clean up after a crash. The TUI forbids concurrent compaction because the host is busy.
* **No surface re-check.** v4 folded `LogRecord` lists twice and aborted on a changed
  surface (`compact/compactor.py:134-160`). Native fold is the surface; the append is
  validated by `validate_event_data` + fold before it is written, and a stale region
  would fail fold's contiguity check (`session/fold.py:374-420`), which surfaces as
  `MessageRefError`; the compactor catches and reports "the session changed while
  summarizing; compaction aborted".
* **Usage never in the log.** `ModelCallRecord` (`observability/trace.py:240`)
  via the trace store; schema §8.5.
* **System prompt is summarized-adjacent but not coverable**: `covers` lists message ids;
  the system message has an id but is not in `view.messages`, and fold rejects covers
  referencing unknown ids. v4's `COMPACT_SYSTEM_PROMPT` check disappears.

`/compact` command wiring: `runtime_status.compact_now` ->
  `self.compactor.compact(self.session)` in a worker group; on `LlmError`
  the TUI shows `conversation.add_error`; success triggers
  `await self.runtime._attach(self.runtime.session)` (host rebuild re-folds the new
  summary). Auto-compaction: v4 ran it inside the agent turn; native loop does it **nowhere today**.
  Decision D4: 0.3.0 ships manual `/compact` plus a pre-send check in
  `TurnLayer._run_turn` (if `compactor.should_compact(view.messages)` ->
  compact before `send`), behind one config flag; the loop does not auto-compact.

### 8.3 `compress` tool, native

Surface kept: user writes a message with `m00012–m00030 title` headers (parser
`parse_content` `compact/compress_tool.py:99` stays as is — it is
pure text). What changes is addressing and validation:

| v4 | native |
| --- | --- |
| refs resolve to `(event seq, Message)` on folded surface (`refs.addressable_messages:248`) | refs resolve to `view.messages` ordinals (excluding system by construction) and freeze `(ref, message_id)` in a sidecar `RefStore` (rebuildable cache) |
| `tool/call` seq spans | call ids from `ToolCallPart` / tool messages in `view.messages`; reject ranges that cut a call/result pair (same code `COMPACT_SPLITS_TOOL_PAIR`) |
| compaction lifecycle records | one `compaction.done` per block, covers = message ids |
| block metadata (title/range) in `compaction/summary` event | sidecar `BlockStore` JSONL (cache; not part of the session) |
| `session.records` required | `ctx.services["session"]` (NativeSession) -> `handle.fold()` |

The `_resolve_session` helper (`compress_tool.py:571-616`) resolves the
native session the same way, then wraps it in an in-process `NativeSessionHandle(session)`
for append/fold. Failure semantics: validate everything **before** the first append; a single append is
atomic, so `COMPACT_PARTIAL_WRITE` cannot happen anymore (one `compaction.done`
per block means a multi-block command may still commit earlier blocks — document that as "each block is
atomic"; 0.3.0 keeps it: a later block failing leaves earlier blocks committed and the tool result lists
which blocks committed, matching today's partial-write UX).

`decompress`/`search_context` tools use `BlockStore` only
(no session access) and are unchanged.

---

## 9. `/sessions` and `/resume` (area 7)

### 9.1 One catalogue

`tui/app_sessions.py` currently merges two lists
(`_merged_sessions` 139-148; `_has_native` 124;
`_session_stamp` 116; `_native_sessions` 127;
`_listing_rows` 150-177). Native: one list from
`runtime.recent_sessions()` -> `sessions.list(include_archived=...)`;
`session_view.listing_lines` / `picker_items` operate on
`NativeSessionInfo` (`session/native_index.py:51-72`:
`id, path, cwd, created_at, updated_at, title, turns, event_count, preset, parent_id, origin,
preview, readable, read_only, archived`). `_session_stamp` and
`_merged_sessions` are deleted; `_has_native` always true.

`/sessions --all` (`app_sessions.py:283-304`) lists archived too;
`/archive`/`/unarchive` call `sessions.archive` (plugin event
`plugin.session.archived`, folded by `native_index.native_session_archived`);
`/rename` calls `sessions.rename` (`session.title` event).

### 9.2 Read-only vs attachable

| state | detection | UX |
| --- | --- | --- |
| attachable | `sessions.open` succeeds (lock free, parses) | `/resume` attaches; label `native` |
| read-only, locked | `SessionLockedError` on open (`session/schema.py:1218-1227`) | label `read-only (in use)`; transcript view; hint "another process owns this session" |
| read-only, damaged | `readable=False` (fold error, `native_index.py:221-230`) | transcript with `fold note: ...`; no attach |
| archived | `info.archived` | hidden unless `--all`; attachable after unarchive |

`_show_native_session` (182-206) becomes the **only** detail view (the v4 branch and
the "dpyro migrate --to v4" hint are deleted); it uses `sessions.transcript` and
`_render_native_transcript` (208-226) unchanged.

### 9.3 `/resume` semantics

`resume(argument)` (`app_sessions.py:228-281`) keeps index/id/text
selection (`matches` from `session_view.py:97-108`), then:
attachable -> `await runtime.open_session(id)`, clear conversation, render welcome,
`"resumed {short}"`, `_render_history` (native history module),
refresh status; read-only -> transcript + hint, as above.

Attach never forks: the user continues the same log (same semantics as v4). A fork is an explicit new
command:

```python
# tui/app_sessions.py
async def fork_command(self, argument: str) -> None:
    if self._working:
        self.notify("a turn is running; fork after it finishes"); return
    source = (argument.strip() or self.runtime.session_id)
    if source.startswith("session-"):
        source = source.removeprefix("session-")
    session = self.runtime.sessions.fork(source)          # returns open+locked
    handle = NativeSessionHandle(session)
    await self.runtime._detach()
    new_id = handle.session_id
    session.close()                                        # release the lock, then reopen via attach
    await self.runtime.open_session(new_id)
    self.conversation.clear(); self._render_welcome()
    self.notify(f"forked {source[:12]} -> {new_id[:12]}")
    self._render_history(); self.refresh_status()
```

Add the row to `builtin/commands_builtin.py` `_ROWS` (17-120; there is
no `/fork` today) with handler `fork_command`; it is a new capability,
not a regression. Fork is full-copy only (native_fork explicitly rejects mid-turn fork because the schema
turn-reason vocabulary has no "forked" reason).

### 9.4 Rendered history and `/hooks`

`_render_history` (`app_sessions.py:46-80`) keeps its shape but reads
`fold(handle.header, handle.events).messages` plus `tui/native_history.py`
processes (no `derive_messages`/`process_map` from
`session/derive.py`). PTC-mode user messages cannot occur natively, so the
`is_ptc_mode_message` skip (`app_sessions.py:56-58`) is deleted.

`/hooks` (`app_panels.py:174-184`) reads the v4 hook journal through
`SessionQueryPlugin` (`builtin/session_query.py`). Schema 1 has no hook
journal event: hooks are observability spans in the trace store. Native implementation:
`/hooks` renders `TraceStore` spans for the current run
(`observability/trace.py`) when trace_export is wired, else prints
`"native sessions do not journal hook calls; see the trace store"`. Honest UX change,
listed in §12.6.


---

## 10. Ordered implementation phases (area 8)

Every phase is independently testable and leaves the TUI working. Blue/green rule for P0-P5: the **v4
plugins keep their ids and bundle entries**; the native plugins are registered under temporary ids and a
single runtime switch (`DEEPPYRO_TUI_NATIVE=1`, later a constructor flag) decides which pair
`TuiRuntime.boot` requires. P6 flips ids, deletes the v4 package surface and the switch.
This keeps each commit deployable and avoids a long-lived dual-write path: only one of the two stacks is
installed in a process.

### P0 — shared native seams (no TUI behavior change)

Files: `src/deeppyro/agent/native_records.py` (new), `agent/native_factory.py`
(new), `session/native_control.py` (new), `agent/native_host.py` (extend),
`agent/loop.py` (`_tool_log`, `run(source=)`,
`repair_open_turn`, hooks to `run_tool_call`),
`agent/context_plan.py` (`tool_specs`). No TUI file changes.
Tests: `tests/agent/test_native_records.py`, `test_native_factory.py`,
`tests/agent/test_native_host.py` (extended), `tests/native/test_loop.py`
(job/goal facts now persisted through `ctx.log`; approval hooks fire).
Exit: native headless still passes `tests/native/*`; session logs now contain
`plugin.job.*` / `goal.changed` facts when those tools run.

### P1 — session control plane and boot (read + create, no turns)

Files: `builtin/sessions.py` (rewrite + keep old class as
`V4SessionsPlugin` under id `sessions-v4`), new
`builtin/native_agents.py` (temporary id `agents-native`) or a branch inside
`builtin/agents.py` (decision D6: separate module, single class swap in P6),
`tui/runtime.py` (boot requires the native pair when the flag is on),
`tui/runtime_state.py`, `tui/runtime_sessions.py` (new: native
session-layer mixin) or `runtime_session.py` rewritten behind a branch,
`tui/runtime_status.py`, `tui/runtime_support.py`,
`tui/app_sessions.py` (single catalogue + native transcript + attach for new sessions),
`tui/main.py` (`--resume`; flag bootstrap), `bundles/core/bundle.toml`.
Tests: `tests/tui/test_native_boot.py` (create, list, open, resume empty session),
`tests/tui/test_native_sessions.py` rewritten (read-only paths gone),
`tests/session/test_native_control.py`, `tests/tui/test_session_view.py`.
Exit: with the flag, TUI boots on a schema-1 session, `/sessions`/`/resume`
work, `/rename`/`/archive` work, no turn can be sent yet
(`/new` creates; submit is refused with "native turns land in P2").

### P2 — native turns in the TUI

Files: `tui/native_hooks.py` (new), `tui/app_turns.py`,
`tui/app_events.py` (no change expected — verify), `tui/app.py`
(resume arg, trace_export wiring), `tui/runtime.py` (`_plugin_services`
trace_export; compactor), `tui/sink.py` (unchanged), `tui/native_history.py` (new),
`tui/app.py`/`app_sessions.py` history imports,
`llm/native_provider.py` consumers.
Tests: `tests/tui/test_native_turn.py` (fake native provider through
`LlmPlugin.add_native_provider`, `builtin/llm.py:73`; asserts the exact event
sequence text/tool_call/tool_result/step/final/usage/status and the persisted log),
`test_native_hooks.py`, `test_native_system_prompt.py` (one
`system.message` fact, reused across turns, rewritten after `/model`),
`test_native_history.py`, `tests/tui/test_app.py`.
Exit: a full prompt/tool turn works; interrupt works; `/status` token meter updates;
`/thinking` renders reasoning; team/subagent still refused in native mode with an
explicit notice.

### P3 — native service bindings (no team/subagent yet)

Files: `working_directory/service.py` (no change expected), `sandbox/policy.py`,
`sandbox/approval.py`, `sandbox/permission.py`, `builtin/activity.py`,
`goal/service.py`, `builtin/goal.py`, `tui/runtime_schedule.py`,
`tui/runtime_schedule_host.py`, `tui/app_goal.py`,
`tui/app_panels.py` (`/jobs`, `/compact`),
`tui/runtime_status.py` `set_workspace`/`compact_now`,
`compact/native_compactor.py` (new), `compact/native_compress.py` (new),
`compact/native_refs.py` (new), `compact/tokens.py` port,
`builtin/compress.py` (register native tool variants).
Tests: `tests/tui/test_native_policy.py`, `test_native_goal.py`,
`test_native_activity.py`, `test_native_schedule.py`,
`tests/compact/test_native_compactor.py`, `tests/compact/test_native_compress.py`,
`tests/builtin/test_activity_native.py`, `tests/tui/test_schedule_host.py` (rewritten).
Exit: `/cwd`, `/permission`, `/goal`, `/plan`,
`/jobs`, `/compact`, schedule deliveries and cold schedule sessions all work on
native sessions; approval prompts appear and are journaled as
`approval.requested`/`approval.decided`.

### P4 — team and subagent on native children

Files: `team/manager.py`, `subagent/manager.py`,
`builtin/team.py`, `builtin/subagent.py`,
`builtin/task_tools.py` (native-only branch after cut), `tui/runtime_session.py`
(`_configure_team`/`_restore_team` wiring), `tui/app_panels.py`
(`_team_snapshot`), `goal/round_driver.py` (port) or bundle drop,
`agent/native_host.py` (`deliver`/`restore` already used),
`session/native_fork.py` (already done).
Tests: `tests/team/test_*` rewritten, `tests/subagent/test_*` rewritten,
new `tests/tui/test_native_team_restore.py` (spawn -> restart TUI -> restore -> queued
message replays), `tests/tui/test_native_fork_command.py` (`/fork`:
parent_id/origin/depth, full copy, parent still appendable).
Exit: `/team` spawn/send/cancel/restore; task board; handoffs; subagent run; fork
command.

### P5 — default flip and dedupe

Files: `bundles/core/bundle.toml` (native ids become canonical), `tui/runtime.py`
(flag defaults on; one minor release of `--legacy-v4-tui` is **not** planned for 0.3.0
per the hard-cut goal; the escape hatch is reverting the release), `headless.py` (delete
`_NativeSinkHooks` and route the headless turn through the shared
`TuiAgentHooks`-equivalent or a `SinkHooks` in
`agent/native_hooks.py` so both front ends share one adapter), `tui/slots.py`
footnote only (event dict unchanged), docs: `docs/native/README.md`,
`docs/design/tui-native-migration.md` (mark superseded by this doc).
Tests: full suite; `tests/test_v4_free.py` (new guard, below) runs green for the modules it
enumerates in this phase; snapshot tests regenerated.

### P6 — deletion (the hard cut)

After P5, delete these files (they become unreferenced) and remove their tests:

```text
src/deeppyro/core/wire.py                       (last importer removed by this phase)
src/deeppyro/session/session.py, store.py, writer.py, reader.py, events.py, derive.py,
                    fork.py, layout.py, metadata.py, image_projection.py, migrate*,
                    session_query/ (v4), builtin/session_query.py
src/deeppyro/agent/session.py, session_state.py, session_records.py, session_stream.py,
                    session_tools.py, session_support.py, session_prompt.py, request.py, inbox.py
src/deeppyro/prompt/projection.py, builtin/projections.py, src/deeppyro/projection/ (package)
src/deeppyro/compact/compactor.py, surface.py, refs.py, compress_tool.py, tokens.py (v4), block_tools.py
src/deeppyro/goal/replay.py, goal/service.py (v4 branch), goal/round_driver.py (if not ported)
src/deeppyro/builtin/sessions.py (v4 class), builtin/agents.py (v4 create), builtin/session_title.py (v4),
src/deeppyro/tui/history.py (v4 readers; folding readers live in native_history.py)
```

Kept and rewritten: `session/schema.py`, `session/fold.py`,
`session/native_index.py`, `session/native_query.py`,
`session/native_fork.py`, `agent/{events,loop,native_host,model_call,run_state,budget,options}.py`,
`observability/*`, `tools/*`, `prompt/prompt.py`,
`builtin/{prompt_service,presets,team,subagent,task_tools,compress,activity,jobs,goal}.py`,
all TUI widget/slot code.

Deletion preconditions (each is a test, not a wish): no module outside the delete list imports a deleted
module (the `test_v4_free.py` guard), and every user-facing command in
`builtin/commands_builtin.py:17-120` has a native implementation.

---

## 11. Test plan (area 9)

### 11.1 Rewrite (existing suites whose subject changes)

| suite | action |
| --- | --- |
| `tests/tui/test_native_sessions.py` | read-only-view assertions replaced by attach/resume/transcript/lock cases |
| `tests/tui/test_runtime_{presets,models,hooks,team_restore}.py` | retarget to native plugins; drop `agent-preset/selected` record assertions, assert fold state instead |
| `tests/tui/test_app*.py`, `test_runtime_behavior_snapshot.py` | event-dict contract frozen; snapshots regenerated from native turns |
| `tests/tui/test_history.py` | becomes `test_native_history.py` (fold-based) |
| `tests/tui/test_schedule_host.py`, `test_team_screen.py`, `test_session_view.py`, `test_commands.py` | data sources swapped; UI assertions kept |
| `tests/compact/*` | `test_compress_tool`/`test_refs` rewritten on native refs; `test_blocks` kept (sidecar); v4 compactor tests deleted |
| `tests/team/*`, `tests/subagent/*` | rewritten against native child sessions; usage tests retarget `UsageSink` |
| `tests/goal/*`, `tests/session_query/*` | native goal facts / trace-backed hook view |
| `tests/builtin/test_projections.py`, `tests/projection/*` | deleted with the subsystem |
| `tests/session/*` (v4 store/derive/fork/migrate/golden/invariants) | deleted in P6; `test_native_*` already cover the new stack |

### 11.2 New suites

* `tests/tui/test_native_boot.py` — boot, create, list, resume, lock/read-only.
* `tests/tui/test_native_turn.py` — scripted native provider; exact sink event order;
  interrupt; provider error; budget exceeded; `finish_reason == "length"` path.
* `tests/tui/test_native_hooks.py` — adapter unit test (no widgets).
* `tests/tui/test_native_system_prompt.py` — one `system.message` fact,
  `fold().system` equals rendered prompt, no duplicate on second turn, new snapshot after
  model/preset/cwd change, replay sees the snapshot in effect.
* `tests/tui/test_native_team_restore.py`, `test_native_fork_command.py`.
* `tests/agent/test_native_records.py` — mapping table incl unknown-type rejection
  before write.
* `tests/agent/test_native_factory.py` — prompt source/facts, PTC refusal.
* `tests/compact/test_native_compactor.py` — region selection, one-event write, covers
  validation, summary-size guard, trace-only usage.
* `tests/compact/test_native_compress.py` — refs/ordinals, tool-pair guard, multi-block
  partial commit, block sidecar.
* `tests/session/test_native_control.py` — `NewSession` variants
  (origin/parent/depth) rejected/validated by schema.
* `tests/integration/test_tui_native_parity.py` — same scripted scenario driven through
  headless native and TUI native; compare folded event kinds + sink event sequences.
* `tests/test_v4_free.py` — import-graph guard: a forked subprocess imports every module
  under `src/deeppyro` and asserts no module in the delete list is imported by anything
  else; plus an `ast` scan for `core.wire`/`session.store`
  imports. Runs last in P6.

Gates unchanged: `pytest`, `ruff check`, `ruff format --check`,
`pyright` (the repo's configured gates), plus the native differential matrix
`tests/native/test_differential*.py` which must stay green after every phase.

---

## 12. UX regressions (honest list)

| # | change | user-visible? | mitigation |
| --- | --- | --- | --- |
| 1 | providers without an OpenAI-compatible API cannot run the native loop | **yes, blocker** | native transports or a documented provider matrix in release notes (§13 R1) |
| 2 | PTC (run_code) presets refused | **yes** | explicit error + docs; no silent degradation |
| 3 | image/attachment parts in native transcripts render as text placeholders | **yes** | `native_index._message_text` is text-only; P6 follow-up adds a native image renderer (v4 `session/image_projection.py` is deleted) |
| 4 | `/hooks` no longer shows a per-session hook journal | yes | trace-backed view or explanatory notice (§9.4) |
| 5 | runtime-context user message no longer injected | **model behavior** | environment facts live in the system prompt snapshot; documented |
| 6 | working-directory changes are system facts + UI notice, not a model-visible synthetic user message | model behavior | prompt refresh on host rebuild; documented |
| 7 | team/subagent usage not in the parent session | no (panel has no usage column) | `UsageSink` trace records |
| 8 | v4 sessions invisible in `/sessions` | **yes** | migration tool or documented archive boundary |
| 9 | LLM-generated session titles not ported in P6 as-is | yes (titles fall back to first user text) | port `builtin/session_title.py` to fold+`native_query.rename` in P3 |
| 10 | lock contention appears as read-only sessions | new UX (better than v4 corruption) | explicit label + hint |
| 11 | `/compact` automaticity changes (manual/pre-send flag) | model context behavior | decision D4 |
| 12 | `/team` restore of a member whose child is locked | yes | member phase failed + message, same shape as v4 restore failures |
| 13 | `/fork` is new (v4 had none) | positive | full-copy only; documented |

---

## 13. Risks and blockers (R1-R12)

* **R1 (blocker) native transport coverage.** `llm/registry.py:96` only builds
  OpenAI-compatible providers; every other configured provider raises. 0.3.0 must either ship native
  transports (KI-010) or accept dropping those providers. No workaround inside the TUI design.
* **R2 (blocker for teams that use PTC) PTC surface.** `builtin/agents.py:204-249`
  builds a `run_code`-only registry for `presentation == "ptc"`; the native
  host has no surface concept. Refusal is the only safe behavior until a native `run_code`
  harness exists.
* **R3 v4 log visibility.** Native codec rejects `version` headers
  (`session/schema.py:166-171`) and the index silently skips unparseable files
  (`native_index.py:196-200`). Without a migration script users lose history in the TUI.
* **R4 compaction correctness.** The single `compaction.done` event relies on fold's
  contiguity validation (`session/fold.py:374-420`); a bad `covers` list is
  rejected but a **valid yet wrong** region is a semantic error the schema cannot catch. Mitigation: the
  compactor validates region boundaries against `view.messages` ids it just folded and
  tests cover tool-pair boundaries.
* **R5 lock contention.** The session lock is acquired at create/open and held for process lifetime
  (`session/schema.py:1214-1239`); a long summarization or a stuck TUI blocks other
  processes from attaching. v4 had no locks; this is a deliberate correctness gain with a new failure
  mode (read-only fallback UX).
* **R6 permission/approval event ordering.** `approval.requested`/`decided`
  are association-free but validated strictly (`session/schema.py:695-715`); the
  `ApprovalService` rewrite must append the pair around the ask callback and survive
  cancellation (native path: append requested, await ask, append decided; on
  `asyncio.CancelledError` append `outcome="cancelled"` before re-raising).
* **R7 prompt behavior drift.** Dropping the runtime-context user message changes what the model sees
  each turn; prompts/sections tested in `tests/builtin/test_prompt_extension.py` and
  `tests/agent/test_prompt.py` must be re-baselined.
* **R8 service ripple.** `builtin/activity.py`, `sandbox/*`,
  `goal/*`, team/subagent task boards and `tests/projection/test_activity_parity.py`
  all encode v4 record shapes; the parity test (which compares ActivityService rows with projections) is
  deleted with projections, and the new native activity branch needs its own parity test against fold.
* **R9 trace wiring.** If no `TraceExportSink` is provided, `context_tokens()`
  stays 0 and `/hooks` has nothing to show. The TUI must fail loudly (status bar shows
  "context: n/a") instead of showing 0.
* **R10 schedule host semantics.** Native `deliver` injects a `user.message`
  with `PluginSource` when idle and queues otherwise; cold sessions are opened, run one
  turn, then closed. Behavior is close to v4's `followup` but message provenance differs
  (fold exposes it), which downstream filters must tolerate.
* **R11 `goal/round_driver.py`** imports v4 `Message`/`SessionHeader`
  and the v4 `Inbox` (`goal/round_driver.py:54-56`). Porting it is a
  small but real work item; dropping it removes autonomous goal rounds.
* **R12 test-suite blast radius.** 344 test files; the v4 suites are the bulk of the coverage. Deleting
  them in P6 without the replacements from P0-P5 green would hide regressions. Exit criteria for each
  phase therefore include the new suite plus `tests/native/*` and
  `tests/agent/test_native_host.py`.

---

## 14. Decisions

* **D1** The schema-1 `system.message` fact is the only persisted prompt snapshot; no
  runtime-context user projection. One writer: `NativeAgentHost.refresh_prompt`.
* **D2** One native session catalogue. Read-only vs attachable is decided by open success/lock/corrupt,
  not by session format; v4 and native are never merged in one list.
* **D3** Resume attaches; fork is an explicit `/fork` command with full-copy semantics
  (`session/native_fork.py:70`).
* **D4** No auto-compaction inside `AgentLoop` in 0.3.0: manual `/compact`
  plus a pre-send threshold check in the TUI turn layer (config-gated).
* **D5** Cross-session token usage (team/subagent) is trace-only; the session log records facts
  (`docs/native/schema.md` §8.5).
* **D6** Blue/green via temporary plugin ids/modules during P0-P5; P6 swaps ids, deletes v4 modules and
  the runtime switch. No dual-write, no dual-read.
* **D7** No new runtime dependencies (stdlib + existing packages only; tests may reuse existing test
  helpers).
* **D8** The hard cut is one release (0.3.0), per project goal. Rollback is a release revert, not a
  runtime flag; `--legacy-v4` remains headless-only during the transition and is removed
  in P6.

---

## Appendix A — final sink event contract

| event | producer | consumers |
| --- | --- | --- |
| `{"type":"session","sessionId":...}` | `TuiRuntime._attach` | slots, plugin status bar |
| `{"type":"status","phase":...}` | `TuiAgentHooks.run_started`, turn layer | status bar, `_phase` |
| `{"type":"thinking","text":...}` | hooks `reasoning_delta` | conversation |
| `{"type":"text","text":...}` | hooks `text_delta` | conversation |
| `{"type":"step","turn":n,"step":n}` | hooks `step_started` | conversation step headers |
| `{"type":"tool_call","callId","name","arguments"}` | hooks `tool_call_started` | tool cards |
| `{"type":"tool_result","callId","isError","text"}` | hooks `tool_call_finished` | tool cards |
| `{"type":"final","text":...}` | turn layer | `ConversationView.finish_turn` |
| `{"type":"usage","input","output","cached"}` | turn layer | context/status bar |
| `{"type":"error","message":...}` | turn layer (exceptions, truncation) | conversation error row |

`phase` is one of `thinking`, `done`, `interrupted` (native adds an explicit interrupted phase that v4 only signalled by a silent abort).

Plugin slots keep receiving these exact dicts, so `tui/slots.py` predicates and
`build_slot_context` (721-729) stay forward-compatible.

## Appendix B — v4 record to native event mapping (extended `RECORD_KIND_MAP`)

```text
agent-preset/selected  -> preset.selected       (payload normalised to schema shape)
model/selection        -> model.selected
sandbox/mode           -> sandbox.mode
approval/policy        -> permission.policy
working-directory/change -> workspace.cwd_changed (absolute path required)
session/title          -> session.title         (source "user"|"llm")
todo/write             -> todo.written
approval/asked         -> approval.requested    (NEW: {approval_id, tool_call_id, tool_name, reason?})
approval/decided       -> approval.decided      (NEW: {approval_id, outcome})
goal/change            -> goal.changed          (NEW: snake_case goal payload + rounds_started)
plan/mode              -> plan.mode             (NEW)
deliverables/presented -> deliverable.presented (NEW: {files:[{path[,description]}]})
job/registered         -> plugin.job.registered (NEW: plugin event)
job/settled            -> plugin.job.settled    (NEW: plugin event)
team/member            -> plugin.team.member    (NEW)
team/message/queued|delivered -> plugin.team.message (NEW: state field)
team/task              -> plugin.team.task      (NEW)
subagent/catalog|descriptor -> plugin.subagent.descriptor (NEW)
hook/invoked|result, feedback/*, llm/retry*, request/*, session-log-*  -> trace store (never sessions)
image/offload, workspace/changes, command/*, tool/ptc-*  -> no native equivalent (PTC out of scope)
```

Unknown mapping still raises `UnsupportedRecordTypeError` before write; new native writers
use `append_native` with the schema enum directly.

## Appendix C — who writes which event (native)

```text
AgentLoop.run            run.started, turn.started, user.message, step.started, assistant.message,
                         tool.call, tool.result, step.ended, turn.ended, run.finished, run.checkpoint
TUI turn layer + host    system.message (host.refresh_prompt), user.message (host.deliver/send)
PermissionPresetService  sandbox.mode, permission.policy
ApprovalService          approval.requested, approval.decided
WorkingDirectoryService  workspace.cwd_changed
TUI session layer        preset.selected, model.selected, session.title, workspace.cwd_changed (set_workspace)
Compactor/compress tool  compaction.done
TeamService              plugin.team.member, plugin.team.message, plugin.team.task
SubagentService          plugin.subagent.descriptor
SessionTitleService      session.title
TaskService (existing)   task.state, task.handoff, task.handoff.result
GoalService (rewired)    goal.changed
SessionQuery/rename      session.title; archive -> plugin.session.archived
```

## Appendix D — anchors to re-verify in P0 (working tree is in flux)

Every line number above was taken from the current working tree. Before implementing, P0 re-checks:
`core/llm.py` (ModelRequest/Usage/StreamEvent), `agent/loop.py`
(_build_plan 499, run_tool_call 726, _tool_context 737), `agent/native_host.py`
(handle 214-398, host 434-612), `agent/context_plan.py` (ToolSpec),
`session/schema.py` (create 447, open 493, append 505, validators 655-1180, lock
1214-1239), `session/fold.py` (system 238, compaction 374-420, fold 607),
`session/native_index.py` (info 51, list 111, transcript 134, archive 276),
`session/native_query.py` (rename 422, archive 442), `session/native_fork.py`
(70), `observability/trace.py` (ModelCallRecord 240, TraceStore 435),
`tui/sink.py`, `tui/app_events.py` (16-51), `tui/app_turns.py`
(35-47, 85-91), `tui/runtime_session.py` (65-235), `tui/app_sessions.py`
(28-380), `tui/runtime_status.py`, `tui/runtime_schedule*.py`,
`team/manager.py` (536-1081), `subagent/manager.py` (557-808),
`compact/*`, `builtin/{agents,sessions,prompt_service,llm,team,subagent,activity,goal,compress}.py`,
`sandbox/{permission,approval,policy,gate}.py`, `src/deeppyro/core/messages.py`,
`bundles/core/bundle.toml`.

