"""Command palette dialog: filter commands, sessions and settings.

The palette is a single NiceGUI dialog. Pure helpers below build and filter
the rows and route the bridge command results; the CommandPalette class only
renders them and forwards navigation to the shell callback.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from nicegui import ui

from deeppyro.web.ui import theme
from deeppyro.web.ui.api import COMMAND_GROUPS
from deeppyro.web.ui.settings_view import section_spec

#: Settings sections listed in the palette (About is not a destination).
SETTINGS_SLUGS: tuple[str, ...] = (
    "provider",
    "agent",
    "permissions",
    "commands",
    "plugins",
    "web",
)

#: Quasar icon for each command group.
COMMAND_ICONS: dict[str, str] = {
    "session": "sessions",
    "model": "cpu",
    "work": "bolt",
    "system": "terminal",
}


class PaletteKind(StrEnum):
    """What activating a palette row does."""

    COMMAND = "command"
    SESSION = "session"
    SETTINGS = "settings"


class PaletteGroup(StrEnum):
    """Visible palette groups, in rendering order."""

    COMMANDS = "Commands"
    SESSIONS = "Sessions"
    SETTINGS = "Settings"


class PaletteAction(StrEnum):
    """What the palette should do after a command returns."""

    NAVIGATE = "navigate"
    MESSAGE = "message"
    ERROR = "error"


PALETTE_GROUPS: tuple[PaletteGroup, ...] = (
    PaletteGroup.COMMANDS,
    PaletteGroup.SESSIONS,
    PaletteGroup.SETTINGS,
)


@dataclass(frozen=True, slots=True)
class PaletteRow:
    """One searchable, activatable row in the palette."""

    group: PaletteGroup
    kind: PaletteKind
    title: str
    subtitle: str = ""
    icon: str = "terminal"
    shortcut: str = ""
    command: str = ""
    path: str = ""
    session_id: str = ""

    @property
    def haystack(self) -> str:
        """Lower-cased text the filter matches against."""
        parts = (
            self.title,
            self.subtitle,
            self.group.value,
            self.command,
            self.path,
            self.session_id,
        )
        return " ".join(part.lower() for part in parts if part)


@dataclass(frozen=True, slots=True)
class CommandAction:
    """A routed command result: navigate, show a message, or show an error."""

    kind: PaletteAction
    path: str = ""
    message: str = ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _rows(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(row for row in value if isinstance(row, Mapping))


def palette_rows(bootstrap: Mapping[str, Any] | None) -> tuple[PaletteRow, ...]:
    """Build the full row list from one bootstrap payload."""
    data = _mapping(bootstrap)
    rows: list[PaletteRow] = []
    for raw in _rows(data.get("commands")):
        name = _text(raw.get("name"))
        if not name:
            continue
        group_name = _text(raw.get("group")) or COMMAND_GROUPS.get(name, "system")
        rows.append(
            PaletteRow(
                group=PaletteGroup.COMMANDS,
                kind=PaletteKind.COMMAND,
                title="/" + name,
                subtitle=_text(raw.get("summary")) or _text(raw.get("usage")),
                icon=COMMAND_ICONS.get(group_name, "terminal"),
                shortcut=_text(raw.get("shortcut")),
                command=name,
            )
        )
    active = _text(data.get("active_session_id"))
    for raw in _rows(data.get("sessions")):
        session_id = _text(raw.get("session_id")) or _text(raw.get("id"))
        if not session_id:
            continue
        parts: list[str] = []
        preset = _text(raw.get("preset"))
        if preset:
            parts.append(preset)
        provider = _text(raw.get("provider"))
        if provider:
            parts.append(provider)
        turns = raw.get("turns")
        if isinstance(turns, int) and not isinstance(turns, bool):
            parts.append(str(turns) + " turns")
        when = theme.human_time(raw.get("updated_at"))
        if when:
            parts.append(when)
        if session_id == active:
            parts.append("active")
        rows.append(
            PaletteRow(
                group=PaletteGroup.SESSIONS,
                kind=PaletteKind.SESSION,
                title=_text(raw.get("title"), "Untitled session"),
                subtitle=" \u00b7 ".join(parts),
                icon="sessions",
                path="/c/" + session_id,
                session_id=session_id,
            )
        )
    for slug in SETTINGS_SLUGS:
        spec = section_spec(slug)
        rows.append(
            PaletteRow(
                group=PaletteGroup.SETTINGS,
                kind=PaletteKind.SETTINGS,
                title=spec.title,
                subtitle=spec.description,
                icon=spec.icon,
                path="/settings/" + slug,
            )
        )
    return tuple(rows)


def filter_palette(rows: Sequence[PaletteRow], query: str | None) -> tuple[PaletteRow, ...]:
    """Filter rows case-insensitively; every whitespace token must match."""
    tokens = [token for token in str(query or "").lower().split() if token]
    if not tokens:
        return tuple(rows)
    return tuple(row for row in rows if all(token in row.haystack for token in tokens))


def grouped_rows(rows: Sequence[PaletteRow]) -> tuple[tuple[PaletteGroup, tuple[PaletteRow, ...]], ...]:
    """Group rows for display, preserving the fixed group order."""
    buckets: dict[PaletteGroup, list[PaletteRow]] = {group: [] for group in PALETTE_GROUPS}
    for row in rows:
        buckets.setdefault(row.group, []).append(row)
    return tuple((group, tuple(buckets[group])) for group in PALETTE_GROUPS if buckets[group])


def route_command_result(result: Any) -> CommandAction:
    """Route a CommandResult (mapping or object) to a palette action."""
    payload = result.to_payload() if callable(getattr(result, "to_payload", None)) else result
    data = _mapping(payload)
    status = _text(data.get("status")).lower()
    message = _text(data.get("message"))
    redirect = _text(data.get("redirect"))
    if status == "redirect" and redirect:
        return CommandAction(kind=PaletteAction.NAVIGATE, path=redirect)
    extra = _mapping(data.get("data"))
    session_id = _text(extra.get("session_id"))
    if session_id:
        return CommandAction(kind=PaletteAction.NAVIGATE, path="/c/" + session_id)
    path = _text(extra.get("path"))
    if path:
        return CommandAction(kind=PaletteAction.NAVIGATE, path=path)
    if status == "error":
        return CommandAction(kind=PaletteAction.ERROR, message=message or "the command did not run")
    if status == "ok":
        return CommandAction(kind=PaletteAction.MESSAGE, message=message or "Command finished.")
    return CommandAction(
        kind=PaletteAction.ERROR,
        message="unexpected command result" + (" (" + status + ")" if status else ""),
    )


def key_name(event: Any) -> str:
    """Best-effort key name from a NiceGUI keydown event."""
    raw = getattr(event, "args", event)
    if isinstance(raw, Mapping):
        raw = raw.get("key")
    elif isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    return _text(raw)


# -- bridge protocol ------------------------------------------------------


@runtime_checkable
class PaletteBridge(Protocol):
    """The bridge methods the palette is allowed to call."""

    async def bootstrap(self) -> Mapping[str, Any]:
        """Read the first-paint payload with commands and sessions."""
        ...

    async def execute(
        self,
        name: str,
        args: list[str] | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Run one slash command and return its CommandResult."""
        ...


def error_text(exc: BaseException) -> str:
    """One compact inline error line."""
    message = " ".join(str(exc).split())
    return message or type(exc).__name__


def _event_value(event: Any) -> Any:
    return getattr(event, "value", event)


# -- dialog ---------------------------------------------------------------


class CommandPalette:
    """Centered command palette dialog over the shell page.

    mount builds the dialog once and loads commands/sessions from the
    bootstrap payload; open refreshes that payload and reveals the dialog.
    Activation never claims a command ran unless the bridge says it did.
    """

    def __init__(
        self,
        bridge: PaletteBridge,
        *,
        on_navigate: Callable[[str], Any] | None = None,
    ) -> None:
        self._bridge = bridge
        self._on_navigate = on_navigate
        self._mounted = False
        self._loaded = False
        self._rows: tuple[PaletteRow, ...] = ()
        self._visible: tuple[PaletteRow, ...] = ()
        self._active = 0
        self._dialog: Any = None
        self._search: Any = None
        self._list: Any = None
        self._error: Any = None
        self._status: Any = None

    @property
    def rows(self) -> tuple[PaletteRow, ...]:
        """The last row list loaded from the bootstrap payload."""
        return self._rows

    @property
    def visible(self) -> tuple[PaletteRow, ...]:
        """The rows currently passing the filter."""
        return self._visible

    @property
    def active_index(self) -> int:
        """Index of the highlighted row within the visible rows."""
        return self._active

    # -- lifecycle --------------------------------------------------------

    async def mount(self, parent: ui.element) -> None:
        """Build the dialog inside parent and load the bootstrap payload."""
        if self._mounted:
            return
        self._mounted = True
        with parent:
            self._dialog = ui.dialog().props("position=top")
            with self._dialog, ui.card().classes("dp-palette"):
                with ui.element("div").classes("dp-palette-input"):
                    theme.icon("search", size="18px")
                    self._search = (
                        ui.input(placeholder="Search commands, sessions and settings")
                        .props("borderless dense autofocus")
                        .classes("dp-palette-search")
                    )
                    self._search.on_value_change(lambda event: self._apply_filter(_event_value(event)))
                    self._search.on("keydown", self._handle_key, args=["key"])
                self._list = ui.element("div").classes("dp-palette-list")
                self._error = ui.label("").classes("dp-palette-error dp-hidden")
                with ui.element("div").classes("dp-palette-foot"):
                    ui.label("Enter run \u00b7 \u2191\u2193 choose \u00b7 Esc close").classes(
                        "dp-palette-hint"
                    )
                    self._status = ui.label("").classes("dp-palette-status dp-faint")
        await self._load()

    async def open(self) -> None:
        """Refresh the payload, reset the filter and reveal the dialog."""
        if self._dialog is None:
            return
        await self._load()
        if self._search is not None:
            self._search.value = ""
        self._apply_filter("")
        self._dialog.open()
        if self._search is not None:
            self._search.run_method("focus")

    def close(self) -> None:
        """Hide the dialog without navigating."""
        if self._dialog is not None:
            self._dialog.close()

    async def _load(self) -> None:
        try:
            payload = await self._bridge.bootstrap()
        except Exception as exc:  # noqa: BLE001 - the palette must show the failure
            self._rows = ()
            self._apply_filter("")
            self._show_error(error_text(exc))
            return
        self._rows = palette_rows(payload if isinstance(payload, Mapping) else {})
        self._loaded = True
        self._apply_filter("")

    # -- rendering --------------------------------------------------------

    def _apply_filter(self, query: Any) -> None:
        self._visible = filter_palette(self._rows, str(query or ""))
        self._active = 0
        self._render_rows()
        if self._status is not None:
            if not self._rows:
                self._status.set_text("")
            elif not self._visible:
                self._status.set_text("No matches")
            else:
                self._status.set_text(str(len(self._visible)) + " results")

    def _render_rows(self) -> None:
        if self._list is None:
            return
        self._list.clear()
        with self._list:
            if not self._visible:
                ui.label("Nothing matches").classes("dp-palette-empty")
                return
            index = 0
            for group, rows in grouped_rows(self._visible):
                ui.label(group.value).classes("dp-palette-group")
                for row in rows:
                    self._render_row(row, index == self._active)
                    index += 1

    def _render_row(self, row: PaletteRow, active: bool) -> None:
        with (
            ui.element("div")
            .classes("dp-palette-row" + (" active" if active else ""))
            .on("click", lambda _event, target=row: self._activate(target))
        ):
            theme.icon(row.icon, size="16px")
            with ui.element("div").classes("dp-palette-row-main"):
                ui.label(row.title).classes("dp-palette-row-title")
                if row.subtitle:
                    ui.label(row.subtitle).classes("dp-palette-row-sub")
            if row.shortcut:
                ui.label(row.shortcut).classes("dp-palette-row-shortcut dp-mono")

    def _set_active(self, index: int) -> None:
        if not self._visible:
            return
        self._active = index % len(self._visible)
        self._render_rows()

    # -- keyboard ---------------------------------------------------------

    async def _handle_key(self, event: Any) -> None:
        key = key_name(event)
        if key in {"Enter", "NumpadEnter"}:
            if self._visible:
                await self._activate(self._visible[self._active])
            return
        if key == "ArrowDown":
            self._set_active(self._active + 1)
            return
        if key == "ArrowUp":
            self._set_active(self._active - 1)
            return
        if key in {"Escape", "Esc"}:
            self.close()

    # -- activation -------------------------------------------------------

    async def _activate(self, row: PaletteRow) -> None:
        if row.kind is PaletteKind.COMMAND:
            await self._run_command(row)
            return
        if row.path:
            await self._navigate(row.path)

    async def _run_command(self, row: PaletteRow) -> None:
        self._clear_error()
        try:
            result = await self._bridge.execute(row.command, [])
        except Exception as exc:  # noqa: BLE001 - command failures are shown inline
            message = error_text(exc)
            self._show_error(message)
            theme.notify_error(message)
            return
        action = route_command_result(result)
        if action.kind is PaletteAction.NAVIGATE:
            await self._navigate(action.path)
            return
        if action.kind is PaletteAction.MESSAGE:
            self.close()
            if self._status is not None:
                self._status.set_text(action.message)
            theme.notify_ok(action.message)
            return
        self._show_error(action.message)
        theme.notify_error(action.message)

    async def _navigate(self, path: str) -> None:
        target = path.strip()
        if not target:
            return
        self.close()
        if callable(self._on_navigate):
            result = self._on_navigate(target)
            if inspect.isawaitable(result):
                await result
            return
        ui.navigate.to(target)

    def _show_error(self, message: str) -> None:
        if self._error is None:
            return
        self._error.set_text(message)
        self._error.classes(remove="dp-hidden")

    def _clear_error(self) -> None:
        if self._error is None:
            return
        self._error.set_text("")
        self._error.classes(add="dp-hidden")


__all__ = [
    "COMMAND_ICONS",
    "PALETTE_GROUPS",
    "SETTINGS_SLUGS",
    "CommandAction",
    "CommandPalette",
    "PaletteAction",
    "PaletteBridge",
    "PaletteGroup",
    "PaletteKind",
    "PaletteRow",
    "error_text",
    "filter_palette",
    "grouped_rows",
    "key_name",
    "palette_rows",
    "route_command_result",
]
