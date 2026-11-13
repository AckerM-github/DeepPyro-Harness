# TUI plugin slots

Status: implemented in layer E. The TUI is a **slot host**: plugins add status
items, conversation rows, panels and settings pages without editing
`tui/app.py` or the core widgets.

| File | Role |
|---|---|
| `src/deeppyro/tui/slots.py` | Textual-free registry, `SlotsPlugin` kernel service, conversation-row helper, context builder, theme/locale access |
| `src/deeppyro/tui/ui_package.py` | declarative `ui.toml` loader for installed bundles (lazy `module:function` imports, theme, locale) |
| `src/deeppyro/tui/widgets/plugin_slots.py` | `PluginStatusBar`, `PluginPanelHost`, `PluginTable`, `open_panel_for` |
| `src/deeppyro/tui/screens/plugin_panel.py` | `PluginPanelScreen` modal and `open_plugin_panel` helper |
| `tests/tui/test_slots.py`, `tests/tui/test_plugin_slots.py`, `tests/tui/test_ui_package.py` | registry/service/helper tests, tiny Textual host apps, and `ui.toml` loader tests |
| `src/deeppyro/tui/app.py`, `theme.tcss` | **not modified here**; the Lead applies the checklist in section 7 |

`slots.py` imports only `deeppyro.api` and `deeppyro.core.errors`, so it can be
loaded by the kernel before any Textual app exists. Widgets are the only
Textual-aware part.

## 1. Slot kinds

```python
STATUS = "status"          # one status line, rendered by PluginStatusBar
CONVERSATION = "conversation"  # one widget per matching agent event
PANEL = "panel"            # modal page, rendered by PluginPanelScreen
SETTINGS = "settings"      # same host as panel, different namespace
```

Every kind is an ordered collection. The order key is
`(order, id)` inside one kind; `SlotRegistry.list()` sorts all kinds by
`(kind, order, id)` (kind order is the lexical order above). Lower `order`
renders first; the default is `DEFAULT_ORDER = 100`.

## 2. Contract

### 2.1 SlotContribution

```python
@dataclass(frozen=True, slots=True)
class SlotContribution:
    kind: SlotKind                 # status | conversation | panel | settings
    id: str                        # stable identity within (kind, id)
    plugin_id: str                 # owner, used in error messages
    order: int
    factory: Callable[..., Any]
    predicate: Callable[[dict[str, Any]], bool] | None = None
    title: str = ""                # panel/settings title
    metadata: Mapping[str, Any] = field(default_factory=dict)
```

Factories are called through `call_slot_factory(factory, context)`: if the
callable declares a positional parameter it receives the context/event, else
it is called with no arguments. Both styles are supported:

```python
slots.add_status_item(lambda context: f"model: {context['model']}")
slots.add_status_item(lambda: "static")
```

### 2.2 SlotRegistry

```python
SlotRegistry(*, plugin_id: str = "")
```

Registration methods all return an idempotent `dispose()` callable:

| Method | Factory contract |
|---|---|
| `add_status_item(renderer, order=100, *, id=None, plugin_id="", title="", metadata=None)` | `renderer(context) -> str \| Text \| None` |
| `add_conversation_row(predicate, factory, order=100, *, id=None, plugin_id="", title="", metadata=None)` | `predicate(event) -> bool`; `factory(event) -> Widget \| None` |
| `add_panel(title, factory, order=100, *, id=None, plugin_id="", metadata=None)` | `factory(context) -> Widget \| str \| Text \| Iterable[Widget] \| None` |
| `add_settings_page(title, factory, order=100, *, id=None, plugin_id="", metadata=None)` | same as panel |

Queries:

```python
registry.list(kind=None)      # list[SlotContribution], sorted (kind, order, id)
registry.status_items()       # list(STATUS)
registry.conversation_rows()  # list(CONVERSATION)
registry.panels()             # list(PANEL)
registry.settings_pages()     # list(SETTINGS)
registry.get(kind, id)        # SlotContribution | None
registry.clear()              # host shutdown / tests
len(registry); (kind, id) in registry
```

Validation happens at registration: a non-callable factory, a non-int order
(`bool` is rejected too), an empty id, or a duplicate `(kind, id)` raises
`SlotError` (a `DeeppyroError`). A duplicate is an error rather than a
silent overwrite so two plugins cannot shadow each other. The exception text
names both owners, for example:

```text
duplicate status slot id 'clock': owned by 'plugin-a', also registered by 'plugin-b'
```

`SlotRegistry` uses an `RLock` around mutations/reads. Registration normally
happens on the kernel boot thread, but a plugin may add items from a worker;
reads are safe.

### 2.3 Ids and ownership

`default_slot_id(kind, factory, title="")`:

- `panel`/`settings`: a slug of the title (`"My Panel!"` -> `"my-panel"`);
- other kinds: `module.qualname`; a lambda appends `:co_firstlineno`
  so two lambdas in one module do not collide accidentally.

The owner is resolved in this order:

1. explicit `plugin_id=`;
2. the registry's `plugin_id` (the service registry has `"slots"`);
3. `factory.__module__`.

Because a bare service cannot see the calling fiber, **plugins should pass
both a stable `id` and `plugin_id=ctx.plugin_id`**. Otherwise all unnamed
contributions are attributed to `"slots"` (or the defining module), which
makes error messages and diagnostics less useful.

Disposal only removes the exact registration that was returned. Calling a
stale dispose after a new contribution reused the same `(kind, id)` is a
no-op, and calling dispose twice is safe.

### 2.4 Failure isolation

- `PluginStatusBar`: one failing renderer becomes a dim `!`; the rest of the
  line still renders.
- `PluginPanelHost`: a factory exception, a mount failure, `None`, a string,
  a Rich `Text` and an iterable are all handled; failures become an inline
  `.plugin-slot-error` line instead of crashing the modal.
- `apply_conversation_slots`: a predicate or factory exception is reported
  through `conversation.add_error(...)` and the next row continues. A broken
  plugin can never kill the turn.
- `resolve_slot_registry`: a missing service or a missing runtime returns
  `None`; the host runs without slots.

### 2.5 Theme tokens and locale

Two auxiliary maps live on the same registry and are shared by every plugin:

```python
registry.set_theme({"accent": "#7aa2f7"})   # replaces the whole map
registry.theme() -> dict[str, str]          # copy
registry.add_locale({"acme.greeting": "Hello"})  # merge
registry.locale() -> dict[str, str]         # copy
registry.translate("acme.greeting", default=None) -> str
```

- Values must be strings; malformed maps raise `SlotError`.
- `translate` returns the mapped string, else `default`, else the key. It does
  **not** interpret `"!key"` indirection or do pluralisation; keep it a flat
  lookup so both hosts and plugins have one predictable rule.
- `build_slot_context` exposes `theme`, `locale` and `translate` (section 4).
- `clear()` clears slots, theme and locale.

## 3. Kernel service: SlotsPlugin

`SlotsPlugin` is a normal `deeppyro.api.Plugin` that provides the shared
registry as the cordis service `slots`:

```python
class SlotsPlugin(Plugin):
    plugin_id = "slots"
    version = "0.1.0"
    inject = []

    def __init__(self, ctx, config):
        super().__init__(ctx, config)
        self.registry = SlotRegistry(plugin_id=self.plugin_id)

    def setup(self, ctx, config):
        ctx.provide("slots", self.registry)
        self.load_ui_packages()   # scans <home>/bundles/*/ui.toml, errors recorded
```

`ctx.provide` goes through the vendored cordis `Context.provide`, so the
service is registered immediately and unregistered with the plugin fiber. A
real-kernel probe (external bundle row below) returned a `SlotRegistry` with
`app.started["slots"] == "ACTIVE"`.

Add one row to `src/deeppyro/bundles/core/bundle.toml` (recommended place:
after `commands-builtin`, before `agents`; it has no dependencies):

```toml
[[plugins]]
id = "slots"
plugin = "deeppyro.tui.slots:SlotsPlugin"
```

The core bundle is `critical = true`. A plugin author may instead put the same
row in an external bundle (non-critical): boot continues without slots if it
fails, and `resolve_slot_registry` returns `None`. Do not mount two rows that
both provide `slots`.

### Writing a slot plugin

```python
from deeppyro.api import Plugin

class ClockPlugin(Plugin):
    plugin_id = "clock"
    version = "0.1.0"
    inject = ["slots"]

    def setup(self, ctx, config):
        slots = ctx.service("slots")
        dispose = slots.add_status_item(
            lambda context: f"clock: {context['model']}",
            id=f"{self.plugin_id}.status",
            plugin_id=self.plugin_id,
        )
        # Tie the contribution to the plugin fiber; unloading the plugin
        # removes the item even though the registry is a plain object.
        ctx.effect(dispose, label="clock-status")
```

## 4. Host context

`build_slot_context(app) -> dict[str, Any]` is the mapping passed to status,
panel and settings factories:

| Key | Value |
|---|---|
| `app` | the `DeepPyroApp` instance |
| `runtime` | `runtime` object (may be `None` before boot) |
| `cwd` | workspace path as `str` |
| `session_id` | active session id |
| `provider` | provider name |
| `model` | model id |
| `phase` | `app._phase` or `"idle"` (`idle`, `thinking`, `tool`, `done`) |
| `input_tokens`, `output_tokens`, `cached_tokens` | cumulative session usage |
| `context_window` | active model context window |
| `usage` | `{"input": int, "output": int, "cached": int}` |
| `busy` | `app.busy` |
| `theme` | copied theme token map from the host registry (empty without one) |
| `locale` | copied translation map from the host registry |
| `translate` | `registry.translate(key, default=None)`, identity fallback without a registry |

The builder is defensive: a broken `runtime.session_usage()` yields zeroes,
missing attributes become empty strings/zero, and it never raises. Status and
panel factories should treat every key as optional and keep rendering cheap;
`PluginStatusBar.refresh_items()` is called on every agent event via
`refresh_status`.

Conversation rows intentionally receive the **agent event**, not the host
context. The optional `context=` argument of `apply_conversation_slots` is
reserved for a future context-aware row contract and is currently unused.

## 5. Conversation events

`apply_conversation_slots(conversation, event, registry, *, context=None)`
walks `registry.conversation_rows()` in order. For each row:

1. call `predicate(event)` (a row without predicate always matches);
2. call `factory(event)`;
3. skip `None`, otherwise `conversation.mount(widget)`;
4. on any exception call
   `conversation.add_error("plugin slot <owner>/<id> failed: <Type>: <message>")`
   and continue.

It returns the mounted widgets (useful for tests). `registry=None` is a
no-op, so the host can call it unconditionally.

Event shapes are the ones in `docs/spec/INTERFACES.md` section 7:
`session`, `status`, `text`, `thinking`, `tool_call`, `tool_result`,
`usage`, `final`, `error`. Example row:

```python
slots.add_conversation_row(
    lambda event: event.get("type") == "tool_result" and event.get("isError"),
    lambda event: Static(f"tool {event['callId']} failed"),
    order=10,
    id=f"{plugin_id}.tool-errors",
    plugin_id=plugin_id,
)
```

The helper is deliberately external to `ConversationView`; the Lead adds one
call in `handle_event` instead of editing the widget.

## 6. Host widgets

### PluginStatusBar

- `PluginStatusBar(registry=None, context_factory=None, *, id="plugin-status-bar")`
- `bind(registry, context_factory=None)` after the runtime is booted.
- `refresh_items(context=None)`: re-render now; with no explicit context it
  calls the context factory (exceptions -> `{}`).
- `render_text(context)`: pure render seam used by tests.
- `plain_text` property.
- Starts with `display = False`; becomes visible only when at least one item
  renders non-empty text. Items are joined with `"  ·  "`.
- A failing item contributes a dim `!`; it never hides other items.

### PluginPanelHost

- `PluginPanelHost(contribution=None, *, factory=None, context=None)`
- `load(context=None)` clears and mounts the result; all errors are inline.
- Supports `Widget`, `str`, Rich `Text`, an iterable of widgets, and `None`
  ("empty panel").
- **Do not name a widget attribute `_context`**: Textual's `MessagePump`
  defines `_context()`, and shadowing it hangs the mount forever. The host
  uses `_slot_context`; this was the root cause of a boot hang during
  development.

### PluginPanelScreen

- `PluginPanelScreen(contribution=None, *, factory=None, title=None, context=None)`
- `ModalScreen[None]`; title defaults to the contribution title/id; Escape
  closes; factory errors are handled by the embedded host.
- `open_plugin_panel(app, registry, *, kind=PANEL, slot_id=None, context=None)`
  picks the first panel or an exact id, pushes the screen, and returns it
  (`None` when no contribution matches).

### PluginTable

- `PluginTable(contributions=(), *, kind=PANEL, id="plugin-table")`
- `OptionList` of panel/settings titles; Enter posts
  `PluginTable.Selected(contribution)`.
- `set_contributions(items)` replaces the list; `open_panel_for(registry, *,
  kind=PANEL, slot_id=None)` is the selection helper used by the modal.

## 7. Integration checklist for the Lead

All edits below are in `src/deeppyro/tui/app.py` unless stated; our file scopes
forbid editing it here.

### 7.1 Imports (after the current import block, around line 35)

```python
from deeppyro.tui.screens.plugin_panel import open_plugin_panel  # optional, section 7.8
from deeppyro.tui.slots import (
    SlotRegistry,
    apply_conversation_slots,
    build_slot_context,
    resolve_slot_registry,
)
from deeppyro.tui.widgets.plugin_slots import PluginStatusBar
```

Remove the optional import if the panel command is not wired; ruff would flag
unused imports.

### 7.2 `DeepPyroApp.__init__` (around line 68, next to `self._phase`)

```python
self._slots: SlotRegistry | None = None
```

### 7.3 `compose` (line 77-83)

Add one line directly after the existing `StatusBar` yield:

```python
        yield StatusBar()
        yield PluginStatusBar()
```

`PluginStatusBar` is height 1 and hides itself when empty, so no layout
change is needed when no plugin contributes a status item.

### 7.4 `on_mount` (after the runtime is assigned, around line 118)

```python
        self._slots = resolve_slot_registry(self.runtime)
        try:
            self.query_one(PluginStatusBar).bind(
                self._slots,
                lambda: build_slot_context(self),
            )
        except Exception:  # noqa: BLE001 - optional chrome must not block boot
            pass
```

Place it after the `try/except` that boots the runtime (line 100-117). When
boot fails, `self.runtime` is `None` and `resolve_slot_registry(None)` returns
`None`; the bar stays hidden. Do not bind in `compose` (runtime does not
exist yet).

### 7.5 `handle_event` (line 168-199)

Add one call at the end of the existing rendering `try` block, after the
`elif kind == "status"` branch and before the `except Exception`:

```python
            apply_conversation_slots(
                conversation,
                event,
                self._slots,
                context=build_slot_context(self),
            )
```

The helper already isolates every plugin exception. Keep it inside the
existing `try` so a plugin cannot break the built-in rendering path either.

### 7.6 `refresh_status` (line 291-326)

Add a private helper and call it once at the start of `refresh_status` (before
the core `StatusBar` query, so it also runs for the `runtime is None` boot
placeholder):

```python
    def _refresh_plugin_status(self) -> None:
        try:
            bar = self.query_one(PluginStatusBar)
        except Exception:  # noqa: BLE001 - composition not ready yet
            return
        bar.refresh_items(build_slot_context(self))

    def refresh_status(self) -> None:
        self._refresh_plugin_status()
        try:
            status = self.query_one(StatusBar)
        except Exception:
            return
        ...
```

This is the only update path for status items; no timer is needed because
`handle_event` and every policy/session command already call
`refresh_status()`.

### 7.7 Theme CSS (`src/deeppyro/tui/theme.tcss`)

Widgets have safe default CSS, but these selectors make them match the theme
(the custom palette does not define `$text-muted`/`$text-error`, so without
overrides the defaults use Textual's standard tokens):

```css
#plugin-status-bar {
    height: 1;
    padding: 0 2;
    background: $surface;
    color: $dim;
    text-wrap: nowrap;
    text-overflow: ellipsis;
    overflow: hidden;
}

#plugin-panel-dialog {
    border: round $accent;
    background: $surface;
}

#plugin-panel-title {
    color: $accent;
}

.plugin-slot-error {
    color: $red;
}

#plugin-table {
    height: auto;
    max-height: 18;
}
```

`PluginPanelScreen` already centers itself and dims the background with
`$background 70%`; override to `rgba(15, 17, 21, 0.82)` if it must match
`ApprovalScreen` exactly.

### 7.8 Opening a panel (optional host command)

```python
    async def show_plugin_panel(self, argument: str) -> None:
        screen = open_plugin_panel(
            self,
            self._slots,
            slot_id=argument or None,
            context=build_slot_context(self),
        )
        if screen is None:
            self.conversation.add_info("no plugin panels")
```

Wire it to a slash command only if the command table is extended; this layer
does not add a built-in command. Alternatively, call `open_plugin_panel` from
`show_plugins()` when the plugin manager screen is absent.

### 7.9 Core bundle row (section 3)

Add the `[[plugins]] id = "slots"` row to
`src/deeppyro/bundles/core/bundle.toml`. Without it, every integration point
above is inert but safe (`self._slots` is `None`, bar hidden, helper no-op).

## 8. Tests

```powershell
uv run pytest tests/tui/test_slots.py tests/tui/test_plugin_slots.py tests/tui/test_ui_package.py -o addopts= -q
# 50 passed

uv run pytest tests/tui -o addopts= -q
# full suite; add test_app.py etc. once the Lead's host integration lands
```

`test_slots.py` covers ordering/filtering, duplicate detection, idempotent
dispose, default ids, context factory, `SlotsPlugin` service provision, context
building, theme/locale access, conversation-row mounting and error isolation,
and registry resolution. `test_plugin_slots.py` uses tiny `App.run_test` hosts
for the status bar (items, errors, Rich Text, missing context), panel host
(widget, string, `None`, factory/mount errors), the modal (title, Escape,
factory error), `open_plugin_panel`, and `PluginTable` selection.
`test_ui_package.py` covers all four sections, theme/locale, `module:function`
lazy imports (real `sys.path` and an injected importer), broken entries,
malformed TOML, missing packages, installed-bundle scanning and
`SlotsPlugin` boot diagnostics.

## 9. Deviations, limits, risks

- **No command is wired to panels in this layer.** The modal and helper are
  ready; adding `/panel` requires touching `commands.py`/the core command
  table, which is outside the task-10 write scope.
- **Ownership is caller-declared.** A registry service cannot infer the
  calling fiber, so omitted `plugin_id` falls back to `"slots"` or the factory
  module. Plugins must pass `plugin_id=ctx.plugin_id` and should tie
  `dispose` to `ctx.effect`.
- **No automatic re-render on registration.** Adding an item after boot does
  not refresh the bar until the next `refresh_status()`; a plugin that adds
  chrome asynchronously should ask the host to refresh or wait for the next
  event.
- **Conversation rows are append-only.** They can add a widget to the live
  stream but cannot replace or remove core rows. History replay
  (`_render_history`) does not call `apply_conversation_slots`; plugin rows
  appear only for events produced live after boot.
- **Status line width.** Long items are clipped by the bar (`overflow:
  hidden`); plugins should keep items short and let the core status bar own
  the primary diagnostics.
- **Panel factory timing.** `PluginPanelHost` mounts synchronously in
  `on_mount`; a factory returning a widget that needs a running app should be
  defensive. Never assign a widget attribute named `_context` (section 6).
- **No test boots a real kernel in this layer.** `SlotsPlugin` is tested with
  a fake context and was additionally probed against a real `boot()` with an
  external bundle row (service returned `SlotRegistry`, state `ACTIVE`).
  A committed boot test would depend on the Lead's final bundle row id.


## 10. Declarative UI packages (`ui.toml`)

The plugin service is the imperative path; a bundle can also declare UI in
`ui.toml` and never import Textual. This mirrors the DSH client-plugin package
format: package metadata stays declarative, factories are named by
`module:function`, and the host owns rendering.

### 10.1 Discovery and bundle layout

```text
~/.dpro/bundles/acme/          # deeppyro_home() / "bundles"
    bundle.toml                # [bundle] id = "acme"
    ui.toml
    acme_ui.py
    locale/en.json             # optional
```

`default_ui_bundles_root()` returns `deeppyro_home() / "bundles"`
(`DEEPPYRO_HOME` overrides `~/.dpro`). The loader scans only direct child
directories that contain `ui.toml`, sorted by directory name. A directory
without `ui.toml` is ignored (a normal bundle with no UI is valid). The bundle
id comes from the sibling `[bundle].id` in `bundle.toml`; a missing or
malformed `bundle.toml` is recorded as an error and the directory name is used
as a fallback so the rest of the package can still load.

`load_ui_package` accepts either the bundle directory or the `ui.toml` file.

### 10.2 Schema

Every section is an array of tables; the only allowed sections are `status`,
`conversation`, `panels`, `settings`, plus the `[theme]` table and the
optional `locale/en.json` file.

| Section | Key | Required | Default | Meaning |
|---|---|---|---|---|
| `status` | `id` | yes | - | unique id within `status` |
| | `renderer` | yes | - | `module:function`; `renderer(context) -> str \| Text \| None` |
| | `order` | no | `100` | lower renders first |
| `conversation` | `id` | yes | - | unique id within `conversation` |
| | `factory` | yes | - | `module:function`; `factory(event) -> Widget \| None` |
| | `predicate` | no | match all | `module:function`; `predicate(event) -> bool` |
| | `order` | no | `100` | evaluation order |
| `panels` | `id` | yes | - | unique id within `panels` |
| | `factory` | yes | - | `factory(context) -> Widget \| str \| Text \| Iterable \| None` |
| | `title` | no | `id` | modal title / table label |
| | `order` | no | `100` | list order |
| `settings` | `id` | yes | - | same contract as `panels`, separate namespace |
| | `factory` | yes | - | same as panel |
| | `title` | no | `id` | list label |
| | `order` | no | `100` | list order |

Validation is per entry: a bad `order`, missing `id`, unknown/missing target,
non-callable target or duplicate `(kind, id)` records an error and skips only
that entry. Unknown top-level keys and unknown entry keys are currently
ignored; they are reserved for future format revisions.

`[theme]` is a flat table of string tokens. `locale/en.json` is a flat JSON
object of string keys to string values. Neither is interpreted by the loader
beyond storing it in the registry.

### 10.3 Loader API

```python
from deeppyro.tui.ui_package import (
    UIPackageError,
    UIPackageReport,
    default_ui_bundles_root,
    load_installed_ui_packages,
    load_ui_package,
    load_ui_package_report,
)
```

```python
def load_ui_package(
    path: str | Path,
    registry: SlotRegistry,
    *,
    import_module: Callable[[str], ModuleType] | None = None,
) -> list[str]: ...

def load_ui_package_report(
    path: str | Path,
    registry: SlotRegistry,
    *,
    import_module: Callable[[str], ModuleType] | None = None,
) -> UIPackageReport: ...

def load_installed_ui_packages(
    registry: SlotRegistry,
    *,
    root: str | Path | None = None,
    import_module: Callable[[str], ModuleType] | None = None,
) -> list[UIPackageReport]: ...

def default_ui_bundles_root() -> Path: ...
```

`load_ui_package` is the convenience wrapper used by callers that only need
the registered ids; it returns `report.slot_ids` in declaration order
(status, then conversation, then panels, then settings). It **never raises for
a broken package**. `UIPackageError` is reserved for programmer errors such as
passing a non-`SlotRegistry` registry.

```python
@dataclass(frozen=True, slots=True)
class UIPackageReport:
    id: str                    # bundle id, or directory-name fallback
    path: Path                 # the ui.toml that was read
    slot_ids: tuple[str, ...]  # successfully registered ids
    errors: tuple[str, ...]    # empty means clean load

    @property
    def ok(self) -> bool: ...
    def summary(self) -> str: ...   # "acme: 3 slot(s), ok"
```

`load_installed_ui_packages` returns one report per bundle with a `ui.toml`,
in directory-name order. A missing or empty root returns `[]`. Any unexpected
exception while loading one package is converted into a report error; the next
package is still loaded.

### 10.4 Imports are lazy, factories run later

For every `module:function` spec the loader imports the module while the
package is being loaded, resolves the attribute, and checks that it is
callable. The callable itself is **not called**; it runs only when the host
renders a status line, handles a matching event, or opens a panel. This means a
typo in a factory name is reported at boot, while a runtime error inside a
factory is handled by the same failure isolation as an imperative plugin.

The package's base directory is temporarily prepended to `sys.path` for the
duration of the load (and removed in `finally` cleanly), so a bundle can import
its own sibling modules:

```text
ui.toml:  renderer = "acme_ui:clock"
acme_ui.py: def clock(context): ...
```

If the caller passes `import_module=`, the loader uses that callable instead
and never touches `sys.path`. Tests use this seam; embedders can use it to
load packages from an in-memory/zip source. Both dotted attributes
(`"acme_ui:widgets.clock"`) and module imports that pull in further siblings
are supported.

### 10.5 Failure isolation

| Failure | Result |
|---|---|
| missing package / `ui.toml` | report error, no slots; never raises |
| malformed TOML | report error for the file; no slots from that package |
| missing `bundle.toml` | directory-name id, no error |
| malformed `bundle.toml` | report error, directory-name id, rest still loads |
| section not an array | report error, that section skipped |
| entry not a table / bad `id` / bad `order` | error for that entry, next entry continues |
| `module:function` syntax invalid | error for that entry |
| module import fails | error for that entry; other entries still load |
| target missing or not callable | error for that entry |
| duplicate `(kind, id)` | registry `SlotError` captured; first registration wins |
| bad `[theme]` value / bad locale JSON | error recorded; package slots still load |

`SlotsPlugin.setup` calls `load_ui_packages()` after `ctx.provide`. Even a
scan-level failure is caught and appended to `ui_errors`; boot never fails
because of a UI package.

```python
plugin.ui_packages  # list[UIPackageReport]
plugin.ui_errors    # list[str]: scan errors + every report error
plugin.ui_summary   # str: "no UI packages" or "id: N slot(s), ok; ..."
plugin.load_ui_packages(bundles_dir=None)  # manual reload (tests/CLI)
```

### 10.6 Theme tokens

`[theme]` tokens are merged in scan order:
`registry.set_theme({**registry.theme(), **tokens})`. Later packages override
earlier keys; a package cannot remove a token. Values must be strings. The host
exposes the resulting map as `context["theme"]`; plugin renderers may use it
for inline Rich styles, but the core Textual widgets still use
`theme.tcss`. There is no automatic CSS-variable bridge in this layer (writing
`theme.tcss` is outside the task write scope).

### 10.7 Locale

`locale/en.json` is loaded after the theme and merged with
`registry.add_locale(...)`. Only `en` is read; other locale files are ignored
until the host grows a locale selector. Translation is a flat lookup:
`context["translate"]("acme.greeting")`. The host does **not** auto-translate
slot titles; packages should call `translate` in their own factories if they
want localized text. `"!key"` interpolation is intentionally not implemented
(keep one simple rule; see section 2.5).

### 10.8 Complete example

```text
~/.dpro/bundles/acme/
    bundle.toml
    ui.toml
    acme_ui.py
    locale/en.json
```

`bundle.toml`:

```toml
[bundle]
id = "acme"
name = "Acme UI"
version = "0.1.0"
api_version = 1
```

`ui.toml`:

```toml
[[status]]
id = "clock"
order = 50
renderer = "acme_ui:clock"

[[conversation]]
id = "todo-row"
predicate = "acme_ui:is_todo"
factory = "acme_ui:todo_row"

[[panels]]
id = "acme"
title = "Acme"
factory = "acme_ui:panel"

[[settings]]
id = "acme-settings"
title = "Acme settings"
factory = "acme_ui:settings"

[theme]
accent = "#7aa2f7"
```

`acme_ui.py`:

```python
from __future__ import annotations

from typing import Any

from textual.widgets import Static


def clock(context: dict[str, Any]) -> str:
    return f"acme: {context.get('phase', 'idle')}"


def is_todo(event: dict[str, Any]) -> bool:
    return event.get("type") == "text" and "todo" in str(event.get("text", "")).lower()


def todo_row(event: dict[str, Any]) -> Static:
    return Static(f"todo detected: {event.get('text', '')}", classes="acme-todo")


def panel(context: dict[str, Any]) -> Static:
    return Static(f"acme panel for {context.get('session_id', '')}")


def settings(context: dict[str, Any]) -> Static:
    return Static("acme settings")
```

`locale/en.json`:

```json
{"acme.greeting": "Hello"}
```

Result: `load_ui_package_report(...).slot_ids` is
`("clock", "todo-row", "acme", "acme-settings")`; all contributions are owned
by `"acme"`; `registry.theme()["accent"] == "#7aa2f7"`; and
`context["translate"]("acme.greeting") == "Hello"`.

### 10.9 Compatibility and limits

- **Backward compatible.** `SlotRegistry`'s existing methods, defaults and
  duplicate errors are unchanged; theme/locale only add state and accessors.
  Imperative plugins keep working and may mix with `ui.toml` contributions.
- **Arbitrary code at boot.** Loading a package imports its modules with the
  user's privileges. Only install bundles you trust; this is the same trust
  boundary as `[[plugins]]` in `bundle.toml`.
- **Process-global import window.** `sys.path` is modified only while one
  package loads and restored in `finally`. Concurrent loads from two threads
  could observe each other's path entries; boot is single-threaded, but a
  future hot-reload worker should serialize package loads.
- **No hot reload.** Adding/changing `ui.toml` requires a restart (or calling
  `SlotsPlugin.load_ui_packages()` again, which duplicates ids and therefore
  reports duplicate errors unless the registry is cleared first).
- **Locale is English-only.** The file name is fixed; a locale selector is a
  future host feature.
- **Id collisions are first-wins.** Directory scan order decides; later
  packages get a recorded error instead of shadowing an earlier slot.
- **Theme is last-wins.** Tokens merge globally; there is no per-package
  namespace, so use prefixed keys (`acme.accent`) for package-private tokens.
- **Diagnostics are passive.** `ui_errors` is available to the host but this
  layer does not pop a notification; a Lead integration may surface it in the
  plugin manager screen.

