# Plugin core: contract, composition, extension points

Status: plugin API v3 is the current contract (0.2.0); layers A (composition kernel) and B
(capability registries) are implemented. Everything below is the contract external plugins and
bundles may rely on. v3 is a hard cut: there is no v1/v2 compatibility shim (ADR 0001 section 7),
so a plugin or bundle declaring another `api_version` is rejected at load time with a migration
note. The loader also supports group rows, include files, bundle dependencies and live plugin
states; the `kernel` runtime service applies volatile config changes without a restart
(sections 3 and 6).

## 1. Layering

Unpluggable core (never replaced by a plugin):

- `src/deeppyro/kernel/` - the native plugin runtime (`container.py`, `context.py`,
  `fiber.py`, `host.py`) plus the loader/composition/boot layer.
- `src/deeppyro/core/wire.py` and `src/deeppyro/core/protocols.py` - frozen wire and LLM/tool contracts.
- `src/deeppyro/session/` - append-only session facts, surface fold and derive rules.
- `src/deeppyro/api/` - the plugin-facing contract.

The runtime is plain Python: services live in a dict-like container keyed by name (plus an
optional isolate scope), a plugin gets a `RuntimeContext` with an explicit parent pointer,
and each mounted plugin is a `Fiber` with a small lifecycle enum (`PENDING`, `LOADING`,
`ACTIVE`, `FAILED`, `DISPOSED`). Effects run in reverse registration order, are
idempotent, and async teardowns are awaited; there is no reflection layer, no prototype
chain and no framework event plumbing. `inject` names are dependency hints: a fiber whose
services are missing stays `PENDING` and starts as soon as they are provided.

Everything else is plugin-provided: the llm/sessions/sandbox/tools/prompt/commands/agents
services are ordinary api plugins in `src/deeppyro/builtin/` and the TUI is a host on top.

## 2. Plugin contract (`deeppyro.api`)

```python
from pydantic import BaseModel
from deeppyro.api import Plugin

class MyConfig(BaseModel):
    greeting: str = "hello"

class MyPlugin(Plugin):
    plugin_id = "my-plugin"
    version = "0.1.0"
    api_version = 3              # must equal deeppyro.api.API_VERSION (3) or the mount is rejected
    inject = ("tools",)          # service names, or contract classes such as CommandsPlugin
    Config = MyConfig            # optional pydantic model; row.config is validated into it

    def setup(self) -> None:
        self.ctx.register_tool(lambda cfg, workspace: MyTool())
```

- API v3 has exactly one mount shape: the adapter constructs the plugin with `(ctx, config)`
  and then calls `setup(self)`, so a plugin reads `self.ctx` and `self.config`. `setup`
  runs once when the plugin fiber activates and may be sync or async. `setup` must take no
  arguments: a method with positional parameters, or a class declaring another
  `api_version`, raises `PluginError` with a migration note (there is no v1/v2 fallback).
- `inject` entries may be service names or contract classes, and typed entries are the
  preferred form (`inject = (KernelConfig, ToolsPlugin)`). Classes resolve to their
  `provide` name at mount time (`deeppyro.api.inject_names`), the fiber then waits on the
  resolved names, and an unbound or non-service class aborts the mount with a
  `CompositionError` that names the row.
- Mounting another plugin has exactly one entry point: `self.ctx.mount(plugin, config)`
  (`deeppyro.kernel.context.RuntimeContext.mount`). The v2 `ctx.plugin` name was removed;
  the kernel adapter and the preset realms both go through `mount`.
- Define `async def stop(self)` when you need explicit cleanup; the loader registers it as
  the fiber disposer. Registration helpers additionally register effects,
  so unloading a plugin removes its tools, prompt sections, commands and providers.
- `volatile` names the config fields a plugin accepts live:
  `volatile: ClassVar[tuple[str, ...]] = ()`. When a row's config is changed at runtime
  (section 6.3), only volatile fields may differ; the kernel runtime re-validates the merged
  config and calls the optional `on_config(config)` hook. A change to any other field
  returns False and marks the row `restart-required` instead. `on_config` must be
  synchronous.
- A `Service` subclass both is a plugin and provides a service:

```python
from deeppyro.api import Service

class MyService(Service):
    provide = "myservice"
    def __init__(self, ctx, config):
        super().__init__(ctx, config)   # registers the service immediately
        self.items = []
```

Dependencies are typed only: `ctx.get(Contract)` returns the service or `None`,
`ctx.require(Contract)` raises `ServiceNotFoundError`, and `ctx.has(Contract)` returns a
bool. Strings are rejected (`TypeError`): a plugin asks for a contract class, not a service
name. `ctx.tools`, `ctx.prompt`, `ctx.commands`, `ctx.llm` and `ctx.presets` are typed
shortcuts over the same lookup. `PluginContext` no longer proxies unknown attributes
(accessing `ctx.some_service` raises `AttributeError`); the explicitly supported surface is
`ctx.get/require/has`, `ctx.provide`, `ctx.mount`, `ctx.effect`, `ctx.on`/`ctx.once`/
`ctx.emit`, `ctx.isolate`/`ctx.intercept`, the registration helpers and `ctx.raw`.
Bus event names always go through `core.bus.EventBus`; the runtime raises no plumbing events
of its own. Capability metadata (`risk`, `idempotent`, `permissions`, `sandbox`,
`token_cost`, `context_window`) lives on the plugin class and is folded into a
`CapabilityManifest` by `self.capability_manifest()` / `deeppyro.api.build_manifest`.

## 3. Composition: bundles, rows, patches

A bundle is a directory with `bundle.toml`:

```toml
[bundle]
id = "my-bundle"
name = "My bundle"
version = "0.1.0"
api_version = 3     # must equal deeppyro.api.API_VERSION; omitted means 3
order = 50          # applied after lower order bundles
critical = false    # default for external bundles

[[plugins]]
id = "my-plugin"
plugin = "my_plugin.py:MyPlugin"   # path relative to the bundle dir, or module:Class
config = { greeting = "hi" }
```

Row keys: `id`, `plugin`, `config`, `disabled`, `before`, `after`, `critical`, `name`,
`isolate`, `intercept`, `group`, `rows`, `include`. The loader validates every bundle
file, include file and nested group row against the pydantic models in
`deeppyro.kernel.bundle_schema` (known keys only, scalar types, recursive row validation), so
unknown keys are rejected with the offending key named. `rows` and `include` are
composition-time fields (sections 3.1 and 3.2).

Patch semantics (deliberately different from a deep merge):

- same `id`: `config` is replaced **as a whole**, `disabled`/`critical` are overridden,
  `isolate`/`intercept` merge, and the plugin class may not change; the loader marks the
  row `overridden` when a later layer replaced its config or disabled flag;
- new `id`: inserted by `before`/`after`, otherwise appended; `plugin` is required
  unless the new row is a container (group);
- layer order: core bundle, then `~/.dpro/bundles/*/bundle.toml` (sorted by order then id),
  then explicit bundle paths, then user manifests (`~/.dpro/plugins.toml`, atomic-edit surface
  owned by the plugin manager). Bundles are reordered so every `requires` dependency mounts
  first; the core bundle always mounts first.

### 3.1 Groups (nested rows and child contexts)

A row becomes a **group** when it sets `group = true` or carries inline children. Inline
children are written either as TOML array-of-tables or as an inline array of tables:

```toml
[[plugins]]
id = "my-group"
isolate = ["my-scope"]
intercept = { myservice = { mode = "group" } }

[[plugins.rows]]
id = "child-a"
plugin = "my_plugin.py:ChildA"

[[plugins.rows]]
id = "child-b"
plugin = "my_plugin.py:ChildB"
```

The group creates one child runtime context (applying its `isolate` and `intercept` layers),
mounts its children in listed order there, and is exposed as a single `App.rows` entry. A
group has no fiber of its own: its state aggregates its children (active only when every child
is active, failed if any child failed; see 6.2), and the children are reported individually with
`parent` set to the group id. A disabled group skips all children; a patch layer can
disable/override the group as a unit but cannot edit its children individually (a group child is
not addressable by row id from a patch layer). Group rows must not declare a `plugin` target.

`rows = [{ id = "child-a", plugin = "..." }, ...]` inline syntax is equivalent to repeated
`[[plugins.rows]]` tables.

### 3.2 Include files

A non-group row may splice an ordered row list from another file:

```toml
[[plugins]]
id = "extra-rows"          # load-time macro: disappears after splicing
include = "extra.toml"     # path relative to the bundle directory
```

The included file contains row tables only (no `[bundle]` wrapper); the included rows are
spliced in place at that point of the parent bundle, in listed order. The include row itself is
not mounted and cannot be targeted by a patch layer. A group row with `include` appends the
included rows to its children instead.

Only `.toml` and `.json` are supported: a TOML file holds `[[plugins]]` (or
`[[rows]]`) tables; a JSON file holds either a list of row objects or an object with a
`plugins` or `rows` list. Includes are resolved relative to the including file, nesting
is capped, and cycles or unknown formats raise `CompositionError`.

### 3.3 Bundle dependencies

The `[bundle]` table accepts `requires`, either as tables or as strings:

```toml
[bundle]
id = "my-bundle"
version = "0.2.0"
requires = [{ id = "core", version = ">=0.1" }, "other-bundle>=1.0"]
```

Every requirement must name a bundle that is part of the same boot (core, a discovered user
bundle or an explicit `bundle_paths` entry). `version` is optional; when present it is a
comparator (`>=`, `<=`, `>`, `<`, `==`, `=`, `!=`) followed by a dotted
numeric version, checked against the required bundle's `version`. The loader mounts
dependencies before their dependants; independent bundles keep the `(order, id)` sort. A
missing bundle, a dependency cycle or an unsupported constraint raises `CompositionError`.

## 4. Core bundle rows

`src/deeppyro/bundles/core/bundle.toml` mounts, in order:

| id | module | service / contribution |
|---|---|---|
| `kernel` | `kernel.runtime` | `kernel`: live row states, volatile config updates (6.3) |
| `llm` | `builtin.llm` | `llm`: providers, `.provider(name)`, `.add_provider` |
| `sessions` | `builtin.sessions` | `sessions`: `.store` (SessionStore) |
| `sandbox` | `builtin.sandbox` | `sandbox`: `.gate(workspace, ask)` |
| `tools` | `builtin.tools` | `tools`: contribution registry, `.registry_for(workspace, scope=None)` |
| `tools-builtin` | `builtin.tools` | eleven built-in tools |
| `presets` | `builtin.presets` | `presets`: named scopes for tools and prompt sections |
| `prompt` | `builtin.prompt_service` | `prompt`: ordered sections, `.build(tools, prompt_context, scope=None)` |
| `prompt-core` | `builtin.prompt_core` | identity/environment/instructions/tools/rules sections |
| `commands` | `builtin.commands` | `commands`: slash-command registry |
| `commands-builtin` | `builtin.commands_builtin` | eleven built-in slash commands (incl. `/panel`) |
| `agents` | `builtin.agents` | `agents`: `.create(...)`, `.options(**overrides)` |
| `plugin-tools` | `tools.builtin.plugin_tools` | four plugin-manager/inspect/settings agent tools |
| `slots` | `tui.slots` | `slots`: TUI slot registry (status items, conversation rows, panels, settings pages) |
| `plugin-manager` | `builtin.plugin_manager` | `plugin_manager`: external plugins and bundles (`.manager`) |
| `settings` | `builtin.settings` | `settings`: config read/write with revision CAS (`.store`) |
| `inspect` | `builtin.inspect` | `inspect`: live services, plugins, tools, prompts, commands, providers, config schema |

The `kernel` row is mounted first so every later row may inject the `kernel` service.
Core rows are `critical = true`: a failure aborts boot with `KernelBootError` and disposes
every already-mounted fiber. External rows default to non-critical: a failure is recorded in
`app.failures` and boot continues.

## 5. Extension points

- tools: `ctx.register_tool(factory, scope=None)`; the factory is `callable(config, workspace)`
  and returns one tool or a list. A non-None `scope` reserves the tool for an agent preset.
- prompt sections: `ctx.register_prompt_section(name, order, renderer, scope=None)`; renderer is
  `callable(tools, prompt_context) -> str`, sorted by `(order, name)`. Core uses 10 identity,
  20 environment, 30 instructions, 40 tools, 50 rules; plugin sections usually sit in between.
  A non-None `scope` renders only for agents created with that preset.
- presets: `ctx.register_preset(PresetSpec(...))`; a preset carries `id`, `name`,
  `description`, `order`, an optional `model` override and extra `instructions`. Sessions are
  created with `agent_preset=<id>` (the id lands in the session header), and the TUI switches
  presets through `/preset`.
- commands: `ctx.register_command(name, handler, summary=..., usage=..., aliases=...)`; handler is
  `callable(host, argument)` where host is the TUI app (or headless host object).
- providers: `ctx.register_provider(name, factory, description=...)`; factory is
  `callable(llm_service) -> LlmProvider` and wins over a config provider with the same name.
- services: a `Service` subclass exposes an entirely new service name; other plugins inject it
  either as a name or as the contract class.
- mounting: `ctx.mount(plugin, config)` mounts a child plugin/fiber under the current context.
  There is no second mount entry point (sections 1 and 2).
- runtime self-extension: the `plugin_manager`, `settings` and `inspect` services (rows
  `plugin-manager`, `settings`, `inspect`) let an agent install/list/enable/remove external
  bundles and plugins, read and write plugin config, and inspect the live composition. The same
  operations are exposed to the model as the `plugin_manager`, `plugin_inspect`,
  `settings_read` and `settings_write` tools contributed by row `plugin-tools`. Installing a
  bundle executes third-party Python on the next boot, so it requires human approval.
- TUI slots: the `slots` service (row `slots`, plugin `deeppyro.tui.slots:SlotsPlugin`) is a
  Textual-free registry with four kinds: `STATUS`, `CONVERSATION`, `PANEL` and `SETTINGS`.
  Plugins register renderers/factories with a unique `id` and optional `order`; the host renders
  them via `PluginStatusBar`, `apply_conversation_slots`, `/panel` and the settings screen.
  See `docs/spec/tui-slots.md` for the contract and the `SlotError`/`dispose` rules.

All registration helpers return a dispose callable and are torn down with the plugin fiber.

### 5.1 Agent presets (per-session composition)

A preset is the smallest per-session composition unit. Tools and prompt sections registered with
`scope=<preset id>` are visible only to an agent created with `preset=<preset id>`; unscoped
contributions stay visible everywhere. `agents.create(..., preset=id)` resolves the preset,
appends its `instructions` to the agent options, applies its `model` override when the caller
did not set one, and builds the system prompt with that scope. Unknown ids raise
`PluginDependencyError`.

## 6. Failure isolation and status

### 6.1 Boot status

`App.failures` lists `PluginStatus` entries for non-critical rows that failed to load;
`App.started` maps every loaded top-level row id to its runtime fiber state (`ACTIVE`,
`PENDING`, ...). Disabled rows are absent from `App.started`; `App.row_states()` reports
them instead. Boot failure paths are covered by `tests/kernel/test_app.py` and
`tests/kernel/test_bundle.py`.

### 6.2 Live plugin states (`App.row_states()`)

`App.row_states()` returns one dict per composition row, in mount order
(`Composition.flat()`: group children follow their group). Keys: `id`, `plugin`,
`name`, `state`, `runtime_state`, `critical`, `origin`, `error`, `group`,
`parent`, `overridden`, `disabled`, `config`, `children`. `runtime_state` is
the raw fiber state (`active`, `pending`, ...) or `group`/`unmounted`; `config`
is the effective config (pydantic models are dumped to plain dicts before reporting).

`state` is one of:

| state | meaning |
|---|---|
| `active` | mounted and running (for a group: every child active) |
| `failed` | the row or one of its children failed to load; see `error`/`App.failures` |
| `disabled` | the row or an ancestor group has `disabled = true`; it was never mounted |
| `overridden` | mounted, and a later patch layer replaced its config/disabled flag |
| `restart-required` | a live config update could not be applied; restart to pick it up |
| `pending` | waiting on injected services, or a group whose children are still loading |

Precedence: disabled > restart-required > failed > (group aggregate or pending) > overridden >
active. `overridden` is load-time metadata: it says the effective row came from a later
layer, not that anything failed.

### 6.3 Kernel runtime service

The first core row (`id = "kernel"`, `deeppyro.kernel.runtime:KernelRuntime`) provides the
`kernel` service. It is a normal `Service` plugin with typed
`inject = (KernelConfig,)` that the loader attaches to the booted `App`:

- `mounted_plugins() -> list[dict]` - every composition row as reported by
  `App.row_states()` (including disabled and failed rows); returns `[]` before the runtime
  is attached. It is the shared source of truth for live plugin states.
- `apply_plugin_config(plugin_id: str, config: dict) -> bool` - live-updates one mounted
  row's config. Only fields listed in the plugin class's `volatile` tuple may change: the
  runtime deep-copies the current config, updates the volatile fields, re-validates with the
  plugin's `Config` model and calls the plugin's optional `on_config(config)` hook.
  Returns `True` when the update was applied (also when all requested values already match,
  which clears a pending `restart-required` mark). Returns `False` for an unknown or
  unmounted id, a group, a disabled row, a plugin without volatile fields, a non-volatile field
  change, or a validation error. The two cases that need a restart (no volatile fields,
  non-volatile change) record the row as `restart-required`; validation errors keep the old
  config active and are not sticky.

The runtime never restarts a fiber and never persists config: volatile updates are in-memory
only, and writing `~/.dpro/plugins.toml` remains the settings store's job.

## 7. Worked example

`tests/integration/test_plugin_composition.py` writes a bundle into a temporary directory with
a plugin file, boots it via `boot(cfg, bundle_paths=[...])`, and asserts that the bundle adds a
tool (callable), a prompt section (rendered), a slash command and an LLM provider without any
change to the repository.

Minimal plugin file:

```python
from deeppyro.api import Plugin
from deeppyro.core.protocols import ToolResult
from deeppyro.tools.base import BaseTool

class GreetTool(BaseTool):
    name = "greet"
    description = "Greet somebody by name."

    async def run(self, args, ctx):
        return ToolResult(text="hello " + str(args.get("who", "world")))

class GreetPlugin(Plugin):
    plugin_id = "greet"
    api_version = 3
    inject = ("tools", "prompt", "commands")

    def setup(self):
        self.ctx.register_tool(lambda cfg, workspace: GreetTool())
        self.ctx.register_command(
            "greet", self.run_greet, summary="greet someone", usage="/greet [name]"
        )
```

## 8. Tests and files

| area | files |
|---|---|
| contract | `src/deeppyro/api/{__init__,errors,types,context,plugin,services,capabilities,version}.py` |
| composition | `src/deeppyro/kernel/{app,composition,bundle,manifest,runtime}.py` |
| core plugins | `src/deeppyro/builtin/*.py`, `src/deeppyro/bundles/core/bundle.toml` |
| tests | `tests/kernel/` (incl. `test_composition_groups.py`, `test_composition_requires.py`, `test_runtime.py`), `tests/integration/test_plugin_composition.py`, `tests/integration/test_agent_presets.py`, `tests/integration/test_plugin_services.py`, `tests/tui/test_commands.py`, `tests/tui/test_slots.py` |

## 9. Known limits

- Settings writes take effect on the next boot unless the changed field is volatile for that
  plugin (section 6.3); the settings store has no cross-process lock and rewrites the user
  manifest without preserving comments.
- `bundle install` only validates structure; the third-party code runs on the next boot, with no
  signature or sandbox. Removing a bundle does not stop an already-loaded plugin.
- Plugin status/conversation slots are append-only: a conversation row contributed after a
  message was rendered is not re-rendered by the host.
- Preset switching rebuilds the live agent but does not rewrite the current session header, so
  `agentPreset` keeps the value the session was created with; only new sessions record the new id.
- Preset `model` overrides skip config validation; an unknown model id fails on the next request.
- Plugins run in-process and are trusted once loaded; there is no OS-level isolation.
- `Plugin.api_version` and `bundle.api_version` must equal the host `API_VERSION` (3); there
  is no negotiation protocol and no v1/v2 compatibility shim (ADR 0001 section 7).
- Live config updates are volatile-only and never restart a fiber; every other field needs a full
  restart (the row is reported `restart-required` meanwhile). `apply_plugin_config` does
  not persist anything: writing user manifests stays the settings store's job.
- Plain host-style plugin classes (constructed and disposed by the runtime, not
  `deeppyro.api.Plugin`) have no `volatile`/`on_config` contract, so the kernel runtime
  cannot reconfigure them live; their row config only applies at boot.
- Bundles are TOML only: unlike the reference DSH implementation there is no YAML and no
  `!!js`/expression evaluation in row config. Include files are TOML or JSON only.
- Include-only rows are load-time macros: after splicing they no longer exist, so a patch layer
  cannot target the include row by id (it can target the spliced rows).
- Group children are not individually addressable from patch layers; patch or disable the whole
  group. Group containers are static once the bundle is loaded.
