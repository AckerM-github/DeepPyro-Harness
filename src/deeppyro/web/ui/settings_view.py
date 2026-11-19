"""Full-screen settings surface for the NiceGUI web UI.

The view is deliberately split in two halves:

* pure helpers at module level (section mapping, schema-to-field descriptors,
  save-state derivation, plugin value collection) that can be tested without a
  browser or a running NiceGUI server;
* SettingsView, a thin rendering shell that reads one payload from the bridge
  (deeppyro.web.ui.bridge.WebUiBridge) and writes every change back through the
  bridge. The bridge validates server side and returns plain dictionaries; this
  page never claims a save succeeded unless the bridge accepted it.

The eight sections and the CSS class names follow docs/design/nicegui-web-ui.md.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from nicegui import ui

from deeppyro.web.ui import theme
from deeppyro.web.ui.api import COMMAND_GROUPS

# -- sections -------------------------------------------------------------


class SettingsSection(StrEnum):
    """Exact URL slugs of the settings sections."""

    PROVIDER = "provider"
    AGENT = "agent"
    PERMISSIONS = "permissions"
    COMMANDS = "commands"
    PLUGINS = "plugins"
    SESSIONS = "sessions"
    WEB = "web"
    ABOUT = "about"


@dataclass(frozen=True, slots=True)
class SectionSpec:
    """Navigation metadata for one settings section."""

    slug: SettingsSection
    label: str
    icon: str
    title: str
    description: str


SECTION_SPECS: tuple[SectionSpec, ...] = (
    SectionSpec(
        slug=SettingsSection.PROVIDER,
        label="Provider",
        icon="bolt",
        title="Provider & model",
        description="Model routing, provider connections and API reachability.",
    ),
    SectionSpec(
        slug=SettingsSection.AGENT,
        label="Agent",
        icon="cpu",
        title="Agent",
        description="Sampling temperature and agent loop limits.",
    ),
    SectionSpec(
        slug=SettingsSection.PERMISSIONS,
        label="Permissions",
        icon="shield",
        title="Permissions",
        description="Sandbox and approval preset applied to new turns.",
    ),
    SectionSpec(
        slug=SettingsSection.COMMANDS,
        label="Commands",
        icon="terminal",
        title="Commands",
        description="Slash commands registered by the running plugins.",
    ),
    SectionSpec(
        slug=SettingsSection.PLUGINS,
        label="Plugins",
        icon="plugins",
        title="Plugins",
        description="Installed plugins and the settings each one declares.",
    ),
    SectionSpec(
        slug=SettingsSection.SESSIONS,
        label="Sessions",
        icon="sessions",
        title="Sessions",
        description="Recent sessions and where they are stored.",
    ),
    SectionSpec(
        slug=SettingsSection.WEB,
        label="Web",
        icon="globe",
        title="Web",
        description="Loopback host, browser access and build information.",
    ),
    SectionSpec(
        slug=SettingsSection.ABOUT,
        label="About",
        icon="info",
        title="About",
        description="Version, runtime and plugin count.",
    ),
)

DEFAULT_SECTION = SettingsSection.PROVIDER

SECTION_ALIASES: dict[str, SettingsSection] = {
    "behavior": SettingsSection.AGENT,
    "model": SettingsSection.PROVIDER,
    "models": SettingsSection.PROVIDER,
    "permission": SettingsSection.PERMISSIONS,
    "plugin": SettingsSection.PLUGINS,
    "session": SettingsSection.SESSIONS,
    "config": SettingsSection.PROVIDER,
    "": DEFAULT_SECTION,
}


def resolve_section(slug: str | None) -> SettingsSection:
    """Map a URL slug (or legacy alias) onto a settings section.

    Unknown slugs fall back to the provider section instead of failing the page.
    """
    if slug is None:
        return DEFAULT_SECTION
    text = str(slug).strip().lower()
    try:
        return SettingsSection(text)
    except ValueError:
        return SECTION_ALIASES.get(text, DEFAULT_SECTION)


def section_specs() -> tuple[SectionSpec, ...]:
    """Return the navigation order of every settings section."""
    return SECTION_SPECS


def section_spec(section: SettingsSection | str) -> SectionSpec:
    """Return the SectionSpec for a section slug or member."""
    resolved = resolve_section(section)
    for spec in SECTION_SPECS:
        if spec.slug is resolved:
            return spec
    return SECTION_SPECS[0]


# -- plugin schema to field descriptors -----------------------------------


class FieldKind(StrEnum):
    """The form control used for one plugin settings field."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"


@dataclass(frozen=True, slots=True)
class FieldOption:
    """One allowed value of an enum field."""

    value: Any
    label: str


@dataclass(frozen=True, slots=True)
class FieldDescriptor:
    """A plugin settings property rendered as a native form control."""

    name: str
    label: str
    kind: FieldKind
    default: Any = None
    description: str = ""
    options: tuple[FieldOption, ...] = ()
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    secret: bool = False


#: Property names that look like secrets. SettingsStore already masks their
#: values; the view renders them as password inputs and never echoes a value.
_SECRET_HINTS = ("key", "token", "secret", "password", "passwd", "credential")

#: JSON-schema types this surface can render as a native control.
_SUPPORTED_TYPES = {"string", "integer", "number", "boolean"}


def humanize_label(name: str) -> str:
    """Turn a snake_case schema key into a readable label."""
    words = [word for word in str(name).replace("-", "_").split("_") if word]
    if not words:
        return str(name)
    if len(words) == 1:
        return words[0].capitalize()
    return " ".join([words[0].capitalize(), *[word.lower() for word in words[1:]]])


def _property_type(prop: Mapping[str, Any]) -> str:
    """Best-effort JSON-schema type for one property."""
    raw = prop.get("type")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item != "null":
                return item
    if "enum" in prop:
        return "string"
    return ""


def _enum_options(prop: Mapping[str, Any]) -> tuple[FieldOption, ...]:
    """Build enum choices from a JSON-schema enum list."""
    raw = prop.get("enum")
    if not isinstance(raw, (list, tuple)):
        return ()
    options: list[FieldOption] = []
    seen: set[str] = set()
    for value in raw:
        if isinstance(value, (dict, list)):
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        label = str(value) if not isinstance(value, str) else humanize_label(value)
        options.append(FieldOption(value=value, label=label))
    return tuple(options)


def _numeric_bound(prop: Mapping[str, Any], key: str) -> float | None:
    """Read a numeric bound, ignoring malformed schema values."""
    raw = prop.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw)


def schema_fields(schema: Mapping[str, Any] | None) -> tuple[FieldDescriptor, ...]:
    """Describe the editable properties of one plugin schema.

    Only plain string/integer/number/boolean/enum properties become controls;
    objects, arrays and malformed definitions are reported by
    unsupported_schema_fields instead of being rendered as fake inputs.
    """
    if not isinstance(schema, Mapping):
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    raw_required = schema.get("required")
    required = set(raw_required) if isinstance(raw_required, (list, tuple, set)) else set()
    descriptors: list[FieldDescriptor] = []
    for raw_name, raw_prop in properties.items():
        if not isinstance(raw_prop, Mapping):
            continue
        name = str(raw_name)
        prop = raw_prop
        kind = _property_type(prop)
        enum_options = _enum_options(prop)
        if enum_options:
            field_kind = FieldKind.ENUM
        elif kind == "string":
            field_kind = FieldKind.STRING
        elif kind == "integer":
            field_kind = FieldKind.INTEGER
        elif kind == "number":
            field_kind = FieldKind.NUMBER
        elif kind == "boolean":
            field_kind = FieldKind.BOOLEAN
        else:
            continue
        title = prop.get("title")
        label = str(title) if isinstance(title, str) and title.strip() else humanize_label(name)
        description = prop.get("description")
        note = str(description) if isinstance(description, str) else ""
        default = prop.get("default", False if field_kind is FieldKind.BOOLEAN else None)
        descriptors.append(
            FieldDescriptor(
                name=name,
                label=label,
                kind=field_kind,
                default=default,
                description=note,
                options=enum_options,
                required=name in required,
                minimum=_numeric_bound(prop, "minimum"),
                maximum=_numeric_bound(prop, "maximum"),
                secret=field_kind is FieldKind.STRING and any(hint in name.lower() for hint in _SECRET_HINTS),
            )
        )
    return tuple(descriptors)


def unsupported_schema_fields(schema: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Property names the schema declares but this surface cannot render."""
    if not isinstance(schema, Mapping):
        return ()
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    unsupported: list[str] = []
    for raw_name, raw_prop in properties.items():
        if not isinstance(raw_prop, Mapping):
            unsupported.append(str(raw_name))
            continue
        kind = _property_type(raw_prop)
        if _enum_options(raw_prop) or kind in _SUPPORTED_TYPES:
            continue
        unsupported.append(str(raw_name))
    return tuple(unsupported)


def coerce_field_value(descriptor: FieldDescriptor, raw: Any) -> Any:
    """Validate and convert one control value for the plugin settings store."""
    if descriptor.kind is FieldKind.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in {"true", "1", "yes", "on"}:
                return True
            if text in {"false", "0", "no", "off", ""}:
                return False
        raise ValueError(descriptor.label + ": expected a boolean")
    if descriptor.kind is FieldKind.INTEGER:
        if isinstance(raw, bool):
            raise ValueError(descriptor.label + ": expected an integer")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(descriptor.label + ": expected an integer") from exc
        if not number.is_integer():
            raise ValueError(descriptor.label + ": expected an integer")
        return int(number)
    if descriptor.kind is FieldKind.NUMBER:
        if isinstance(raw, bool):
            raise ValueError(descriptor.label + ": expected a number")
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(descriptor.label + ": expected a number") from exc
    if descriptor.kind is FieldKind.ENUM:
        for option in descriptor.options:
            if raw == option.value or str(raw) == str(option.value):
                return option.value
        raise ValueError(descriptor.label + ": not one of the allowed values")
    return "" if raw is None else str(raw)


def collect_plugin_values(
    descriptors: Sequence[FieldDescriptor],
    raw_values: Mapping[str, Any],
) -> dict[str, Any]:
    """Collect the settings patch from the rendered plugin controls.

    Blank controls are omitted so the settings store keeps the existing value
    (that is how a blank masked secret input means "leave it alone"). Required
    blank controls raise ValueError so the page can show the message inline
    instead of sending a partial patch.
    """
    values: dict[str, Any] = {}
    for descriptor in descriptors:
        if descriptor.name not in raw_values:
            continue
        raw = raw_values[descriptor.name]
        blank = raw is None or (isinstance(raw, str) and not raw.strip())
        if blank:
            if descriptor.secret:
                continue
            if descriptor.required:
                raise ValueError(descriptor.label + ": a value is required")
            continue
        if descriptor.secret and isinstance(raw, str) and set(raw.strip()) <= {"*", "\u2022"}:
            continue
        values[descriptor.name] = coerce_field_value(descriptor, raw)
    return values


# -- payload projections --------------------------------------------------


@dataclass(frozen=True, slots=True)
class Choice:
    """One select option: a machine value and a human label."""

    value: str
    label: str


def choice_options(choices: Sequence[Choice]) -> dict[str, str]:
    """Convert choices into the mapping NiceGUI select widgets take."""
    return {choice.value: choice.label for choice in choices}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _text_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(text for text in (_text(item) for item in value) if text)


def provider_rows(payload: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], ...]:
    """Provider rows from the settings payload (provider section, then config)."""
    data = _mapping(payload)
    rows = _mapping(data.get("provider")).get("providers")
    if not isinstance(rows, (list, tuple)):
        rows = _mapping(data.get("config")).get("providers")
    if not isinstance(rows, (list, tuple)):
        return ()
    return tuple(row for row in rows if isinstance(row, Mapping))


def provider_choices(payload: Mapping[str, Any] | None) -> tuple[Choice, ...]:
    """Select options for the configured providers."""
    choices: list[Choice] = []
    for row in provider_rows(payload):
        name = _text(row.get("name"))
        if name:
            choices.append(Choice(value=name, label=name))
    return tuple(choices)


def current_provider(payload: Mapping[str, Any] | None) -> str:
    """Currently selected provider name, or the first configured provider."""
    data = _mapping(payload)
    current = _text(_mapping(data.get("provider")).get("current"))
    if not current:
        current = _text(_mapping(agent_values(data)).get("provider"))
    if not current:
        choices = provider_choices(data)
        current = choices[0].value if choices else ""
    return current


def model_choices(payload: Mapping[str, Any] | None, provider: str) -> tuple[Choice, ...]:
    """Select options for the models of one provider."""
    target = _text(provider)
    if not target:
        return ()
    for row in provider_rows(payload):
        if _text(row.get("name")) != target:
            continue
        models = row.get("models")
        if not isinstance(models, (list, tuple)):
            return ()
        choices: list[Choice] = []
        for raw in models:
            model = _mapping(raw)
            model_id = _text(model.get("id"))
            if not model_id:
                continue
            name = _text(model.get("name"))
            label = model_id if not name or name == model_id else name + " (" + model_id + ")"
            choices.append(Choice(value=model_id, label=label))
        return tuple(choices)
    return ()


def current_model(payload: Mapping[str, Any] | None) -> str:
    """Currently selected model id, if the payload reports one."""
    data = _mapping(payload)
    model = _text(_mapping(data.get("provider")).get("model"))
    if model:
        return model
    return _text(_mapping(agent_values(data)).get("model"))


def agent_values(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The agent block (provider, model, temperature, max_tokens, max_steps)."""
    data = _mapping(payload)
    agent = data.get("agent")
    if isinstance(agent, Mapping):
        return agent
    return _mapping(_mapping(data.get("config")).get("agent"))


def permission_presets(payload: Mapping[str, Any] | None) -> tuple[Choice, ...]:
    """Select options for the permission presets."""
    permissions = _mapping(_mapping(payload).get("permissions"))
    rows = permissions.get("presets")
    if not isinstance(rows, (list, tuple)):
        return ()
    choices: list[Choice] = []
    for raw in rows:
        row = _mapping(raw)
        name = _text(row.get("name"))
        if not name:
            continue
        description = _text(row.get("description"))
        label = name + " \u2014 " + description if description else name
        choices.append(Choice(value=name, label=label))
    return tuple(choices)


def current_permission(payload: Mapping[str, Any] | None) -> str:
    """Currently selected permission preset."""
    permissions = _mapping(_mapping(payload).get("permissions"))
    current = _text(permissions.get("current"))
    if current:
        return current
    choices = permission_presets(payload)
    return choices[0].value if choices else ""


def _mapping_rows(value: Any) -> tuple[Mapping[str, Any], ...]:
    """Rows of a payload list, skipping anything that is not a mapping."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(row for row in value if isinstance(row, Mapping))


# -- commands catalogue ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandRow:
    """One slash command as shown in the read-only settings catalogue."""

    name: str
    group: str
    summary: str = ""
    usage: str = ""
    aliases: tuple[str, ...] = ()
    plugin: str = ""


GROUP_ORDER: tuple[str, ...] = tuple(dict.fromkeys(COMMAND_GROUPS.values()))
GROUP_LABELS: dict[str, str] = {
    "session": "Sessions",
    "model": "Model",
    "work": "Work",
    "system": "System",
}


def group_for_command(name: str) -> str:
    """Palette group for a command name, defaulting to system."""
    return COMMAND_GROUPS.get(name, "system")


def command_rows(commands: Any) -> tuple[CommandRow, ...]:
    """Normalize the payload command list into catalogue rows."""
    if not isinstance(commands, (list, tuple)):
        return ()
    rows: list[CommandRow] = []
    for raw in commands:
        row = _mapping(raw)
        name = _text(row.get("name"))
        if not name:
            continue
        aliases = _text_list(row.get("aliases"))
        group = _text(row.get("group")) or group_for_command(name)
        rows.append(
            CommandRow(
                name=name,
                group=group,
                summary=_text(row.get("summary")),
                usage=_text(row.get("usage"), "/" + name),
                aliases=aliases,
                plugin=_text(row.get("plugin")),
            )
        )
    return tuple(rows)


def grouped_commands(rows: Sequence[CommandRow]) -> tuple[tuple[str, tuple[CommandRow, ...]], ...]:
    """Group catalogue rows for display, preserving payload order per group."""
    buckets: dict[str, list[CommandRow]] = {}
    for row in rows:
        buckets.setdefault(row.group, []).append(row)
    known = [group for group in GROUP_ORDER if group in buckets]
    extra = sorted(group for group in buckets if group not in GROUP_ORDER)
    return tuple((group, tuple(buckets[group])) for group in [*known, *extra])


# -- write feedback -------------------------------------------------------


class SaveTone(StrEnum):
    """Tone of the save bar or an inline state label."""

    IDLE = "idle"
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SaveState:
    """What the save bar reports after one write."""

    text: str
    tone: SaveTone = SaveTone.IDLE
    detail: str = ""

    @property
    def klass(self) -> str:
        """CSS class for this state."""
        return "dp-save-" + self.tone.value

    @classmethod
    def idle(cls, text: str = "Change a value, then save.") -> SaveState:
        """A calm initial hint."""
        return cls(text=text, tone=SaveTone.IDLE)

    @classmethod
    def error(cls, message: str) -> SaveState:
        """A failed write; never rendered as success."""
        return cls(text="Not saved", tone=SaveTone.ERROR, detail=message)


def _join_fields(fields: Sequence[str]) -> str:
    return ", ".join(fields)


def save_bar_state(response: Any) -> SaveState:
    """Derive the save-bar state from a bridge write response.

    Plugin settings responses carry restart_required/changed and the
    live/restart field lists; config-editor responses are plain snapshots and
    simply mean "Saved".
    """
    data = response if isinstance(response, Mapping) else {}
    changed = _text_list(data.get("changed"))
    restart_fields = _text_list(data.get("restart_fields"))
    if not restart_fields:
        restart_fields = _text_list(data.get("volatile_changed"))
    live_fields = _text_list(data.get("live_fields"))
    if not live_fields:
        live_fields = _text_list(data.get("live_changed"))
    restart = bool(data.get("restart_required"))
    if restart or restart_fields:
        detail = "next boot: " + _join_fields(restart_fields) if restart_fields else "applies on next boot"
        if changed and not restart_fields:
            detail = detail + " \u00b7 changed: " + _join_fields(changed)
        return SaveState(text="Restart required", tone=SaveTone.WARN, detail=detail)
    detail_parts: list[str] = []
    if live_fields:
        detail_parts.append("live: " + _join_fields(live_fields))
    if changed:
        detail_parts.append("changed: " + _join_fields(changed))
    return SaveState(text="Saved", tone=SaveTone.OK, detail=" \u00b7 ".join(detail_parts))


def probe_state(result: Any) -> SaveState:
    """Turn a provider test result (data, not exception) into a state label."""
    data = result if isinstance(result, Mapping) else {}
    ok = bool(data.get("ok"))
    detail = _text(data.get("detail"))
    latency = data.get("latency_ms")
    if ok:
        text = "Reachable"
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            text = text + " \u00b7 " + str(round(float(latency))) + " ms"
        return SaveState(text=text, tone=SaveTone.OK, detail="" if detail in {"", "ok"} else detail)
    return SaveState(text="Test failed", tone=SaveTone.ERROR, detail=detail or "the provider did not respond")


def plugin_save_detail(response: Any) -> str:
    """One line describing what a plugin settings write changed."""
    data = response if isinstance(response, Mapping) else {}
    parts: list[str] = []
    changed = _text_list(data.get("changed"))
    if changed:
        parts.append("changed: " + _join_fields(changed))
    live_fields = _text_list(data.get("live_fields")) or _text_list(data.get("live_changed"))
    if live_fields:
        parts.append("live: " + _join_fields(live_fields))
    restart_fields = _text_list(data.get("restart_fields")) or _text_list(data.get("volatile_changed"))
    if restart_fields:
        parts.append("restart: " + _join_fields(restart_fields))
    return " \u00b7 ".join(parts)


def error_text(exc: BaseException) -> str:
    """One compact inline error line for an exception raised by the bridge."""
    message = " ".join(str(exc).split())
    return message or type(exc).__name__


def event_value(event: Any) -> Any:
    """Read the value carried by a NiceGUI value-change event."""
    return getattr(event, "value", event)


# -- bridge protocol ------------------------------------------------------


@runtime_checkable
class SettingsBridge(Protocol):
    """The bridge methods the settings surface is allowed to call."""

    async def settings(self) -> Mapping[str, Any]:
        """Read the full settings payload."""
        ...

    async def update_agent(self, **changes: Any) -> Mapping[str, Any]:
        """Write agent settings."""
        ...

    async def update_provider(self, provider: str, **changes: Any) -> Mapping[str, Any]:
        """Write one provider connection."""
        ...

    async def test_provider(self, provider: str) -> Mapping[str, Any]:
        """Probe one provider; failures are returned as data."""
        ...

    async def set_permission(self, preset: str) -> Mapping[str, Any]:
        """Set the default permission preset."""
        ...

    def plugin_settings(self, plugin_id: str) -> Mapping[str, Any]:
        """Describe one plugin's settings schema, values and revision."""
        ...

    def update_plugin_settings(
        self,
        plugin_id: str,
        values: Mapping[str, Any],
        expected_revision: str = "",
    ) -> Mapping[str, Any]:
        """Write one plugin settings patch against the described revision."""
        ...


# -- view -----------------------------------------------------------------


class SettingsView:
    """Full-screen settings page built from native NiceGUI components.

    mount reads one settings payload through the bridge and renders the
    requested section; navigation switches sections through the registered
    /settings/{section} routes.
    """

    def __init__(
        self,
        bridge: SettingsBridge,
        *,
        section: str = DEFAULT_SECTION.value,
        on_close: Callable[[], Any] | None = None,
    ) -> None:
        self._bridge = bridge
        self._section = resolve_section(section)
        self._on_close = on_close
        self._mounted = False
        self._payload: dict[str, Any] | None = None
        self._nav: dict[SettingsSection, Any] = {}
        self._body: Any = None
        self._title: Any = None
        self._subtitle: Any = None
        self._save_status: Any = None
        self._save_detail: Any = None

    @property
    def section(self) -> SettingsSection:
        """The resolved section this view renders."""
        return self._section

    @property
    def payload(self) -> Mapping[str, Any] | None:
        """The last settings payload read from the bridge, if any."""
        return self._payload

    # -- lifecycle --------------------------------------------------------

    async def mount(self, parent: ui.element) -> None:
        """Build the page shell inside parent and load the first payload."""
        if self._mounted:
            return
        self._mounted = True
        parent.classes("dp-settings-page")
        root = ui.element("div").classes("dp-settings").style("height:100%;width:100%")
        # NiceGUI adds children to the page slot by default; the settings page
        # must live inside the route container so the viewport can lay it out.
        root.move(parent)
        with root:
            nav = ui.element("aside").classes("dp-settings-nav")
            with nav:
                brand = ui.element("div").classes("dp-settings-brand")
                with brand:
                    theme.icon("settings", size="18px")
                    ui.label("Settings").classes("dp-settings-brand-title")
                for spec in section_specs():
                    self._mount_nav_item(spec)
                spacer = ui.element("div").classes("dp-settings-nav-spacer")
                spacer.style("flex:1")
                ui.button("Back to chat", icon="close", on_click=self._close).props(
                    "flat dense no-caps"
                ).classes("dp-settings-back")
            content = ui.element("main").classes("dp-settings-content")
            with content:
                head = ui.element("header").classes("dp-settings-head")
                with head:
                    self._title = ui.label("").classes("dp-settings-title")
                    self._subtitle = ui.label("").classes("dp-settings-sub")
                self._body = ui.element("div").classes("dp-settings-body")
                bar = ui.element("footer").classes("dp-save-bar")
                with bar:
                    self._save_status = ui.label("").classes("dp-save-status")
                    self._save_detail = ui.label("").classes("dp-save-detail")
        self._sync_head()
        self._render_body()
        await self._load()

    def _mount_nav_item(self, spec: SectionSpec) -> None:
        item = ui.element("div").classes("dp-settings-nav-item")
        with item:
            theme.icon(spec.icon, size="16px")
            ui.label(spec.label).classes("dp-settings-nav-label")
        item.on("click", lambda _event, slug=spec.slug: self._navigate(slug))
        self._nav[spec.slug] = item

    async def _load(self) -> None:
        try:
            payload = await self._bridge.settings()
        except Exception as exc:  # noqa: BLE001 - the page must render the failure
            self._render_load_error(error_text(exc))
            return
        if not isinstance(payload, Mapping):
            self._render_load_error("the settings payload was not a mapping")
            return
        self._payload = dict(payload)
        self._render_body()
        self._set_save(SaveState.idle("Settings are read from config.toml and saved through the bridge."))

    async def _reload_section(self, state: SaveState | None = None) -> None:
        try:
            payload = await self._bridge.settings()
        except Exception as exc:  # noqa: BLE001 - keep the current form when refresh fails
            self._set_save(SaveState.error(error_text(exc)))
            return
        if not isinstance(payload, Mapping):
            self._set_save(SaveState.error("the settings payload was not a mapping"))
            return
        self._payload = dict(payload)
        self._render_body()
        if state is not None:
            self._set_save(state)

    async def _close(self) -> None:
        if self._on_close is None:
            ui.navigate.to("/")
            return
        result = self._on_close()
        if inspect.isawaitable(result):
            await result

    # -- shell ------------------------------------------------------------

    def _sync_head(self) -> None:
        spec = section_spec(self._section)
        if self._title is not None:
            self._title.set_text(spec.title)
        if self._subtitle is not None:
            self._subtitle.set_text(spec.description)
        for slug, item in self._nav.items():
            if slug is self._section:
                item.classes(add="active")
            else:
                item.classes(remove="active")

    def _navigate(self, section: SettingsSection) -> None:
        ui.navigate.to("/settings/" + section.value)

    def _render_body(self) -> None:
        if self._body is None:
            return
        self._sync_head()
        self._body.clear()
        with self._body:
            if self._payload is None:
                ui.label("Loading settings\u2026").classes("dp-faint")
                return
            payload = self._payload
            match self._section:
                case SettingsSection.PROVIDER:
                    self._render_provider(payload)
                case SettingsSection.AGENT:
                    self._render_agent(payload)
                case SettingsSection.PERMISSIONS:
                    self._render_permissions(payload)
                case SettingsSection.COMMANDS:
                    self._render_commands(payload)
                case SettingsSection.PLUGINS:
                    self._render_plugins(payload)
                case SettingsSection.SESSIONS:
                    self._render_sessions(payload)
                case SettingsSection.WEB:
                    self._render_web(payload)
                case SettingsSection.ABOUT:
                    self._render_about(payload)

    def _render_load_error(self, message: str) -> None:
        if self._body is None:
            return
        self._set_save(SaveState.error(message))
        self._body.clear()
        with self._body, self._card("dp-settings-card dp-error-card"):
            ui.label("Settings unavailable").classes("dp-card-title")
            ui.label(message).classes("dp-error-text")
            ui.label("The page did not load a settings payload and has not changed anything.").classes(
                "dp-field-note"
            )

    def _set_save(self, state: SaveState, detail: str | None = None) -> None:
        if self._save_status is None or self._save_detail is None:
            return
        self._save_status.classes(remove="dp-save-idle dp-save-ok dp-save-warn dp-save-error")
        self._save_status.classes(add=state.klass)
        self._save_status.set_text(state.text)
        self._save_detail.set_text(state.detail if detail is None else detail)

    # -- shared widgets ---------------------------------------------------

    @contextmanager
    def _card(self, classes: str = "dp-settings-card") -> Iterator[None]:
        with ui.element("section").classes("dp-settings-card").classes(classes):
            yield

    @contextmanager
    def _field(self, label: str, *, note: str = "", required: bool = False) -> Iterator[None]:
        with ui.element("div").classes("dp-field"):
            with ui.element("div").classes("dp-field-label"):
                ui.label(label + (" *" if required else "")).classes("dp-field-name")
                if note:
                    ui.label(note).classes("dp-field-note")
            yield

    def _inline_state(self) -> Any:
        element = ui.label("").classes("dp-inline-state")
        element.classes(add="dp-hidden")
        return element

    def _set_inline(self, element: Any, state: SaveState, *, detail: str | None = None) -> None:
        text = state.text
        note = state.detail if detail is None else detail
        if note:
            text = text + " \u2014 " + note
        element.classes(remove="dp-inline-ok dp-inline-warn dp-inline-error dp-hidden")
        tone_classes = {
            SaveTone.OK.value: "dp-inline-ok",
            SaveTone.WARN.value: "dp-inline-warn",
            SaveTone.ERROR.value: "dp-inline-error",
        }
        klass = tone_classes.get(state.tone.value, "")
        if klass:
            element.classes(add=klass)
        element.set_text(text)

    def _empty(self, title: str, detail: str = "") -> None:
        with self._card("dp-settings-card dp-empty-card"):
            ui.label(title).classes("dp-empty-title")
            if detail:
                ui.label(detail).classes("dp-empty-note")

    def _info_row(self, label: str, value: str, *, note: str = "") -> None:
        with ui.element("div").classes("dp-info-row"):
            ui.label(label).classes("dp-info-label")
            with ui.element("div").classes("dp-info-value"):
                ui.label(value).classes("dp-mono")
                if note:
                    ui.label(note).classes("dp-field-note")

    def _primary(self, text: str, handler: Callable[[], Any]) -> Any:
        return (
            ui.button(text, on_click=handler)
            .classes("dp-primary-btn")
            .props("no-caps")
            .style("background: var(--dp-accent) !important; color: var(--dp-accent-ink) !important")
        )

    def _ghost(self, text: str, handler: Callable[[], Any]) -> Any:
        return ui.button(text, on_click=handler).classes("dp-ghost-btn").props("no-caps")

    # -- provider ---------------------------------------------------------

    def _render_provider(self, payload: Mapping[str, Any]) -> None:
        providers = provider_rows(payload)
        agent = agent_values(payload)
        current = current_provider(payload)
        with self._card():
            ui.label("Model routing").classes("dp-card-title")
            ui.label("The next turn uses this provider and model.").classes("dp-field-note")
            if not providers:
                self._empty("No providers configured", "Edit config.toml to add a provider.")
            else:
                with ui.element("div").classes("dp-form-grid"):
                    provider_select = (
                        ui.select(
                            choice_options(provider_choices(payload)),
                            value=current or None,
                            label="Provider",
                        )
                        .props("outlined dense")
                        .classes("dp-form-input")
                    )
                    model_options = choice_options(model_choices(payload, current))
                    if model_options:
                        model_select = (
                            ui.select(
                                model_options,
                                value=current_model(payload) or None,
                                label="Model",
                            )
                            .props("outlined dense")
                            .classes("dp-form-input")
                        )
                    else:
                        model_select = (
                            ui.input(label="Model", value=current_model(payload))
                            .props("outlined dense")
                            .classes("dp-form-input")
                        )

                def provider_changed(event: Any) -> None:
                    name = _text(event_value(event))
                    choices = model_choices(payload, name)
                    if isinstance(model_select, ui.select):
                        model_select.set_options(
                            choice_options(choices),
                            value=choices[0].value if choices else None,
                        )

                provider_select.on_value_change(provider_changed)
                state_label = self._inline_state()

                async def apply_routing() -> None:
                    provider = _text(provider_select.value)
                    model = _text(model_select.value)
                    if not provider or not model:
                        self._set_inline(state_label, SaveState.error("choose a provider and a model"))
                        return
                    try:
                        response = await self._bridge.update_agent(provider=provider, model=model)
                    except Exception as exc:  # noqa: BLE001 - show the bridge error inline
                        message = error_text(exc)
                        self._set_inline(state_label, SaveState.error(message))
                        self._set_save(SaveState.error(message))
                        return
                    state = save_bar_state(response)
                    self._set_save(state)
                    await self._reload_section(state)

                with ui.element("div").classes("dp-form-actions"):
                    self._primary("Apply", apply_routing)
                if agent:
                    ui.label(
                        "Current: "
                        + _text(agent.get("provider"), "?")
                        + " / "
                        + _text(agent.get("model"), "?")
                    ).classes("dp-field-note")

        for row in providers:
            self._render_provider_card(row, current=current)

    def _render_provider_card(self, row: Mapping[str, Any], *, current: str) -> None:
        name = _text(row.get("name"))
        if not name:
            return
        api = _text(row.get("api"))
        base_url = _text(row.get("base_url"))
        api_key_set = bool(row.get("api_key_set"))
        with self._card("dp-provider-card"):
            with ui.element("div").classes("dp-provider-head"):
                ui.label(name).classes("dp-provider-name")
                if api:
                    theme.badge(api, tone="muted")
                theme.badge("key stored" if api_key_set else "no key", tone="ok" if api_key_set else "warn")
                if name == current:
                    theme.badge("in use", tone="ok")
            ui.label(base_url or "base_url not reported").classes("dp-provider-url dp-mono dp-faint")
            env_name = _text(row.get("api_key_env"))
            base_url_input = (
                ui.input(label="Base URL", value=base_url).props("outlined dense").classes("dp-form-input")
            )
            key_input = (
                ui.input(
                    label="API key",
                    value="",
                    placeholder="leave blank to keep the stored key",
                    password=True,
                    password_toggle_button=False,
                )
                .props("outlined dense")
                .classes("dp-form-input")
            )
            env_input = (
                ui.input(
                    label="API key env",
                    value=env_name,
                    placeholder="environment variable holding the key; empty clears it",
                )
                .props("outlined dense")
                .classes("dp-form-input")
            )
            ui.label(
                "The stored API key never leaves the server; only whether one is stored is reported."
            ).classes("dp-field-note")
            state_label = self._inline_state()

            async def save_provider() -> None:
                changes: dict[str, Any] = {}
                candidate = _text(base_url_input.value)
                if candidate and candidate != base_url:
                    changes["base_url"] = candidate
                raw_key = key_input.value
                if isinstance(raw_key, str) and raw_key.strip():
                    changes["api_key"] = raw_key.strip()
                env_value = _text(env_input.value).strip()
                if env_value != env_name:
                    changes["api_key_env"] = env_value
                if not changes:
                    self._set_inline(state_label, SaveState.idle("No changes to save."))
                    return
                try:
                    response = await self._bridge.update_provider(name, **changes)
                except Exception as exc:  # noqa: BLE001 - show the bridge error inline
                    message = error_text(exc)
                    self._set_inline(state_label, SaveState.error(message))
                    self._set_save(SaveState.error(message))
                    return
                state = save_bar_state(response)
                self._set_save(state)
                await self._reload_section(state)

            async def test_provider() -> None:
                try:
                    result = await self._bridge.test_provider(name)
                except Exception as exc:  # noqa: BLE001 - probe errors are shown, not raised
                    self._set_inline(state_label, SaveState.error(error_text(exc)))
                    return
                self._set_inline(state_label, probe_state(result))

            with ui.element("div").classes("dp-form-actions"):
                self._primary("Save", save_provider)
                self._ghost("Test", test_provider)

    # -- agent ------------------------------------------------------------

    def _render_agent(self, payload: Mapping[str, Any]) -> None:
        agent = agent_values(payload)
        if not agent:
            self._empty("Agent settings unavailable", "The settings payload did not include an agent block.")
            return
        temperature = agent.get("temperature")
        max_tokens = agent.get("max_tokens")
        max_steps = agent.get("max_steps")
        with self._card():
            ui.label("Sampling").classes("dp-card-title")
            with self._field(
                "Temperature", note="0.0 to 2.0; the bridge rejects anything outside that range."
            ):
                temperature_label = ui.label("").classes("dp-field-value dp-mono")
                slider = ui.slider(
                    min=0.0,
                    max=2.0,
                    step=0.05,
                    value=float(temperature) if isinstance(temperature, (int, float)) else 0.7,
                ).classes("dp-slider")

                def temperature_changed(event: Any) -> None:
                    temperature_label.set_text(format(float(event_value(event)), ".2f"))

                slider.on_value_change(temperature_changed)
                temperature_changed(slider.value)
            with self._field("Max tokens", note="Positive integer; server-validated."):
                tokens_input = (
                    ui.number(
                        value=int(max_tokens) if isinstance(max_tokens, int) else None, min=1, precision=0
                    )
                    .props("outlined dense")
                    .classes("dp-form-input")
                )
            with self._field("Max steps", note="Positive integer; bounds one agent turn."):
                steps_input = (
                    ui.number(
                        value=int(max_steps) if isinstance(max_steps, int) else None, min=1, precision=0
                    )
                    .props("outlined dense")
                    .classes("dp-form-input")
                )
            state_label = self._inline_state()

            async def save_agent() -> None:
                try:
                    temperature_value = float(slider.value) if slider.value is not None else 0.0
                    tokens_value = int(tokens_input.value) if tokens_input.value is not None else 0
                    steps_value = int(steps_input.value) if steps_input.value is not None else 0
                except (TypeError, ValueError):
                    self._set_inline(
                        state_label,
                        SaveState.error("temperature, max tokens and max steps must be numbers"),
                    )
                    return
                if not 0.0 <= temperature_value <= 2.0:
                    self._set_inline(state_label, SaveState.error("temperature must be between 0.0 and 2.0"))
                    return
                if tokens_value <= 0 or steps_value <= 0:
                    self._set_inline(
                        state_label, SaveState.error("max tokens and max steps must be positive")
                    )
                    return
                try:
                    response = await self._bridge.update_agent(
                        temperature=temperature_value,
                        max_tokens=tokens_value,
                        max_steps=steps_value,
                    )
                except Exception as exc:  # noqa: BLE001 - show the bridge error inline
                    message = error_text(exc)
                    self._set_inline(state_label, SaveState.error(message))
                    self._set_save(SaveState.error(message))
                    return
                state = save_bar_state(response)
                self._set_save(state)
                await self._reload_section(state)

            with ui.element("div").classes("dp-form-actions"):
                self._primary("Save", save_agent)

    # -- permissions ------------------------------------------------------

    def _render_permissions(self, payload: Mapping[str, Any]) -> None:
        permissions = _mapping(payload.get("permissions"))
        presets = permission_presets(payload)
        if not presets:
            self._empty("No permission presets", "The settings payload did not include a preset catalog.")
            return
        current = current_permission(payload)
        custom = bool(permissions.get("custom"))
        default = _text(permissions.get("default"))
        rows = {_text(row.get("name")): row for row in _mapping_rows(permissions.get("presets"))}
        with self._card():
            ui.label("Permission preset").classes("dp-card-title")
            ui.label("Applied to new turns; running turns keep their preset.").classes("dp-field-note")
            select = (
                ui.select(choice_options(presets), value=current or presets[0].value, label="Preset")
                .props("outlined dense")
                .classes("dp-form-input")
            )
            detail = ui.label("").classes("dp-field-note")

            def show_detail(value: Any) -> None:
                row = rows.get(_text(value), {})
                parts = [key + ": " + _text(row.get(key)) for key in ("sandbox", "approval", "description")]
                detail.set_text(" \u00b7 ".join(part for part in parts if not part.endswith(": ")))

            show_detail(select.value)
            select.on_value_change(lambda event: show_detail(event_value(event)))
            if custom:
                theme.badge("custom permission mode", tone="warn")
            if default:
                ui.label("Config default: " + default).classes("dp-field-note")
            state_label = self._inline_state()

            async def save_permission() -> None:
                preset = _text(select.value)
                if not preset:
                    self._set_inline(state_label, SaveState.error("choose a permission preset"))
                    return
                try:
                    response = await self._bridge.set_permission(preset)
                except Exception as exc:  # noqa: BLE001 - show the bridge error inline
                    message = error_text(exc)
                    self._set_inline(state_label, SaveState.error(message))
                    self._set_save(SaveState.error(message))
                    return
                state = save_bar_state(response)
                self._set_save(state)
                await self._reload_section(state)

            with ui.element("div").classes("dp-form-actions"):
                self._primary("Save", save_permission)

    # -- commands ---------------------------------------------------------

    def _render_commands(self, payload: Mapping[str, Any]) -> None:
        rows = command_rows(payload.get("commands"))
        if not rows:
            self._empty("No commands reported", "The settings payload did not include a command catalog.")
            return
        ui.label(
            "Every command is addressable from the chat composer and the command palette. "
            "This page documents them; it never executes one."
        ).classes("dp-section-note")
        for group, group_rows in grouped_commands(rows):
            with self._card("dp-command-group"):
                with ui.element("div").classes("dp-command-head"):
                    ui.label(GROUP_LABELS.get(group, group.title())).classes("dp-group-title")
                    theme.badge(str(len(group_rows)), tone="muted")
                for row in group_rows:
                    with ui.element("div").classes("dp-command-row"):
                        with ui.element("div").classes("dp-command-main"):
                            with ui.element("div").classes("dp-command-name-row"):
                                ui.label("/" + row.name).classes("dp-command-name dp-mono")
                                if row.plugin:
                                    theme.badge(row.plugin, tone="muted")
                            if row.summary:
                                ui.label(row.summary).classes("dp-command-summary")
                        if row.usage:
                            ui.label(row.usage).classes("dp-command-usage dp-mono dp-faint")
                        aliases = ", ".join(row.aliases)
                        if aliases:
                            ui.label("aliases: " + aliases).classes("dp-command-meta dp-faint")

    # -- plugins ----------------------------------------------------------

    def _render_plugins(self, payload: Mapping[str, Any]) -> None:
        plugins = _mapping(payload.get("plugins"))
        rows = _mapping_rows(plugins.get("items"))
        revision = _text(plugins.get("revision"))
        ui.label(
            "Plugin settings are read and written through the settings store. "
            "Each plugin exposes its own pydantic schema."
        ).classes("dp-section-note")
        if revision:
            ui.label("Store revision " + revision).classes("dp-field-note dp-mono")
        if not rows:
            self._empty("No plugins loaded", "The kernel did not report any plugin items.")
            return
        for row in rows:
            self._render_plugin_card(row)

    def _render_plugin_card(self, row: Mapping[str, Any]) -> None:
        plugin_id = _text(row.get("id"))
        name = _text(row.get("name")) or plugin_id
        if not plugin_id:
            return
        version = _text(row.get("version"))
        state = _text(row.get("state"))
        enabled = bool(row.get("enabled"))
        error = _text(row.get("error"))
        with self._card("dp-plugin-card"):
            with ui.element("div").classes("dp-plugin-head"):
                with ui.element("div").classes("dp-plugin-title"):
                    ui.label(name).classes("dp-plugin-name")
                    if version:
                        ui.label("v" + version).classes("dp-plugin-id dp-mono dp-faint")
                    if plugin_id != name:
                        ui.label(plugin_id).classes("dp-plugin-id dp-mono dp-faint")
                if state:
                    theme.badge(state, tone="ok" if enabled else "muted")
                theme.badge("enabled" if enabled else "disabled", tone="ok" if enabled else "warn")
            if _text(row.get("description")):
                ui.label(_text(row.get("description"))).classes("dp-plugin-desc")
            if error:
                ui.label(error).classes("dp-error-text")
            expansion = ui.expansion("Configure", icon="tune").classes("dp-plugin-configure")
            with expansion:
                fields_container = ui.element("div").classes("dp-plugin-fields")
            with fields_container:
                ui.label("Expand to load this plugin's settings.").classes("dp-field-note")
            loaded = {"done": False}

            def expanded(event: Any) -> None:
                if not event_value(event) or loaded["done"]:
                    return
                loaded["done"] = True
                fields_container.clear()
                try:
                    described = self._bridge.plugin_settings(plugin_id)
                except Exception as exc:  # noqa: BLE001 - plugin describe errors are shown inline
                    with fields_container:
                        ui.label(error_text(exc)).classes("dp-error-text")
                    return
                if not isinstance(described, Mapping):
                    with fields_container:
                        ui.label("The settings store returned an unexpected payload.").classes(
                            "dp-error-text"
                        )
                    return
                with fields_container:
                    self._render_plugin_fields(plugin_id, described)

            expansion.on_value_change(expanded)

    def _render_plugin_fields(self, plugin_id: str, described: Mapping[str, Any]) -> None:
        config_source = _text(described.get("config_source"))
        target = _text(described.get("target"))
        if config_source or target:
            parts = [part for part in (config_source, target) if part]
            ui.label("source: " + " / ".join(parts)).classes("dp-field-note")
        if described.get("disabled"):
            theme.badge("settings disabled", tone="warn")
        error = _text(described.get("error"))
        if error:
            ui.label(error).classes("dp-error-text")
        schema = described.get("schema")
        if not isinstance(schema, Mapping):
            ui.label(
                "This plugin does not expose an editable settings schema; nothing can be edited here."
            ).classes("dp-field-note dp-faint")
            return
        descriptors = schema_fields(schema)
        if not descriptors:
            unsupported = unsupported_schema_fields(schema)
            detail = "This schema has no editable scalar fields."
            if unsupported:
                detail += " Unsupported: " + ", ".join(unsupported) + "."
            ui.label(detail).classes("dp-field-note dp-faint")
            return
        values = described.get("values")
        stored = values if isinstance(values, Mapping) else {}
        controls: dict[str, tuple[FieldDescriptor, Any]] = {}
        for descriptor in descriptors:
            initial = stored.get(descriptor.name, descriptor.default)
            control = self._render_plugin_field(descriptor, initial)
            controls[descriptor.name] = (descriptor, control)
        live_fields = _text_list(described.get("live_fields"))
        restart_fields = _text_list(described.get("restart_fields"))
        if live_fields:
            ui.label("Live fields: " + ", ".join(live_fields)).classes("dp-field-note")
        if restart_fields:
            ui.label("Restart required for: " + ", ".join(restart_fields)).classes("dp-field-note")
        state_label = self._inline_state()
        revision = _text(described.get("revision"))

        async def save_plugin() -> None:
            raw_values = {name: control.value for name, (_descriptor, control) in controls.items()}
            try:
                prepared = collect_plugin_values(
                    [descriptor for descriptor, _c in controls.values()], raw_values
                )
            except ValueError as exc:
                self._set_inline(state_label, SaveState.error(str(exc)))
                return
            try:
                response = self._bridge.update_plugin_settings(
                    plugin_id, prepared, expected_revision=revision
                )
            except Exception as exc:  # noqa: BLE001 - show the store error inline
                message = error_text(exc)
                self._set_inline(state_label, SaveState.error(message))
                self._set_save(SaveState.error(message))
                return
            if not isinstance(response, Mapping):
                self._set_inline(
                    state_label, SaveState.error("The settings store returned an unexpected payload.")
                )
                return
            detail = plugin_save_detail(response)
            if bool(response.get("restart_required")):
                state = SaveState("Saved \u00b7 restart required", SaveTone.WARN, detail)
            elif bool(response.get("volatile_changed")):
                state = SaveState("Saved \u00b7 volatile field", SaveTone.WARN, detail)
            else:
                state = SaveState("Saved", SaveTone.OK, detail)
            self._set_inline(state_label, state)
            self._set_save(state)

        with ui.element("div").classes("dp-form-actions"):
            self._primary("Save plugin settings", save_plugin)

    def _render_plugin_field(self, descriptor: FieldDescriptor, initial: Any) -> Any:
        with self._field(descriptor.label, note=descriptor.description, required=descriptor.required):
            return self._schema_control(descriptor, initial)

    def _schema_control(self, descriptor: FieldDescriptor, initial: Any) -> Any:
        if descriptor.secret:
            placeholder = "leave blank to keep the stored value"
            value = ""
            if isinstance(initial, str) and initial and not set(initial) <= {"*", "\u2022"}:
                value = initial
            return (
                ui.input(value=value, placeholder=placeholder, password=True, password_toggle_button=False)
                .props("outlined dense")
                .classes("dp-form-input")
            )
        if descriptor.kind is FieldKind.BOOLEAN:
            return ui.switch(value=bool(initial)).classes("dp-form-switch")
        if descriptor.kind is FieldKind.ENUM:
            options = choice_options(
                [Choice(value=str(option.value), label=option.label) for option in descriptor.options]
            )
            value = initial if initial in [str(option.value) for option in descriptor.options] else None
            return ui.select(options, value=value).props("outlined dense").classes("dp-form-input")
        if descriptor.kind in (FieldKind.INTEGER, FieldKind.NUMBER):
            numeric = initial if isinstance(initial, (int, float)) and not isinstance(initial, bool) else None
            control = ui.number(
                value=numeric,
                min=descriptor.minimum,
                max=descriptor.maximum,
                precision=0 if descriptor.kind is FieldKind.INTEGER else None,
            )
            return control.props("outlined dense").classes("dp-form-input")
        text = initial if isinstance(initial, str) else ""
        return ui.input(value=text).props("outlined dense").classes("dp-form-input")

    # -- sessions ---------------------------------------------------------

    def _render_sessions(self, payload: Mapping[str, Any]) -> None:
        sessions = _mapping(payload.get("sessions"))
        items = _mapping_rows(sessions.get("items"))
        active = _text(sessions.get("active_session_id"))
        count = sessions.get("count")
        with self._card("dp-session-list"):
            with ui.element("div").classes("dp-card-head"):
                ui.label("Recent sessions").classes("dp-card-title")
                if isinstance(count, int):
                    theme.badge(str(count) + " total", tone="muted")
            ui.label("Open a session in the chat view. Archived sessions are not listed.").classes(
                "dp-field-note"
            )
            if not items:
                ui.label("No sessions yet; start one from the chat page.").classes("dp-field-note dp-faint")
            for row in items:
                session_id = _text(row.get("id"))
                if not session_id:
                    continue
                with (
                    ui.element("div")
                    .classes("dp-session-row")
                    .on("click", lambda _event, sid=session_id: ui.navigate.to("/c/" + sid))
                ):
                    with ui.element("div").classes("dp-session-main"):
                        with ui.element("div").classes("dp-session-title-row"):
                            ui.label(_text(row.get("title"), "Untitled session")).classes("dp-session-title")
                            if session_id == active:
                                theme.badge("active", tone="ok")
                        if _text(row.get("preview")):
                            ui.label(_text(row.get("preview"))).classes("dp-session-preview")
                    with ui.element("div").classes("dp-session-meta"):
                        if _text(row.get("preset")):
                            theme.badge(_text(row.get("preset")), tone="muted")
                        turns = row.get("turns")
                        if isinstance(turns, int):
                            ui.label(str(turns) + " turns").classes("dp-session-meta-text dp-faint")
                        ui.label(theme.human_time(row.get("updated_at"))).classes(
                            "dp-session-meta-text dp-faint"
                        )
            with ui.element("div").classes("dp-form-actions"):
                target = "/c/" + active if active else "/"
                ui.button("Open chat", icon="chat_bubble", on_click=lambda: ui.navigate.to(target)).props(
                    "unelevated no-caps dense"
                ).classes("dp-primary-btn").style(
                    "background: var(--dp-accent) !important; color: var(--dp-accent-ink) !important"
                )

    # -- web --------------------------------------------------------------

    def _render_web(self, payload: Mapping[str, Any]) -> None:
        web = _mapping(payload.get("web"))
        host = _text(web.get("host"), "127.0.0.1")
        port = web.get("port")
        about = _mapping(payload.get("about"))
        with self._card():
            ui.label("Web surface").classes("dp-card-title")
            ui.label("This host-only UI binds through the DSH web service.").classes("dp-field-note")
            self._info_row("Host", host)
            if port is not None:
                self._info_row("Port", str(port))
            self._info_row("URL", "http://" + host + (":" + str(port) if port is not None else ""))
            loopback = host in {"127.0.0.1", "::1", "localhost"}
            theme.badge(
                "loopback only" if loopback else "non-loopback host", tone="ok" if loopback else "warn"
            )
            self._info_row("Version", _text(about.get("version"), "unknown"), note="From the about section.")
        with self._card():
            ui.label("Access and cookies").classes("dp-card-title")
            ui.label(
                "Browser access is authorised by the host-local session token the web service issues. "
                "The settings payload does not expose the token or cookie value, so this page never displays it."
            ).classes("dp-field-note")
            ui.label("Do not reverse-proxy this UI: the loopback binding is the security boundary.").classes(
                "dp-field-note dp-faint"
            )

    # -- about ------------------------------------------------------------

    def _render_about(self, payload: Mapping[str, Any]) -> None:
        about = _mapping(payload.get("about"))
        config = _mapping(payload.get("config"))
        web = _mapping(payload.get("web"))
        with self._card():
            ui.label("About").classes("dp-card-title")
            self._info_row("Version", _text(about.get("version"), "unknown"))
            self._info_row("Python", _text(about.get("python"), "unknown"))
            self._info_row("Platform", _text(about.get("platform"), "unknown"))
            plugins = about.get("plugins")
            if isinstance(plugins, int):
                self._info_row("Plugins", str(plugins))
            self._info_row("Runtime", "NiceGUI host web UI")
            self._info_row("Web host", _text(web.get("host"), "unknown"))
            self._info_row("Config", _text(config.get("path"), "unknown"), note="config.toml")
        with self._card():
            ui.label("Design constraints").classes("dp-card-title")
            ui.label(
                "Everything is a plugin, data flows through the session log, and traces record tokens, "
                "latency and retries."
            ).classes("dp-field-note")
            ui.label(
                "The settings surface is AI-native: changes are written to the same config the agent reads, "
                "and plugin settings describe their live and restart fields."
            ).classes("dp-field-note dp-faint")


__all__ = [
    "DEFAULT_SECTION",
    "CommandRow",
    "FieldDescriptor",
    "FieldKind",
    "FieldOption",
    "SaveState",
    "SaveTone",
    "SectionSpec",
    "SettingsBridge",
    "SettingsSection",
    "SettingsView",
    "agent_values",
    "choice_options",
    "coerce_field_value",
    "collect_plugin_values",
    "command_rows",
    "current_model",
    "current_permission",
    "current_provider",
    "error_text",
    "group_for_command",
    "grouped_commands",
    "humanize_label",
    "model_choices",
    "permission_presets",
    "plugin_save_detail",
    "probe_state",
    "provider_choices",
    "provider_rows",
    "resolve_section",
    "save_bar_state",
    "schema_fields",
    "section_spec",
    "section_specs",
    "unsupported_schema_fields",
]
