# Plugin core: contract, composition, extension points

Status: layers A (composition kernel) and B (capability registries) are implemented.
Everything below is the contract external plugins and bundles may rely on.

## 1. Layering

Unpluggable core (never replaced by a plugin):

- `src/deeppyro/_vendor/cordis_port/` - vendored plugin runtime (fibers, services, effects).
- `src/deeppyro/core/wire.py` and `src/deeppyro/core/protocols.py` - frozen wire and LLM/tool contracts.
- `src/deeppyro/session/` - append-only session facts, surface fold and derive rules.
- `src/deeppyro/kernel/` - loader, composition, lifecycle, failure isolation.
- `src/deeppyro/api/` - the plugin-facing contract.

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
    api_version = 1              # integer, checked against deeppyro.api.API_VERSION
    inject = ["tools"]           # cordis service dependencies
    Config = MyConfig            # optional pydantic model; row.config is validated into it

    def setup(self, ctx, config: MyConfig) -> None:
        ctx.register_tool(lambda cfg, workspace: MyTool())
```

- `setup(ctx, config)` runs once when the plugin fiber activates. It may be sync or async.
- Define `async def stop(self)` when you need explicit cleanup; the loader registers it as
  the fiber disposer. Registration helpers additionally dispose themselves via cordis effects,
  so unloading a plugin removes its tools, prompt sections, commands and providers.
- A `Service` subclass both is a plugin and provides a cordis service:

```python
from deeppyro.api import Service

class MyService(Service):
    provide = "myservice"
    def __init__(self, ctx, config):
        super().__init__(ctx, config)   # registers the service immediately
        self.items = []
```

`ctx.service(name)` / `ctx.get(name)` / `ctx.has(name)` resolve dependencies;
`ctx.tools`, `ctx.prompt`, `ctx.commands`, `ctx.llm` (and later `ctx.presets`) are typed shortcuts.
`PluginContext` proxies every other attribute to the raw cordis context, so `ctx.on`, `ctx.emit`,
`ctx.effect`, `ctx.isolate` and `ctx.intercept` stay available.

## 3. Composition: bundles, rows, patches

A bundle is a directory with `bundle.toml`:

```toml
[bundle]
id = "my-bundle"
name = "My bundle"
version = "0.1.0"
order = 50          # applied after lower order bundles
critical = false    # default for external bundles

[[plugins]]
id = "my-plugin"
plugin = "my_plugin.py:MyPlugin"   # path relative to the bundle dir, or module:Class
config = { greeting = "hi" }
```

Row keys: `id`, `plugin`, `config`, `disabled`, `before`, `after`, `critical`, `name`,
`isolate`, `intercept`. Unknown keys are rejected.

Patch semantics (deliberately different from a deep merge):

- same `id`: `config` is replaced **as a whole**, `disabled`/`critical` are overridden,
  `isolate`/`intercept` merge, and the plugin class may not change;
- new `id`: inserted by `before`/`after`, otherwise appended; `plugin` is required;
- layer order: core bundle, then `~/.dpro/bundles/*/bundle.toml` (sorted by order then id),
  then explicit bundle paths, then user manifests (`~/.dpro/plugins.toml`, atomic-edit surface
  owned by the plugin manager).

## 4. Core bundle rows

`src/deeppyro/bundles/core/bundle.toml` mounts, in order:

| id | module | service / contribution |
|---|---|---|
| `llm` | `builtin.llm` | `llm`: providers, `.provider(name)`, `.add_provider` |
| `sessions` | `builtin.sessions` | `sessions`: `.store` (SessionStore) |
| `sandbox` | `builtin.sandbox` | `sandbox`: `.gate(workspace, ask)` |
| `tools` | `builtin.tools` | `tools`: contribution registry, `.registry_for(workspace, scope=None)` |
| `tools-builtin` | `builtin.tools` | eleven built-in tools |
| `presets` | `builtin.presets` | `presets`: named scopes for tools and prompt sections |
| `prompt` | `builtin.prompt_service` | `prompt`: ordered sections, `.build(tools, prompt_context, scope=None)` |
| `prompt-core` | `builtin.prompt_core` | identity/environment/instructions/tools/rules sections |
| `commands` | `builtin.commands` | `commands`: slash-command registry |
| `commands-builtin` | `builtin.commands_builtin` | the ten built-in slash commands |
| `agents` | `builtin.agents` | `agents`: `.create(...)`, `.options(**overrides)` |

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
- services: a `Service` subclass exposes an entirely new service name.

All registration helpers return a dispose callable and are torn down with the plugin fiber.

### 5.1 Agent presets (per-session composition)

A preset is the smallest per-session composition unit. Tools and prompt sections registered with
`scope=<preset id>` are visible only to an agent created with `preset=<preset id>`; unscoped
contributions stay visible everywhere. `agents.create(..., preset=id)` resolves the preset,
appends its `instructions` to the agent options, applies its `model` override when the caller
did not set one, and builds the system prompt with that scope. Unknown ids raise
`PluginDependencyError`.

## 6. Failure isolation and status

`App.failures` lists `PluginStatus` entries for non-critical rows that failed to load;
`App.started` maps every mounted row id to its cordis fiber state (`ACTIVE`, `PENDING`, ...).
Boot failure paths are covered by `tests/kernel/test_app.py` and `tests/kernel/test_bundle.py`.

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
    inject = ["tools", "prompt", "commands"]

    def setup(self, ctx, config):
        ctx.register_tool(lambda cfg, workspace: GreetTool())
        ctx.register_command("greet", self.run_greet, summary="greet someone", usage="/greet [name]")
```

## 8. Tests and files

| area | files |
|---|---|
| contract | `src/deeppyro/api/{__init__,errors,types,context,plugin}.py` |
| composition | `src/deeppyro/kernel/{app,composition,bundle,manifest}.py` |
| core plugins | `src/deeppyro/builtin/*.py`, `src/deeppyro/bundles/core/bundle.toml` |
| tests | `tests/kernel/`, `tests/integration/test_plugin_composition.py`, `tests/integration/test_agent_presets.py`, `tests/tui/test_commands.py` |

## 9. Known limits

- No install/remove of bundles yet (layer D: `plugin_manager`), no settings editor and no live
  inspection yet.
- Preset switching rebuilds the live agent but does not rewrite the current session header, so
  `agentPreset` keeps the value the session was created with; only new sessions record the new id.
- Preset `model` overrides skip config validation; an unknown model id fails on the next request.
- Plugins run in-process and are trusted once loaded; there is no OS-level isolation.
- `api_version` is a single integer gate, not a negotiation protocol.
