# Agent presets: revisions, agent modes and per-preset plugin realms

Status: implemented by the `presets` core plugin (`src/deeppyro/builtin/presets.py`), the
`presets`/agents/tools/prompt integration and `api/types.py` fields. This document is the contract
for the revision, agent-mode and realm additions; `docs/spec/plugin-core.md` §5.1 still describes
the original scope-only preset model.

## 1. Model

An agent preset is an immutable snapshot of a per-session composition:

- `PresetSpec` carries `id`, display `name`/`description`, `model`, `instructions`, `order`,
  `mode`, its `plugins` rows and the host-assigned `revision`.
- A **revision** is one immutable generation of a preset id. Editing a preset never mutates a
  registered revision: it registers a new one. Agents that already exist keep the revision they
  were created with, so a live session never changes composition mid-turn.
- A **realm** is the isolated mounting of a revision's `PresetPluginRow` rows. Rows run in a child
  cordis context of the `presets` fiber, and every registration made through their
  `PluginContext` is automatically tagged with `(preset id, revision)`.

## 2. Built-in presets and agent modes

Every `PresetSpec` carries a `mode` field (`AGENT_MODE_DEFAULT` = `"default"` unless set) and a
`hidden` flag (default `False`). The mode partitions the delegation tools an agent may see; it is
part of the frozen preset snapshot just like the model or instructions. Hidden presets are not
offered by the `/preset` picker and cannot be selected with `/preset`; they exist for a dedicated
command. Two presets are built in at boot, both at revision 0, without plugin rows and without
consuming the host revision counter:

| id | name | order | mode | hidden | purpose |
|---|---|---|---|---|---|
| `default` | Default | 0 | `default` | no | host-wide tools and prompt sections; one-shot delegation |
| `team` | Team | 200 | `team` | yes | orchestrate persistent members instead of one-shot delegation |

`/preset` lists only visible, default-mode presets; `/preset team` is rejected with a hint to use
`/team on`, so a typo cannot silently enter team mode. Team mode is entered and left **on the
current session** with `/team on` and `/team off`:

- Both rebuild the live agent around the same durable session, so conversation history is preserved
  and the next turn sees the new system prompt and tool set.
- `/team on` remembers the preset that was active before team mode. Repeating `/team on` is
  idempotent.
- `/team off` restores that remembered preset (the `default` preset if unknown) and refuses while
  any team member is spawning, running or idle, listing the live members; neither switch is allowed
  during a running turn.
- The switch is atomic from the UI's point of view: materialization and agent rebuild happen before
  the runtime/UI state is committed; if the rebuild fails the previous preset fields and agent are
  restored and the error is surfaced.
- `/team` without an argument still opens the roster panel (or a hint when the team is empty and
  team mode is off); `/team board` opens the task board.

The active preset and (for team mode) the remembered previous preset are persisted in the session
record described in §9, so reopening a session restores team mode and can later leave it correctly.
Neither built-in can be removed through the revision counter; editing either id through
`presets.add()` creates a new immutable revision exactly like a plugin-registered preset.

### Tool sets by mode

| consumer | sees `team_*` | sees `subagent` | other tools |
|---|---|---|---|
| default-mode agent | never | yes | host-wide + `default`-scoped |
| team-mode agent | yes | never | host-wide + `team`-scoped |
| teammate (child agent created by `TeamService`) | never | never | host-wide + child preset scoped, minus both delegation groups |

- The team plugin registers its `team_*` tool factory with `scope=TEAM_PRESET_ID`, so the host-wide
  registry never contains team tools; `tools.registry_for(workspace, scope="team")` does.
- `AgentsService.create()` filters the resolved registry defensively, so the split also holds when a
  caller passes an explicit `registry=`: team mode removes `TEAM_MODE_DISABLED_TOOLS` (`subagent`),
  every other mode removes `TEAM_TOOL_NAMES`. Filtering is non-mutating
  (`ToolRegistry.without(names)` returns a narrow copy); the caller's registry is untouched.
- `TeamService._create_agent()` further strips `CHILD_DISABLED_TOOLS` (`team_*` + `subagent`) from the
  registry handed to a member agent, so a teammate can never spawn another team or a one-shot
  subagent. A registry double with no `without()` method and no named tools is passed through
  unchanged (it is the host's job to keep it honest). This guarantee covers the configured
  `registry`; if a host configures no registry at all it must make the preset-scoped registry
  child-safe itself.

```python
from deeppyro.api import TEAM_PRESET_ID

agent = agents.create(..., preset=TEAM_PRESET_ID)   # explicit team mode
assert agent.preset_spec.mode == "team"
```

## 3. Host-assigned revisions

```python
spec = presets.add(PresetSpec(id="reviewer", name="Reviewer", model="reviewer-model"))
spec.revision            # 1 (default preset is revision 0)
```

- `add()` always ignores any `revision` on the incoming spec and assigns the next value of a
  host-wide monotonic counter. Registering an existing id **no longer raises**; it adds a revision
  and makes it the latest.
- `add()` returns a `remove()` callable that unregisters **that one revision** (and disposes its
  realm). Use `await presets.remove_revision(id, revision)` when teardown must be awaited.
- Removing the latest revision makes the previous revision latest again; removing the last one
  removes the preset id entirely.
- Lookups:
  - `resolve(id=None, *, revision=None)` – latest revision, or one exact revision. Unknown ids and
    unknown revisions raise `PluginDependencyError`.
  - `resolve_revision(id, revision)` – alias for `resolve(id, revision=revision)`.
  - `revision_of(id)` – latest revision number or `None`; `revisions(id)` – all live revisions.
  - `get(id)`, `latest(id)`, `default()`, `all()` (latest revision per id, sorted by
    `(order, id)`), `ids()`.
- The `default` preset always exists at revision 0 and never needs realm activation.

## 4. Declaring realm rows

```python
from deeppyro.api import PresetPluginRow, PresetSpec

ctx.register_preset(PresetSpec(
    id="reviewer",
    model="reviewer-model",
    plugins=(
        PresetPluginRow(id="review-tools", plugin="review_tools.py:ReviewTools"),
        PresetPluginRow(id="audit", plugin="deeppyro.plugins.audit:Audit", config={"strict": True}),
    ),
    base=Path(__file__).parent,
))
```

- `PresetPluginRow.plugin` uses the same target syntax as bundle rows: `module:Class`, a dotted
  `module`, or a relative `*.py` path resolved against `PresetSpec.base` (normally the bundle
  directory). `resolve_row()` from `deeppyro.kernel.composition` is reused; preset realms do not
  use a second resolver.
- `config` is validated with the plugin's `Config` model exactly like the kernel does.
- `disabled=True` skips a row without failing the realm.
- `isolate` lists cordis service names that the row mounts in its own isolation layer
  (`ctx.isolate(name)`), mirroring bundle `isolate` rows.

## 5. Activation: schedule, then await

Plugin `setup()` is synchronous, so realm mounting starts eagerly as a task when the revision is
registered (`add()`) or first resolved. Registrations are not guaranteed to be visible until that
task completes. Hosts **must** await materialization before creating an agent for a preset that
carries plugin rows:

```python
app = await boot(cfg, bundle_paths=[...])
await app.service("presets").ensure_all()   # or ensure("reviewer") / ensure("reviewer", revision=3)
agent = agents.create(..., preset="reviewer")
```

- `ensure(id=None, *, revision=None)` is idempotent and shared: concurrent callers await the same
  mount.
- `is_materialized(id, revision=None)` reports readiness (presets without rows are always
  materialized).
- `agents.create()` refuses presets with unmaterialized rows with a `PluginDependencyError`
  telling the caller to await `presets.ensure()`; this turns a silent missing-tool bug into an
  explicit contract error.
- `materialized_revisions()` lists realm keys for diagnostics.

## 6. Contribution visibility

Registrations coming from a realm `PluginContext` are pinned to the preset id and the revision of
the realm. Registrations made by ordinary plugins stay unscoped.

| contribution | `scope=None` | `scope=<id>`, `revision=None` | `scope=<id>, revision=<r>` |
|---|---|---|---|
| unscoped | visible | visible | visible |
| scoped `<id>`, revision `None` (legacy) | hidden | visible | visible |
| scoped `<id>`, revision `<r>` | hidden | hidden | visible only for that `r` |

- `PluginContext.register_tool()` / `register_prompt_section()` take `scope=None` and derive it via
  `contribution_scope()`: inside a realm an explicit `scope` equal to the realm preset keeps the
  realm revision; an explicit different scope is treated as a legacy unscoped-revision
  contribution; an omitted scope inherits the realm scope and revision.
- `tools.registry_for(workspace, scope=..., revision=...)` and
  `prompt.sections(...)/build(..., scope=..., revision=...)` implement the table above.
- Names do not collide across realms:
  - Tools: duplicate tool names are resolved **host-first** (registration order; the first tool
    wins) and later duplicates are logged at debug level. A realm tool may therefore share a name
    with a host tool without breaking the registry; the realm contributes nothing for that name.
  - Prompt sections: a duplicate is rejected only when `(name, scope, revision)` is identical, so
    the same logical section can exist in several revisions and only the pinned one renders.

## 7. Failure isolation

Realm rows are mounted one by one. If a row fails (bad target, import error, `Config`
validation, `setup()` exception), the failure is recorded as a `RealmFailure` and the mount
continues with the next row; the host boot is never failed by a preset realm:

```python
failures = presets.realm_failures("reviewer")   # [RealmFailure(preset_id, revision, row_id, plugin, error)]
```

- A partially loaded fiber is disposed together with the healthy rows on removal/teardown.
- Realm fibers are children of the `presets` plugin fiber, so unloading the plugin disposes every
  realm even if it was never explicitly removed.
- Removing a revision cancels a pending mount task and disposes its fibers in reverse order.

## 8. Agents: frozen snapshots

```python
agent = agents.create(
    session=session, provider=provider, sink=sink, cwd=workspace,
    preset="reviewer",              # or preset_revision=3 to pin one revision
    # registry omitted: built from tools.registry_for(cwd, scope=id, revision=rev)
)
agent.preset            # "reviewer"
agent.preset_revision   # 3
agent.preset_spec       # the immutable PresetSpec used
```

- `create()` resolves the latest revision unless the caller passes `preset_revision=N`; it snapshots
  model/instructions, the tool registry and the prompt builder for that revision. No re-resolution
  happens during turns, so edits only affect **new** agents.
- Passing an explicit `registry` keeps working (backward compatible); omitting it uses the
  revision-filtered registry from the `tools` service. In both paths the registry is narrowed by the
  preset's `mode` (§2): a default-mode agent never sees `team_*`, a team-mode agent never sees
  `subagent`.
- `agent.preset_spec.mode` is the snapshot the filtering used; it does not change for a live agent.
- The preset id (not the revision) lands in the session header's `agentPreset` field, matching the
  existing session contract.

## 9. Host integration notes (TUI / runtime)

- After boot, `await presets.ensure_all()` before showing "new agent" affordances or creating
  agents. Awaiting only the preset the user picked is enough.
- To reproduce a session faithfully after a restart, persist the revision alongside the preset id
  and pass it back through `agents.create(preset_revision=...)`; if that revision was removed,
  fall back to the latest and surface the change. Session headers only record the preset the session
  was created with, so the runtime writes the last valid selection as an `agent-preset/selected`
  session record: `{preset, mode, revision?, previous?}` (revision omitted when unpinned, previous
  only in team mode). On resume the last record wins over the header; a team record also restores the
  remembered preset used by the next `/team off`.
- The TUI runtime exposes `set_agent_preset(id)` and `set_team_mode(enabled)`; the slash commands are
  thin wrappers. Direct `agents.create(preset=TEAM_PRESET_ID)` remains valid for non-TUI hosts, but
  a host that offers the team toggle should use `set_team_mode()` so the previous preset and the
  live-member guard are enforced.
- Editing UI: registering a new revision via `presets.add()` is the supported "edit" operation;
  it returns a `remove()` for rollback.

## 10. Deviations and known limits

- **Realm service isolation is opt-in.** Only service names listed in `PresetPluginRow.isolate`
  get their own cordis isolation layer. Other services a realm row contributes (commands, LLM
  providers, arbitrary services) are registered in the host realm and are not filtered by preset
  scope; tools and prompt sections are the fully scoped contribution types in this iteration.
- Realm activation is asynchronous and explicit; there is no blocking "activate on boot" pass
  inside the preset plugin.
- Tool-name collisions are host-first and silent apart from a debug log; the realm's version is
  not reachable while the host tool has the same name.
- A preset revision is global process state, not persisted per session, so two sessions started at
  different times may use different revisions of the same preset id unless the host pins the
  revision.
- Mode-based registry filtering uses `ToolRegistry.without()`. A custom registry object without that
  method (or iterable named tools) is passed through unchanged by `AgentsService.create()`; the mode
  split is guaranteed for the real `ToolRegistry` and the built-in team service, not for arbitrary
  duck-typed doubles.
- The built-in `default` and `team` presets are registered at boot at revision 0 and are not exposed
  through the revision counter, so `presets.remove_revision()` cannot delete them.
- `PresetSpec.plugin_id` records the registering plugin (used for display/ownership only); it does
  not affect revision numbering, which is host-wide and monotonic.

## 11. API additions at a glance

`api/types.py`: `PresetPluginRow`, `PresetSpec.plugins/revision/base/mode/hidden`,
`PromptSection.revision`, `ToolContribution.revision`, the `AGENT_MODE_*` / `*_TOOL_NAMES`
constants (including `CHILD_DISABLED_TOOLS`).
`api/context.py`: `PluginContext(default_scope=..., default_revision=...)`,
`contribution_scope()`.
`builtin/presets.py`: revision registry (`add/resolve/resolve_revision/revision_of/revisions`),
realm lifecycle (`ensure/ensure_all/is_materialized/realm_failures/materialized_revisions/
remove_revision`), `RealmFailure`, the built-in `default` and `team` presets, `visible()`.
`builtin/tools.py` + `builtin/prompt_service.py`: revision-filtered
`registry_for()/sections()/build()`.
`builtin/agents.py`: `create(..., preset_revision=..., registry=None)`, mode-based registry
filtering, `preset.preset_revision`, `preset.preset_spec.mode`.
`tools/registry.py`: non-mutating `ToolRegistry.without(names)`.
`builtin/team.py`: team tool factory scoped to `TEAM_PRESET_ID`.
`team/manager.py`: member registries strip `CHILD_DISABLED_TOOLS`.

## 12. Tests

- `tests/builtin/test_preset_revisions.py` – revision immutability, scope/revision filtering,
  live-agent snapshots, row failure isolation, host-first collisions, revision removal, the two
  built-in presets/modes/hidden flag, `visible()`, and mode-based delegation-tool filtering.
- `tests/tui/test_runtime_presets.py` – session preset records and resume (including team mode and
  its previous preset), atomic switch/rollback, busy rejection, live-member guard, hidden-team
  rejection, and `visible()` filtering.
- `tests/tui/test_app.py` – `/preset team` rejection and `/team on`/`/team off` on the current session.
- `tests/team/test_team_plugin.py` – team tools are registered with `scope="team"` and stay out of
  the host-wide registry.
- `tests/team/test_team_service.py` – member agents receive a registry without `team_*`/`subagent`.
- `tests/tools/test_registry.py` – `ToolRegistry.without()` is non-mutating and order-preserving.
- `tests/integration/test_agent_presets.py` – end-to-end scope test plus realm activation,
  `ensure_all()` requirement and old/new agent revision behavior.
